"""客服知识回复草稿 API 的最小授权与故障语义测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.llm.llm_client import LLMResponse
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from api.tickets import ticket_router
from exceptions import BaseAppException, LLMError
from infra.casbin_enforcer import init_casbin
from middleware.auth import AuthMiddleware
from middleware.request_id import RequestIDMiddleware

AGENT_A = {"id": 303, "username": "agent-a", "role": "agent"}
AGENT_B = {"id": 404, "username": "agent-b", "role": "agent"}
OWNED_TICKET = {
    "ticket_id": "ticket-owned-by-agent-a",
    "assigned_agent_id": 303,
    "customer_name": "张三",
    "phone": "13800138000",
    "issue": "设备无法开机，应该如何处理？",
    "urgency": "high",
    "status": "processing",
    "created_at": "2026-08-24T10:00:00",
}
KNOWLEDGE = [
    {
        "id": "knowledge-boot-v1",
        "title": "设备无法开机排查指南",
        "source": "knowledge:boot-guide-v1",
        "content": "建议先确认电源连接，再长按电源键十秒后重新开机。",
    }
]


async def fake_verify_token(token: str):
    return ({"agent-a-token": AGENT_A, "agent-b-token": AGENT_B}[token], "internal")


def make_app(llm_client: object) -> FastAPI:
    """创建包含真实中间件、路由和异常处理器的测试应用。"""
    init_casbin()
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.include_router(ticket_router)
    app.state.llm_client = llm_client
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


async def post_draft(app: FastAPI, token: str) -> httpx.Response:
    """通过真实认证中间件请求草稿接口。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with patch("middleware.auth.verify_token", new=AsyncMock(side_effect=fake_verify_token)):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/v1/agent/support-reply-drafts/ticket-owned-by-agent-a",
                headers={"Authorization": f"Bearer {token}"},
            )


@pytest.mark.asyncio
async def test_assigned_agent_receives_draft_and_knowledge_references() -> None:
    llm = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content="请您先检查电源连接后重试开机。")))
    app = make_app(llm)

    with (
        patch("api.tickets.get_agent_ticket", new=AsyncMock(return_value=OWNED_TICKET)),
        patch("api.tickets.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
    ):
        response = await post_draft(app, "agent-a-token")

    assert response.status_code == 200
    body = response.json()
    assert body["draft"] == "请您先检查电源连接后重试开机。"
    assert body["knowledge_references"] == [{"title": "设备无法开机排查指南", "reference": "knowledge:boot-guide-v1"}]
    assert body["needs_human_follow_up"] is False
    prompt = str(llm.chat.await_args.args[0])
    assert "张三" not in prompt
    assert "13800138000" not in prompt


@pytest.mark.asyncio
async def test_other_agent_gets_404_without_rag_or_llm() -> None:
    llm = SimpleNamespace(chat=AsyncMock())
    app = make_app(llm)
    ticket_lookup = AsyncMock(return_value=None)
    search = AsyncMock()

    with (
        patch("api.tickets.get_agent_ticket", new=ticket_lookup),
        patch("api.tickets.hybrid_search", new=search),
    ):
        response = await post_draft(app, "agent-b-token")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    ticket_lookup.assert_awaited_once_with("ticket-owned-by-agent-a", 404)
    search.assert_not_awaited()
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_knowledge_returns_safe_human_follow_up() -> None:
    llm = SimpleNamespace(chat=AsyncMock())
    app = make_app(llm)

    with (
        patch("api.tickets.get_agent_ticket", new=AsyncMock(return_value=OWNED_TICKET)),
        patch("api.tickets.hybrid_search", new=AsyncMock(return_value=[])),
    ):
        response = await post_draft(app, "agent-a-token")

    assert response.status_code == 200
    body = response.json()
    assert "需要人工补充" in body["draft"]
    assert body["knowledge_references"] == []
    assert body["needs_human_follow_up"] is True
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_issue_pii_is_redacted_before_rag_llm_and_response() -> None:
    ticket_with_pii = {
        **OWNED_TICKET,
        "issue": "请联系 13800138000 或 customer@example.com 处理无法开机问题。",
    }
    llm = SimpleNamespace(chat=AsyncMock(return_value=LLMResponse(content="请先检查电源连接。")))
    app = make_app(llm)
    search = AsyncMock(return_value=KNOWLEDGE)

    with (
        patch("api.tickets.get_agent_ticket", new=AsyncMock(return_value=ticket_with_pii)),
        patch("api.tickets.hybrid_search", new=search),
    ):
        response = await post_draft(app, "agent-a-token")

    assert response.status_code == 200
    search_call = search.await_args
    assert search_call is not None
    rag_query = search_call.args[0]
    llm_call = llm.chat.await_args
    assert llm_call is not None
    prompt = str(llm_call.args[0])
    for pii in ("13800138000", "customer@example.com"):
        assert pii not in rag_query
        assert pii not in prompt
        assert pii not in response.text


@pytest.mark.asyncio
async def test_llm_failure_keeps_unified_503_semantics() -> None:
    llm = SimpleNamespace(chat=AsyncMock(side_effect=LLMError("provider secret", status_code=503)))
    app = make_app(llm)

    with (
        patch("api.tickets.get_agent_ticket", new=AsyncMock(return_value=OWNED_TICKET)),
        patch("api.tickets.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
    ):
        response = await post_draft(app, "agent-a-token")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "provider secret" not in response.text
