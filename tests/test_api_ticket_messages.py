"""工单消息 API 的最小范围授权测试。"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from api.tickets import ticket_router
from exceptions import BaseAppException
from infra.casbin_enforcer import init_casbin
from middleware.auth import AuthMiddleware
from middleware.request_id import RequestIDMiddleware

USERS_BY_TOKEN = {
    "customer-a-token": {"id": 101, "username": "customer-a", "role": "customer"},
    "customer-b-token": {"id": 202, "username": "customer-b", "role": "customer"},
    "agent-a-token": {"id": 303, "username": "agent-a", "role": "agent"},
    "agent-b-token": {"id": 404, "username": "agent-b", "role": "agent"},
}
MESSAGES = [
    {
        "message_id": 1,
        "author_role": "customer",
        "content": "设备无法开机。",
        "ai_assisted": False,
        "created_at": "2026-08-24T10:00:00+00:00",
    },
    {
        "message_id": 2,
        "author_role": "agent",
        "content": "请先检查电源连接。",
        "ai_assisted": True,
        "created_at": "2026-08-24T10:05:00+00:00",
    },
]


async def fake_verify_token(token: str):
    """返回与现有 AuthMiddleware 一致的最小内部/外部身份信息。"""
    user = USERS_BY_TOKEN[token]
    return user, "internal" if user["role"] == "agent" else "external"


def make_app() -> FastAPI:
    """创建使用真实认证、请求 ID 和统一错误处理的测试应用。"""
    init_casbin()
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.include_router(ticket_router)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


async def request(app: FastAPI, token: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """以指定用户身份通过真实 AuthMiddleware 发起请求。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with patch("middleware.auth.verify_token", new=AsyncMock(side_effect=fake_verify_token)):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )


@pytest.mark.asyncio
async def test_customer_can_read_own_ticket_messages() -> None:
    app = make_app()
    messages = AsyncMock(return_value=MESSAGES)

    with patch("api.tickets.list_customer_ticket_messages", new=messages):
        response = await request(app, "customer-a-token", "GET", "/api/v1/tickets/ticket-a/messages")

    assert response.status_code == 200
    assert response.json()["messages"] == MESSAGES
    messages.assert_awaited_once_with("ticket-a", 101)


@pytest.mark.asyncio
async def test_customer_cannot_read_another_customers_ticket_messages() -> None:
    app = make_app()
    messages = AsyncMock(return_value=None)

    with patch("api.tickets.list_customer_ticket_messages", new=messages):
        response = await request(app, "customer-b-token", "GET", "/api/v1/tickets/ticket-a/messages")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    messages.assert_awaited_once_with("ticket-a", 202)


@pytest.mark.asyncio
async def test_assigned_agent_can_send_ai_assisted_reply() -> None:
    app = make_app()
    created = AsyncMock(return_value=MESSAGES[1])

    with patch("api.tickets.create_agent_ticket_message", new=created):
        response = await request(
            app,
            "agent-a-token",
            "POST",
            "/api/v1/tickets/ticket-a/messages",
            json={"content": "请先检查电源连接。", "ai_assisted": True},
        )

    assert response.status_code == 200
    assert response.json() == MESSAGES[1]
    created.assert_awaited_once_with("ticket-a", 303, "请先检查电源连接。", True)


@pytest.mark.asyncio
async def test_other_agent_cannot_send_ticket_reply() -> None:
    app = make_app()
    created = AsyncMock(return_value=None)

    with patch("api.tickets.create_agent_ticket_message", new=created):
        response = await request(
            app,
            "agent-b-token",
            "POST",
            "/api/v1/tickets/ticket-a/messages",
            json={"content": "越权回复"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    created.assert_awaited_once_with("ticket-a", 404, "越权回复", False)


@pytest.mark.asyncio
async def test_customer_can_add_follow_up_to_own_ticket() -> None:
    """客户追问写入自己的工单，并由 Store 决定是否重新进入 AI 队列。"""
    app = make_app()
    follow_up = {
        "message_id": 3,
        "author_role": "customer",
        "content": "还是无法开机。",
        "ai_assisted": False,
        "created_at": "2026-08-24T10:10:00+00:00",
    }
    created = AsyncMock(return_value=follow_up)

    with patch("api.tickets.create_customer_ticket_message", new=created):
        response = await request(
            app,
            "customer-a-token",
            "POST",
            "/api/v1/tickets/ticket-a/customer-messages",
            json={"content": "还是无法开机。"},
        )

    assert response.status_code == 200
    assert response.json() == follow_up
    created.assert_awaited_once_with("ticket-a", 101, "还是无法开机。")


@pytest.mark.asyncio
async def test_customer_cannot_add_follow_up_to_another_ticket() -> None:
    """越权客户统一拿到安全 404，不泄露工单存在。"""
    app = make_app()
    created = AsyncMock(return_value=None)

    with patch("api.tickets.create_customer_ticket_message", new=created):
        response = await request(
            app,
            "customer-b-token",
            "POST",
            "/api/v1/tickets/ticket-a/customer-messages",
            json={"content": "越权追问"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    created.assert_awaited_once_with("ticket-a", 202, "越权追问")


@pytest.mark.asyncio
async def test_customer_can_confirm_ai_result_and_close_own_ticket() -> None:
    app = make_app()
    closed = AsyncMock(return_value=True)

    with patch("api.tickets.close_customer_ticket", new=closed):
        response = await request(app, "customer-a-token", "POST", "/api/v1/tickets/ticket-a/close")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "已关闭"}
    closed.assert_awaited_once_with("ticket-a", 101)


@pytest.mark.asyncio
async def test_only_assigned_agent_can_close_ticket() -> None:
    app = make_app()
    closed = AsyncMock(return_value=False)

    with patch("api.tickets.close_agent_ticket", new=closed):
        response = await request(app, "agent-b-token", "POST", "/api/v1/tickets/ticket-a/close")

    assert response.status_code == 404
    closed.assert_awaited_once_with("ticket-a", 404)


@pytest.mark.asyncio
async def test_customer_created_ticket_enters_agent_queue_first() -> None:
    """客户从售后页建单也应先交给自主 Agent，不直接落入人工队列。"""
    app = make_app()
    created = AsyncMock()
    fetched = AsyncMock(
        return_value={
            "ticket_id": "TKNEW",
            "customer_name": "",
            "phone": "",
            "issue": "设备无法开机",
            "urgency": "medium",
            "status": "AI待处理",
            "created_at": "2026-08-25T10:00:00+00:00",
        }
    )

    with (
        patch("api.tickets.create_ticket", new=created),
        patch("api.tickets.get_customer_ticket", new=fetched),
    ):
        response = await request(
            app,
            "customer-a-token",
            "POST",
            "/api/v1/tickets",
            json={"issue": "设备无法开机"},
        )

    assert response.status_code == 201
    assert response.json()["status"] == "AI待处理"
    assert created.await_args is not None
    assert created.await_args.kwargs["customer_user_id"] == 101
    assert created.await_args.kwargs["status"] == "AI待处理"


@pytest.mark.asyncio
async def test_ticket_reply_rejects_content_over_4000_characters() -> None:
    app = make_app()
    created = AsyncMock()

    with patch("api.tickets.create_agent_ticket_message", new=created):
        response = await request(
            app,
            "agent-a-token",
            "POST",
            "/api/v1/tickets/ticket-a/messages",
            json={"content": "x" * 4001},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    created.assert_not_awaited()
