from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import FlagEmbedding
import httpx
import pytest
import sentence_transformers
import tiktoken
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import BaseAppException
from middleware.auth import AuthMiddleware
from middleware.request_id import RequestIDMiddleware

# SessionManager 在模块导入时初始化 tiktoken 编码器。集成测试不验证 tokenizer，
# 因此只在导入真实路由时替换编码器，避免测试依赖外部下载。
with (
    patch.object(tiktoken, "get_encoding", return_value=MagicMock()),
    patch.object(sentence_transformers, "SentenceTransformer", return_value=MagicMock()),
    patch.object(FlagEmbedding, "FlagReranker", return_value=MagicMock()),
):
    from agent.llm.session import SessionContext
    from api.chat import chat_router
    from api.session import session_router


USERS_BY_TOKEN = {
    "token-a": {"id": 101, "username": "customer-a", "role": "customer"},
    "token-b": {"id": 202, "username": "customer-b", "role": "customer"},
}


def make_session(session_id: str, message: str) -> SessionContext:
    return SessionContext(
        session_id=session_id,
        title=f"title-{session_id}",
        messages=[{"role": "user", "content": message}],
        created_at=1_700_000_000.0,
        last_active=1_700_000_100.0,
    )


def make_app() -> tuple[FastAPI, MagicMock, MagicMock]:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)

    session_a = make_session("session-a", "private message from customer A")
    session_b = make_session("session-b", "private message from customer B")

    session = MagicMock()

    async def get_session(session_id: str, user_id: int):
        if session_id == "session-a" and user_id == 101:
            return session_a
        if session_id == "session-b" and user_id == 202:
            return session_b
        return None

    async def get_or_create_session(session_id: str | None, user_id: int):
        if session_id is None:
            return session_a
        return await get_session(session_id, user_id)

    session.get = AsyncMock(side_effect=get_session)
    session.get_or_create = AsyncMock(side_effect=get_or_create_session)
    session.resolve = AsyncMock(side_effect=lambda query, session_id, user_id: query)
    session.add_turn = AsyncMock()
    session.add_turn_simple = AsyncMock()

    async def delete_session(session_id: str, user_id: int) -> bool:
        return session_id == "session-a" and user_id == 101

    async def list_sessions(user_id: int) -> list[dict]:
        if user_id == 101:
            return [
                {
                    "session_id": "session-a",
                    "title": "title-session-a",
                    "created_at": 1_700_000_000.0,
                    "last_active": 1_700_000_100.0,
                    "message_count": 1,
                }
            ]
        if user_id == 202:
            return [
                {
                    "session_id": "session-b",
                    "title": "title-session-b",
                    "created_at": 1_700_000_000.0,
                    "last_active": 1_700_000_100.0,
                    "message_count": 1,
                }
            ]
        return []

    session.delete = AsyncMock(side_effect=delete_session)
    session.list_sessions = AsyncMock(side_effect=list_sessions)

    agent = MagicMock()
    agent.run = AsyncMock()
    agent.run_stream = AsyncMock()

    intent_router = MagicMock()
    intent_router.route = AsyncMock(return_value=SimpleNamespace(target="agent"))

    app.state.session = session
    app.state.agent = agent
    app.state.intent_router = intent_router
    app.state.plan_execute_agent = MagicMock()
    app.include_router(session_router)
    app.include_router(chat_router)

    # 后添加的 RequestIDMiddleware 位于外层，真实执行顺序为：
    # RequestIDMiddleware → AuthMiddleware → API 路由。
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app, session, agent


async def fake_verify_token(token: str):
    return USERS_BY_TOKEN[token], "external"


async def request(
    app: FastAPI,
    token: str,
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(kwargs.pop("headers", {}))
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(side_effect=fake_verify_token),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, headers=headers, **kwargs)


def assert_resource_not_available(response: httpx.Response) -> None:
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_customer_can_read_own_session():
    app, session, _ = make_app()

    response = await request(app, "token-a", "GET", "/api/v1/sessions/session-a")

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "session-a"
    assert body["messages"][0]["content"] == "private message from customer A"
    session.get.assert_awaited_once_with("session-a", 101)


@pytest.mark.asyncio
async def test_customer_cannot_read_another_customers_session():
    app, session, _ = make_app()

    response = await request(app, "token-b", "GET", "/api/v1/sessions/session-a")

    assert_resource_not_available(response)
    assert "private message from customer A" not in response.text
    assert "属于用户" not in response.text
    assert session.get.await_args.args == ("session-a", 202)


@pytest.mark.asyncio
async def test_chat_rejects_another_customers_session_before_agent_call():
    app, session, agent = make_app()

    response = await request(
        app,
        "token-a",
        "POST",
        "/api/v1/chat",
        json={"query": "继续刚才的问题", "session_id": "session-b"},
    )

    assert_resource_not_available(response)
    assert "private message from customer B" not in response.text
    session.get_or_create.assert_awaited_once_with("session-b", 101)
    session.resolve.assert_not_awaited()
    agent.run.assert_not_awaited()
    agent.run_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_stream_rejects_another_customers_session_before_streaming():
    app, session, agent = make_app()

    response = await request(
        app,
        "token-a",
        "POST",
        "/api/v1/chat/stream",
        json={"query": "继续刚才的问题", "session_id": "session-b"},
    )

    assert_resource_not_available(response)
    assert "data:" not in response.text
    session.get_or_create.assert_awaited_once_with("session-b", 101)
    session.resolve.assert_not_awaited()
    session.add_turn.assert_not_awaited()
    agent.run_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_can_use_own_chat_stream():
    app, session, agent = make_app()

    async def fake_stream(*args, **kwargs):
        yield {"event": "start"}
        yield {"event": "done", "answer": "mock answer", "total_steps": 1}

    agent.run_stream = MagicMock(side_effect=fake_stream)

    response = await request(
        app,
        "token-a",
        "POST",
        "/api/v1/chat/stream",
        json={"query": "继续刚才的问题", "session_id": "session-a"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"event": "start"' in response.text
    assert '"event": "done"' in response.text
    assert '"answer": "mock answer"' in response.text
    session.get_or_create.assert_awaited_once_with("session-a", 101)
    session.resolve.assert_awaited_once_with("继续刚才的问题", "session-a", 101)
    agent.run_stream.assert_called_once()
    session.add_turn.assert_awaited_once()
    add_turn_args = session.add_turn.await_args.args
    assert add_turn_args[0] == "session-a"
    assert add_turn_args[1] == 101


@pytest.mark.asyncio
async def test_customer_can_delete_own_session_but_not_anothers():
    app, session, _ = make_app()

    own_response = await request(app, "token-a", "DELETE", "/api/v1/sessions/session-a")
    other_response = await request(app, "token-b", "DELETE", "/api/v1/sessions/session-a")

    assert own_response.status_code == 200
    assert own_response.json() == {"ok": True}
    assert_resource_not_available(other_response)
    assert session.delete.await_args_list == [
        call("session-a", 101),
        call("session-a", 202),
    ]


@pytest.mark.asyncio
async def test_session_list_is_scoped_to_current_customer():
    app, session, _ = make_app()

    response = await request(app, "token-a", "GET", "/api/v1/sessions")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["session_id"] for item in body["sessions"]] == ["session-a"]
    assert "session-b" not in response.text
    session.list_sessions.assert_awaited_once_with(101)
