"""tests/test_api_chat.py — /chat + /chat/stream 端点测试

用 FastAPI TestClient + mock app state，不依赖真实 LLM/Redis/PG。
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.engines.loop import LoopResult
from agent.llm.intent_router import Intent
from agent.llm.resolve import resolve_pronouns
from agent.llm.session import SessionContext
from api.chat import chat_router
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import BaseAppException


# =============================================================================
# Mocks
# =============================================================================
class _MockIntentRouter:
    """总是返回 agent（走 AgentLoop，不调 hybrid_search）"""

    async def route(self, query: str = "") -> Intent:
        return Intent(
            target="agent",
            table="",
            query=query,
            confidence=0.95,
        )


class _MockAgentLoop:
    """总是返回固定回答"""

    def __init__(self, answer: str = "Mock 回答"):
        self._answer = answer

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        return LoopResult(
            answer=self._answer,
            total_steps=1,
            total_tokens=50,
            total_latency_ms=100.0,
        )

    async def run_stream(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        """模拟流式回答"""
        yield {"event": "start"}
        for char in self._answer:
            yield {"event": "token", "content": char}
        yield {
            "event": "done",
            "answer": self._answer,
            "total_steps": 1,
        }


class _MockSessionManager:
    """内存版 SessionManager，不依赖 PostgreSQL。"""

    def __init__(self):
        self._sessions: dict[str, SessionContext] = {}

    async def get_or_create(
        self,
        session_id: str | None,
        owner_user_id: int,
    ) -> SessionContext | None:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or "mock-session-id"
        ctx = SessionContext(session_id=sid)
        self._sessions[sid] = ctx
        return ctx

    async def resolve(
        self,
        query: str,
        session_id: str | None,
        owner_user_id: int,
    ) -> str:
        if session_id and session_id in self._sessions:
            return resolve_pronouns(query, self._sessions[session_id].last_entities)
        return query

    async def add_turn(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        result: LoopResult,
    ) -> None:
        ctx = self._sessions.get(session_id)
        if ctx:
            ctx.messages.append({"role": "user", "content": query})
            ctx.messages.append({"role": "assistant", "content": result.answer})
            if result.last_entities:
                ctx.last_entities.update(result.last_entities)

    async def add_turn_simple(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        answer: str,
    ) -> None:
        ctx = self._sessions.get(session_id)
        if ctx:
            ctx.messages.append({"role": "user", "content": query})
            ctx.messages.append({"role": "assistant", "content": answer})


# =============================================================================
# TestClient fixture
# =============================================================================
@pytest.fixture
def client():
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user = {"id": 1, "username": "test-user", "role": "customer"}
        return await call_next(request)

    app.include_router(chat_router)
    app.state.agent = _MockAgentLoop(answer="这是测试回答")
    app.state.session = _MockSessionManager()
    app.state.intent_router = _MockIntentRouter()
    # plan_execute agent (used when intent is plan_execute)
    app.state.plan_execute_agent = _MockAgentLoop(
        answer=json.dumps({"answer": "逐步诊断结果", "plan": ["步骤1", "步骤2"]})
    )
    return TestClient(app)


# =============================================================================
# POST /chat
# =============================================================================
class TestChatEndpoint:
    def test_basic_chat(self, client):
        """基本请求 → 返回 200 + ChatResponse 格式"""
        resp = client.post("/api/v1/chat", json={"query": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "这是测试回答"
        assert "session_id" in data
        assert data["session_id"] == "mock-session-id"
        assert data["total_steps"] == 1
        assert data["total_tokens"] == 50

    def test_chat_with_session_id(self, client):
        """传 session_id → 复用同一个会话"""
        resp1 = client.post(
            "/api/v1/chat",
            json={"query": "问题1", "session_id": "my-session"},
        )
        assert resp1.status_code == 200
        sid1 = resp1.json()["session_id"]
        assert sid1 == "my-session"

        resp2 = client.post(
            "/api/v1/chat",
            json={"query": "问题2", "session_id": "my-session"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["session_id"] == "my-session"

    def test_empty_query_rejected(self, client):
        """空 query → 400 (pydantic 校验 min_length=1)"""
        resp = client.post("/api/v1/chat", json={"query": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_query_too_long_rejected(self, client):
        """超长 query → 400"""
        resp = client.post("/api/v1/chat", json={"query": "a" * 2001})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_missing_query_rejected(self, client):
        """缺少必填字段 → 400"""
        resp = client.post("/api/v1/chat", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_response_format(self, client):
        """返回的 JSON 结构完整"""
        resp = client.post("/api/v1/chat", json={"query": "测试"})
        data = resp.json()
        assert set(data.keys()) == {"answer", "session_id", "total_steps", "total_tokens"}
        assert isinstance(data["answer"], str)
        assert isinstance(data["session_id"], str)
        assert isinstance(data["total_steps"], int)
        assert isinstance(data["total_tokens"], int)


# =============================================================================
# POST /chat/stream
# =============================================================================
class TestChatStreamEndpoint:
    def test_stream_basic(self, client):
        """SSE 流式请求 → 返回 text/event-stream"""
        with client.stream("POST", "/api/v1/chat/stream", json={"query": "你好"}) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line and line.startswith("data: "):
                    payload = line[6:]  # 去掉 "data: " 前缀
                    events.append(json.loads(payload))

        assert len(events) > 0
        # 第一个事件应该是 start
        assert events[0]["event"] == "start"
        assert events[0]["session_id"] == "mock-session-id"

    def test_stream_contains_done_event(self, client):
        """stream 最终有 done 事件"""
        with client.stream("POST", "/api/v1/chat/stream", json={"query": "测试流式"}) as resp:
            events = []
            for line in resp.iter_lines():
                if line and line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1

    def test_stream_empty_query_rejected(self, client):
        """空 query → 400"""
        resp = client.post("/api/v1/chat/stream", json={"query": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"
