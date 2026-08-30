"""tests/test_api_chat.py — /chat + /chat/stream 端点测试

用 FastAPI TestClient + mock app state，不依赖真实 LLM/Redis/PG。
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import tiktoken
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.engines.loop import LoopResult
from agent.llm.intent_router import Intent, SupportRequest
from agent.llm.resolve import resolve_pronouns
from agent.tools_registry import ToolResult
from api.chat import (
    ChatRequest,
    _await_support_case_customer,
    _build_ticket_issue,
    _claim_chat_run,
    _entities_from_retrieval,
    _is_confirmed_human_handoff,
    _is_current_chat_run,
    _merge_evidence_context,
    _persist_support_case_progress,
    _record_support_case_facts,
    _resume_pending_case,
    _support_case_needs_customer_turn,
    chat_router,
    chat_stream,
)
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import BaseAppException, DependencyUnavailableError, LLMError
from service.support_case_service import SupportCaseService
from store.support_case_store import SupportCase

# 本文件只测试 HTTP 编排，避免 SessionManager 导入时为了下载 tokenizer
# 访问外网。真实 tokenizer 由 SessionManager/集成环境单独验证。
with patch.object(tiktoken, "get_encoding", return_value=object()):
    from agent.llm.session import SessionContext


# =============================================================================
# Mocks
# =============================================================================
class _MockIntentRouter:
    """总是返回 agent（走 AgentLoop，不调 hybrid_search）"""

    async def route(self, query: str = "", history=None, case_context="", knowledge_context="") -> Intent:
        return Intent(
            target="agent",
            table="",
            query=query,
            confidence=0.95,
        )


class _MockTicketIntentRouter:
    """将测试请求稳定路由到客户售后工单闭环。"""

    async def route(self, query: str = "", history=None, case_context="", knowledge_context="") -> Intent:
        return Intent(target="ticket", query=query, confidence=1.0)


class _MockToolRegistry:
    """记录受控工具调用，不连接真实工单数据库。"""

    def __init__(self):
        self.execute = AsyncMock(
            return_value=ToolResult(
                name="create_ticket",
                status="success",
                data={"ticket_id": "TK-DEMO-001"},
            )
        )


class _ChatRunRedis:
    """只覆盖会话运行令牌测试所需的 Redis 操作。"""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        assert ex > 0
        self.values[key] = value

    async def get(self, key: str) -> bytes | None:
        value = self.values.get(key)
        return value.encode() if value is not None else None


@pytest.mark.asyncio
async def test_later_chat_run_supersedes_earlier_run(monkeypatch):
    """同一会话后发请求必须让旧流失效，而不同 run 不共用令牌。"""
    redis = _ChatRunRedis()
    monkeypatch.setattr("api.chat.get_redis", lambda: redis)

    first_run = await _claim_chat_run("session-a")
    second_run = await _claim_chat_run("session-a")

    assert first_run != second_run
    assert await _is_current_chat_run("session-a", first_run) is False
    assert await _is_current_chat_run("session-a", second_run) is True


class _MockAgentLoop:
    """总是返回固定回答"""

    def __init__(self, answer: str = "Mock 回答"):
        self._answer = answer
        self.last_context = ""

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        self.last_context = context
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


class _MockSupportWorkflow:
    """记录复杂客服请求是否进入独立 Workflow。"""

    def __init__(self, answer: str = "复杂流程回答"):
        self.answer = answer
        self.calls: list[dict] = []

    async def run(
        self,
        query,
        *,
        context="",
        history=None,
        system_prompt_extra="",
        case_context="",
        support_requests=None,
        tool_context=None,
    ):
        self.calls.append(
            {
                "query": query,
                "context": context,
                "history": history,
                "system_prompt_extra": system_prompt_extra,
                "case_context": case_context,
                "support_requests": support_requests,
                "tool_context": tool_context,
            }
        )
        return LoopResult(
            answer=self.answer,
            total_steps=2,
            total_tokens=12,
            total_latency_ms=10.0,
        )


def _support_case_request(service: SupportCaseService):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(support_case_service=service)))


def _support_case_fixture(status: str = "ACTIVE") -> SupportCase:
    now = datetime.now(UTC)
    return SupportCase(
        case_id=uuid4(),
        session_id=UUID("00000000-0000-0000-0000-000000000003"),
        customer_user_id=1,
        status=status,  # type: ignore[arg-type]
        request_stack=[],
        selected_subjects={},
        verified_facts={},
        pending={},
        pending_command={},
        version=1,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_api_persists_normalized_agent_tool_facts_into_case():
    service = SupportCaseService()
    case = _support_case_fixture()
    recorded = SupportCase(**{**case.__dict__, "verified_facts": {"refund_status": "COMPLETED"}})
    service.record_verified_facts = AsyncMock(return_value=recorded)  # type: ignore[method-assign]

    result = await _record_support_case_facts(
        _support_case_request(service),
        case=case,
        loop_result=LoopResult(
            verified_facts={"query_refund_status": {"status": "success"}},
            decision_facts={"refund_status": "COMPLETED"},
        ),
    )

    assert result == recorded
    service.record_verified_facts.assert_awaited_once_with(case, facts={"refund_status": "COMPLETED"})


@pytest.mark.asyncio
async def test_api_uses_workflow_confirmation_boundary_not_llm_risk():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[
            SupportRequest(
                domain="after_sales",
                operation="after_sales_transition",
                risk="read_only",
            )
        ],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_confirmation",
            "next_action": "AWAITING_CONFIRMATION",
        },
    )

    pending_command = service.await_customer.await_args.kwargs["pending_command"]
    pending = service.await_customer.await_args.kwargs["pending"]
    assert pending["kind"] == "customer_confirmation"
    assert pending_command["status"] == "PROPOSED_NOT_EXECUTED"
    assert pending_command["requires_explicit_confirmation"] is True
    assert pending_command["risk"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_api_does_not_propose_command_for_clarification():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="clarify", risk="read_only")],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_customer",
            "next_action": "ASK_CLARIFICATION",
            "next_actor": "CUSTOMER",
        },
    )

    assert service.await_customer.await_args.kwargs["pending"]["kind"] == "customer_clarification"
    assert service.await_customer.await_args.kwargs["pending_command"] == {}


@pytest.mark.asyncio
async def test_api_persists_displayed_customer_choices_in_pending_frame():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status")],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_customer",
            "next_action": "ASK_CHOICE",
            "next_actor": "CUSTOMER",
            "pending_choices": [
                {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                {"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
            ],
        },
    )

    pending = service.await_customer.await_args.kwargs["pending"]
    assert pending["kind"] == "customer_choice"
    assert pending["subject_type"] == "order"
    assert [item["order_id"] for item in pending["choices"]] == ["SOREAL_A6", "SOREAL_A7"]
    assert service.await_customer.await_args.kwargs["pending_command"] == {}


@pytest.mark.asyncio
async def test_api_resolves_pending_choice_without_recreating_case():
    case = _support_case_fixture(status="AWAITING_CUSTOMER")
    case = SupportCase(
        **{
            **case.__dict__,
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                    {"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
                ],
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "selected_subjects": {"order_id": "SOREAL_A7"},
            "pending": {},
            "version": case.version + 1,
        }
    )
    service = SupportCaseService()
    service.select_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]

    resumed = await _resume_pending_case(
        _support_case_request(service),
        case=case,
        raw_query="第二个",
    )

    assert resumed is selected
    assert resumed.case_id == case.case_id
    service.select_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="ordinal",
    )


@pytest.mark.asyncio
async def test_api_keeps_case_awaiting_when_pending_choice_is_ambiguous():
    case = _support_case_fixture(status="AWAITING_CUSTOMER")
    case = SupportCase(
        **{
            **case.__dict__,
            "pending": {
                "kind": "customer_choice",
                "choices": [
                    {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                    {"order_id": "SOREAL_A7", "product_name": "联想笔记本", "amount_cents": 189900},
                ],
            },
        }
    )
    service = SupportCaseService()
    service.select_customer_subject = AsyncMock()  # type: ignore[method-assign]
    service.resume_customer_response = AsyncMock()  # type: ignore[method-assign]

    resumed = await _resume_pending_case(
        _support_case_request(service),
        case=case,
        raw_query="笔记本那个",
    )

    assert resumed is case
    service.select_customer_subject.assert_not_awaited()
    service.resume_customer_response.assert_not_awaited()


def test_api_does_not_treat_staff_block_as_customer_turn():
    assert (
        _support_case_needs_customer_turn(
            Intent(target="agent", requests=[SupportRequest(domain="refund", operation="expected_arrival")]),
            workflow_progress={"goal_status": "blocked", "next_actor": "STAFF"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_api_persists_capability_gap_for_staff_not_customer():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.mark_awaiting_staff = AsyncMock(return_value=case)  # type: ignore[method-assign]

    await _persist_support_case_progress(
        _support_case_request(service),
        case=case,
        intent=Intent(
            target="agent",
            requests=[SupportRequest(domain="refund", operation="expected_arrival")],
        ),
        loop_result=LoopResult(
            answer="当前无法提供可信到账时间。",
            workflow_progress={
                "goal": "expected_arrival",
                "goal_status": "blocked",
                "next_action": "ESCALATE_OR_EXPLAIN",
                "next_actor": "STAFF",
                "reason": "capability_unavailable",
                "unavailable_capabilities": ["query_refund_expected_arrival"],
            },
        ),
    )

    service.mark_awaiting_staff.assert_awaited_once()
    assert service.mark_awaiting_staff.await_args.kwargs["reason"] == "capability_unavailable"


@pytest.mark.asyncio
async def test_api_records_refund_self_service_handoff_as_completed_case_not_refund_success():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.record_verified_facts = AsyncMock(return_value=case)  # type: ignore[method-assign]
    service.complete = AsyncMock(return_value=case)  # type: ignore[method-assign]

    await _persist_support_case_progress(
        _support_case_request(service),
        case=case,
        intent=Intent(target="agent", requests=[SupportRequest(domain="refund", operation="request")]),
        loop_result=LoopResult(
            answer="请在订单页提交退款申请。",
            decision_facts={"refund_entry": "?page=orders&refund_order=SO-1"},
            workflow_progress={
                "goal": "request",
                "goal_status": "resolved",
                "next_action": "SELF_SERVICE_HANDOFF",
                "next_actor": "NONE",
                "resolution_type": "SELF_SERVICE_HANDOFF",
            },
        ),
    )

    service.complete.assert_awaited_once()
    outcome = service.complete.await_args.kwargs["outcome"]
    assert outcome["completion"] == "SELF_SERVICE_HANDOFF"
    assert outcome["resolution_type"] == "SELF_SERVICE_HANDOFF"


def test_product_retrieval_records_current_product_for_next_turn():
    assert _entities_from_retrieval(
        "laptop_products",
        [{"title": "惠普 惠普锐Pro"}],
    ) == {"product": "惠普 惠普锐Pro"}
    assert _entities_from_retrieval("knowledge_chunks", [{"title": "售后政策"}]) == {}


def test_ticket_issue_keeps_recent_customer_context():
    issue = _build_ticket_issue(
        "我哪知道订单号，反正就是最近的那单，我想退了",
        [
            {"role": "user", "content": "刚买的电脑突然开不了机，没摔过也没进水"},
            {"role": "assistant", "content": "请提供订单号"},
        ],
    )

    assert "电脑突然开不了机" in issue
    assert "订单号" in issue
    assert "客户售后诉求" in issue


def test_selected_product_context_is_evidence_not_route_override():
    """详情页商品补充公开事实，但不能覆盖已识别的售后 Goal。"""
    intent = Intent(target="agent", domain="refund", operation="status")
    context = _merge_evidence_context(
        "当前商品详情（优先依据）：\n名称：Pallas II DDR5 6000 32G\n支持 XMP 3.0",
        "[知识来源: refund.md / 退款] 退款状态说明",
    )

    assert intent.target == "agent"
    assert intent.domain == "refund"
    assert "Pallas II DDR5 6000 32G" in context
    assert "知识来源" in context


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

    async def truncate_from(
        self,
        session_id: str,
        owner_user_id: int,
        sequence_no: int,
    ) -> bool:
        ctx = self._sessions.get(session_id)
        if ctx is None or sequence_no < 0:
            return False
        ctx.messages = ctx.messages[:sequence_no]
        ctx.last_entities = {}
        return True


# =============================================================================
# TestClient fixture
# =============================================================================
@pytest.fixture
def client(monkeypatch):
    # HTTP 编排测试不加载 embedding 模型或访问知识库；检索行为由独立 RAG 单测覆盖。
    monkeypatch.setattr("api.chat._pre_route_knowledge_context", AsyncMock(return_value=""))
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
    app.state.registry = _MockToolRegistry()
    # plan_execute agent (used when intent is plan_execute)
    app.state.plan_execute_agent = _MockAgentLoop(
        answer=json.dumps({"answer": "逐步诊断结果", "plan": ["步骤1", "步骤2"]})
    )
    app.state.support_workflow_agent = _MockSupportWorkflow()
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

    @pytest.mark.asyncio
    async def test_plain_chat_replaces_unbound_refund_transaction_claim(self, client):
        """退款语境下没有可信订单事实时，普通出口不能透传模型交易结论。"""
        client.app.state.agent = _MockAgentLoop(answer="我查询到您名下有两笔退款记录，一笔处理中，另一笔已完成。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "戴尔笔记本和 Sony 耳机这两件都有退款"},
            )

        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "请提供或确认具体订单号" in answer
        assert "处理中" not in answer
        assert "已完成" not in answer
        assert "两笔退款记录" not in answer

    @pytest.mark.asyncio
    async def test_llm_failure_returns_503_and_does_not_save_fake_answer(self, client):
        """LLM 失败不能伪装成 200，也不能写入假的 assistant 消息。"""
        agent_run = AsyncMock(
            side_effect=LLMError(
                "provider failure",
                retry_count=1,
                status_code=503,
                last_response="retry_exhausted",
            )
        )
        client.app.state.agent.run = agent_run
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "请查询订单"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert response.json()["error"]["message"] != "服务暂时不可用"
        assert "provider failure" not in response.text
        agent_run.assert_awaited_once()
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_circuit_returns_503_without_fake_chat_result(self, client):
        """熔断后的依赖错误使用统一 503 语义。"""
        agent_run = AsyncMock(side_effect=DependencyUnavailableError("内部依赖详情"))
        client.app.state.agent.run = agent_run
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "你好"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "内部依赖详情" not in response.text
        agent_run.assert_awaited_once()
        client.app.state.session.add_turn.assert_not_awaited()

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

    @pytest.mark.asyncio
    async def test_edit_message_truncates_then_regenerates(self, client):
        """编辑历史消息时，只保留其前文并以新问题重新生成。"""
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            first = await http_client.post(
                "/api/v1/chat",
                json={"query": "旧问题", "session_id": "edit-session"},
            )
            assert first.status_code == 200
            response = await http_client.post(
                "/api/v1/chat",
                json={
                    "query": "新问题",
                    "session_id": "edit-session",
                    "replace_from_sequence": 0,
                },
            )

        assert response.status_code == 200
        messages = client.app.state.session._sessions["edit-session"].messages
        assert messages[0]["content"] == "新问题"
        assert all(message["content"] != "旧问题" for message in messages)

    @pytest.mark.asyncio
    async def test_edit_without_existing_session_returns_404(self, client):
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "新问题", "replace_from_sequence": 0},
            )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_short_confirmation_defaults_to_stock_lookup(self, client):
        """“需要”默认执行上一轮推荐中已提出的库存查询。"""
        session = client.app.state.session
        session._sessions["follow-up-session"] = SessionContext(
            session_id="follow-up-session",
            messages=[
                {
                    "role": "assistant",
                    "content": "首选推荐：微星魔影15（i7/RTX4070）\n需要我帮你对比其他型号或查询库存吗？",
                }
            ],
            last_entities={"product": "微星魔影15"},
        )
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="查询 微星魔影15 的实时库存",
                confidence=1.0,
            )
        )
        client.app.state.intent_router.route = route
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "需要", "session_id": "follow-up-session"},
            )

        assert response.status_code == 200
        route.assert_awaited_once_with(
            "查询 微星魔影15 的实时库存",
            history=session._sessions["follow-up-session"].history,
            knowledge_context="",
        )

    @pytest.mark.asyncio
    async def test_complex_support_route_uses_support_workflow(self, client):
        """多步骤客服请求进入 SupportWorkflow，不再直接调用普通 AgentLoop。"""
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="核对部分发货订单并询问客户选择",
                confidence=1.0,
                domain="order_fulfillment",
                operation="partial_fulfillment",
                state="needs_customer_choice",
                next_step="ASK_CHOICE",
                required_tools=["track_order", "check_stock"],
            )
        )
        client.app.state.intent_router.route = route
        client.app.state.agent.run = AsyncMock(side_effect=AssertionError("不应走普通 AgentLoop"))

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "订单里有一件缺货，另一件有货，怎么处理？"},
            )

        assert response.status_code == 200
        assert response.json()["answer"].startswith("复杂流程回答")
        assert response.json()["total_steps"] == 2
        assert len(client.app.state.support_workflow_agent.calls) == 1
        assert client.app.state.support_workflow_agent.calls[0]["system_prompt_extra"]
        route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_short_reply_resumes_pending_support_case(self, client):
        """“第一个”必须回到待选 Case，而不是被路由成孤立 RAG 问题。"""
        active_case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            customer_user_id=1,
            status="AWAITING_CUSTOMER",
            request_stack=[
                {
                    "domain": "refund",
                    "operation": "refund_request",
                    "next_step": "LOOKUP",
                    "required_tools": ["track_order"],
                    "risk": "customer_confirmation",
                }
            ],
            selected_subjects={},
            verified_facts={"track_order": {"status": "success", "data": {"count": 2}}},
            pending={"kind": "customer_choice", "options_limit": 2},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )
        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        resumed_case = SupportCase(**{**active_case.__dict__, "status": "ACTIVE", "version": 3})
        service.resume_customer_response = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        service.await_customer = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        route = AsyncMock(return_value=Intent(target="rag", query="第一个", confidence=0.9, case_update="continue"))
        client.app.state.intent_router.route = route

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "第一个"})

        assert response.status_code == 200
        assert route.await_args.kwargs["case_context"]
        service.resume_customer_response.assert_awaited_once_with(active_case)
        workflow_call = client.app.state.support_workflow_agent.calls[0]
        assert workflow_call["support_requests"][0]["operation"] == "request"
        assert workflow_call["support_requests"][0]["required_tools"] == ["track_order"]

    @pytest.mark.asyncio
    async def test_human_ticket_is_created_only_after_case_confirmation(self, client):
        active_case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            customer_user_id=1,
            status="AWAITING_CUSTOMER",
            request_stack=[
                {
                    "domain": "human",
                    "operation": "human_handoff",
                    "next_step": "ASK_CLARIFICATION",
                    "required_tools": [],
                    "risk": "staff_approval",
                }
            ],
            selected_subjects={},
            verified_facts={},
            pending={"kind": "customer_confirmation"},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )
        assert _is_confirmed_human_handoff(active_case, "是") is True

        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        resumed_case = SupportCase(**{**active_case.__dict__, "status": "ACTIVE", "version": 3})
        service.resume_customer_response = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        service.mark_awaiting_staff = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        client.app.state.intent_router.route = AsyncMock(
            return_value=Intent(target="rag", query="是", confidence=0.9, case_update="continue")
        )

        transport = httpx.ASGITransport(app=client.app)
        with patch("api.chat.enqueue_human_ticket", new=AsyncMock(return_value=True)) as enqueue:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
                response = await http_client.post("/api/v1/chat", json={"query": "是"})

        assert response.status_code == 200
        assert "TK-DEMO-001" in response.json()["answer"]
        client.app.state.registry.execute.assert_awaited_once()
        service.mark_awaiting_staff.assert_awaited_once()
        enqueue.assert_awaited_once()

    def test_read_only_support_case_can_complete_without_a_customer_turn(self):
        assert (
            _support_case_needs_customer_turn(
                Intent(
                    target="agent",
                    domain="delivery",
                    operation="track_order",
                    next_step="LOOKUP",
                    requests=[
                        SupportRequest(
                            domain="delivery",
                            operation="track_order",
                            next_step="LOOKUP",
                            risk="read_only",
                        )
                    ],
                )
            )
            is False
        )

    def test_resolved_workflow_can_complete_after_pending_reply(self):
        """旧 pending 仅供恢复上下文，不能阻止已解决的本轮工作流结束。"""
        case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            customer_user_id=1,
            status="ACTIVE",
            request_stack=[],
            selected_subjects={},
            verified_facts={},
            pending={"kind": "customer_choice", "options_limit": 2},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )

        assert (
            _support_case_needs_customer_turn(
                Intent(target="agent", domain="delivery", operation="track_order"),
                case,
                {"goal_status": "resolved", "next_action": "ANSWER"},
            )
            is False
        )

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
    @pytest.mark.asyncio
    async def test_stream_refund_follow_up_never_emits_unverified_facts(self, client):
        """跨轮退款上下文必须在任何 customer-visible token 发出前经过同一事实边界。"""
        session = client.app.state.session
        session._sessions["stream-refund-follow-up"] = SessionContext(
            session_id="stream-refund-follow-up",
            messages=[
                {
                    "role": "assistant",
                    "content": "这笔退款目前还在处理中。",
                    "_decision_facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                    "_decision_contexts": [
                        {
                            "subject_type": "order",
                            "subject_id": "SO-STREAM-A3",
                            "provenance": "current",
                            "source": "query_refund_status",
                            "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                        }
                    ],
                }
            ],
        )
        client.app.state.agent = _MockAgentLoop(
            answer="退款申请已提交，目前处于平台审核处理阶段，一般需要 1-7 个工作日。"
        )

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "页面上显示还在处理中", "session_id": "stream-refund-follow-up"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "1-7 个工作日" not in visible_tokens
        assert "平台审核" not in visible_tokens
        assert "根据上一轮系统查询" in visible_tokens

    @pytest.mark.asyncio
    async def test_stream_subjectless_legacy_refund_facts_do_not_support_transaction_claims(self, client):
        """SSE 不能把旧 flat metadata 当作退款事实来源。"""
        session = client.app.state.session
        session._sessions["stream-subjectless-legacy"] = SessionContext(
            session_id="stream-subjectless-legacy",
            messages=[
                {
                    "role": "assistant",
                    "content": "上一轮查询结果。",
                    "_decision_facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                }
            ],
        )
        client.app.state.agent = _MockAgentLoop(answer="根据上一轮查询，这笔退款目前还在处理中，退款金额为 ¥8999.00。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "页面上显示还在处理中", "session_id": "stream-subjectless-legacy"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "处理中" not in visible_tokens
        assert "8999" not in visible_tokens
        assert "请提供或确认具体订单号" in visible_tokens

    @pytest.mark.asyncio
    async def test_stream_unbound_refund_transaction_claim_is_never_emitted(self, client):
        """首轮无 subject 的多笔退款结论不能在 SSE token 中泄露。"""
        client.app.state.agent = _MockAgentLoop(answer="我查询到您名下有两笔退款记录，一笔处理中，另一笔已完成。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "戴尔笔记本和 Sony 耳机这两件都有退款"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "请提供或确认具体订单号" in visible_tokens
        assert "处理中" not in visible_tokens
        assert "已完成" not in visible_tokens
        assert "两笔退款记录" not in visible_tokens

    @pytest.mark.asyncio
    async def test_refund_request_offers_self_service_before_creating_ticket(self, client):
        """普通退款申请先进入本人订单页，不应直接制造人工工单。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "我要申请退款"})

        assert response.status_code == 200
        assert '"event": "tool_call"' not in response.text
        assert '"name": "create_ticket"' not in response.text
        assert "?page=orders" in response.text
        assert "需要人工" in response.text
        assert '"event": "done"' in response.text
        client.app.state.registry.execute.assert_not_awaited()
        client.app.state.agent.run_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_progress_update_does_not_create_a_second_ticket(self, client):
        """“已经退了”是退款进度说明，不能被误判为新的售后工单。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "已经退了"})

        assert response.status_code == 200
        assert '"name": "create_ticket"' not in response.text
        assert "不用重复创建工单" in response.text
        assert "?page=orders" in response.text
        client.app.state.registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_human_request_is_guided_before_ticket_creation(self, client):
        """第一次“退款并转人工”先给自助入口，避免一句话直接制造人工任务。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "我要退款，请转人工"})

        assert response.status_code == 200
        assert '"name": "create_ticket"' not in response.text
        assert "仍需人工" in response.text
        client.app.state.registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_human_request_after_guidance_can_create_ticket(self, client):
        """客户已收到入口仍坚持人工时，才允许创建人工售后任务。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        session = client.app.state.session
        session._sessions["refund-human-session"] = SessionContext(
            session_id="refund-human-session",
            messages=[
                {
                    "role": "assistant",
                    "content": "[前往我的订单申请退款](?page=orders)",
                }
            ],
        )

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "退款页面我不想弄，转人工", "session_id": "refund-human-session"},
            )

        assert response.status_code == 200
        assert '"name": "create_ticket"' in response.text
        client.app.state.registry.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_customer_ticket_result_is_not_hidden_when_session_history_save_fails(self, client):
        """工单已落库时，会话存档失败不能把成功业务动作伪装成 AI 服务故障。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.session.add_turn_simple = AsyncMock(side_effect=TypeError("persistence failed"))

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "电脑冒烟了"})

        assert response.status_code == 200
        assert '"event": "done"' in response.text
        assert "TK-DEMO-001" in response.text
        assert "DEPENDENCY_UNAVAILABLE" not in response.text

    @pytest.mark.asyncio
    async def test_stream_short_confirmation_defaults_to_stock_lookup(self, client):
        """短确认在流式路径也会直接进入库存查询。"""
        session = client.app.state.session
        session._sessions["stream-follow-up"] = SessionContext(
            session_id="stream-follow-up",
            messages=[
                {
                    "role": "assistant",
                    "content": "首选推荐：微星魔影15（i7/RTX4070）\n需要我帮你对比其他型号或查询库存吗？",
                }
            ],
            last_entities={"product": "微星魔影15"},
        )
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="查询 微星魔影15 的实时库存",
                confidence=1.0,
            )
        )
        client.app.state.intent_router.route = route

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "需要", "session_id": "stream-follow-up"},
            )

        assert response.status_code == 200
        route.assert_awaited_once_with(
            "查询 微星魔影15 的实时库存",
            history=session._sessions["stream-follow-up"].history,
        )

    @pytest.mark.asyncio
    async def test_stream_complex_support_route_uses_support_workflow(self, client):
        """流式入口与普通入口使用同一复杂客服 Workflow。"""
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="核对配送和部分发货状态",
                confidence=1.0,
                domain="order_fulfillment",
                operation="partial_fulfillment",
                state="needs_customer_choice",
                next_step="ASK_CHOICE",
                required_tools=["track_order", "check_stock"],
            )
        )
        client.app.state.intent_router.route = route
        client.app.state.agent.run_stream = AsyncMock(side_effect=AssertionError("不应走普通流式 AgentLoop"))

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "一件缺货一件有货，能不能先发有货的？"},
            )

        assert response.status_code == 200
        assert '"event": "done"' in response.text
        assert "复杂流程回答" in response.text
        assert len(client.app.state.support_workflow_agent.calls) == 1

    @pytest.mark.asyncio
    async def test_stream_cancellation_does_not_continue_or_save(self, client):
        from starlette.requests import Request

        blocked = asyncio.Event()
        continued = False

        async def blocked_stream(*args, **kwargs):
            nonlocal continued
            yield {"event": "start"}
            yield {"event": "token", "content": "部分回答"}
            await blocked.wait()
            continued = True
            yield {"event": "done", "answer": "不应发送", "total_steps": 1}

        client.app.state.agent.run_stream = MagicMock(side_effect=blocked_stream)
        client.app.state.session.add_turn = AsyncMock()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "raw_path": b"/api/v1/chat/stream",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 50000),
            "app": client.app,
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        request.state.user = {"id": 1, "username": "test-user", "role": "customer"}
        stream_response = await chat_stream(ChatRequest(query="你好"), request)
        body_iterator = stream_response.body_iterator

        await anext(body_iterator)
        await anext(body_iterator)
        await anext(body_iterator)
        cancelled = asyncio.create_task(anext(body_iterator))
        await asyncio.sleep(0)
        cancelled.cancel()

        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await body_iterator.aclose()

        assert continued is False
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_setup_llm_failure_returns_503_before_sse(self, client):
        client.app.state.intent_router.route = AsyncMock(side_effect=LLMError("provider failure", status_code=503))

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "data:" not in response.text
        assert "provider failure" not in response.text

    @pytest.mark.asyncio
    async def test_stream_failure_after_start_sends_error_without_done_or_save(self, client):
        async def broken_stream(*args, **kwargs):
            yield {"event": "start"}
            yield {"event": "token", "content": "部分回答"}
            raise DependencyUnavailableError("provider secret")

        client.app.state.agent.run_stream = MagicMock(side_effect=broken_stream)
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 200
        assert '"event": "error"' in response.text
        assert '"code": "DEPENDENCY_UNAVAILABLE"' in response.text
        assert '"request_id": "-"' in response.text
        assert '"event": "done"' not in response.text
        assert "provider secret" not in response.text
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_success_sends_start_token_done_and_saves(self, client):
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"event": "start"' in response.text
        assert '"event": "token"' in response.text
        assert '"event": "done"' in response.text
        client.app.state.session.add_turn.assert_awaited_once()

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
