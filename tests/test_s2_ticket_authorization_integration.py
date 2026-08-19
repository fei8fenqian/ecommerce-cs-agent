from contextlib import ExitStack
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


CUSTOMER_A_TICKET = {
    "ticket_id": "ticket-a",
    "customer_name": "张三",
    "phone": "13800138000",
    "issue": "客户 A 的完整问题描述",
    "urgency": "high",
    "status": "open",
    "created_at": "2026-08-19T10:00:00",
}

UNCLAIMED_TICKET = {
    "ticket_id": "ticket-unclaimed",
    "urgency": "medium",
    "status": "open",
    "created_at": "2026-08-19T10:01:00",
    "assigned_agent_id": None,
}

AGENT_A_TICKET = {
    "ticket_id": "ticket-owned-by-agent-a",
    "customer_name": "李四",
    "phone": "13900139000",
    "issue": "客服 A 已认领的完整问题描述",
    "urgency": "urgent",
    "status": "processing",
    "created_at": "2026-08-19T10:02:00",
    "assigned_agent_id": 303,
}

AGENT_B_TICKET = {
    "ticket_id": "ticket-owned-by-agent-b",
    "customer_name": "王五",
    "phone": "13700137000",
    "issue": "客服 B 已认领的完整问题描述",
    "urgency": "low",
    "status": "processing",
    "created_at": "2026-08-19T10:03:00",
    "assigned_agent_id": 404,
}


async def fake_verify_token(token: str):
    user = USERS_BY_TOKEN[token]
    user_type = "internal" if user["role"] == "agent" else "external"
    return user, user_type


def make_app() -> FastAPI:
    init_casbin()
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.include_router(ticket_router)

    # 后添加的 RequestIDMiddleware 位于外层：
    # RequestIDMiddleware → AuthMiddleware → ticket_router。
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


def make_store_mocks() -> dict[str, AsyncMock]:
    async def get_customer_ticket(ticket_id: str, user_id: int):
        if ticket_id == "ticket-a" and user_id == 101:
            return CUSTOMER_A_TICKET
        return None

    async def list_customer_tickets(user_id: int, status: str | None = None):
        if user_id == 101:
            return [
                {
                    "ticket_id": "ticket-a",
                    "customer_name": "张三",
                    "urgency": "high",
                    "status": "open",
                    "created_at": "2026-08-19T10:00:00",
                }
            ]
        if user_id == 202:
            return [
                {
                    "ticket_id": "ticket-b",
                    "customer_name": "赵六",
                    "urgency": "low",
                    "status": "closed",
                    "created_at": "2026-08-19T10:04:00",
                }
            ]
        return []

    async def get_agent_ticket(ticket_id: str, user_id: int):
        if ticket_id == "ticket-unclaimed":
            return UNCLAIMED_TICKET
        if ticket_id == "ticket-owned-by-agent-a" and user_id == 303:
            return AGENT_A_TICKET
        if ticket_id == "ticket-owned-by-agent-b" and user_id == 404:
            return AGENT_B_TICKET
        return None

    async def list_agent_tickets(agent_id: int, status: str | None = None):
        if agent_id == 303:
            return [UNCLAIMED_TICKET, AGENT_A_TICKET]
        return []

    async def claim_ticket(ticket_id: str, agent_id: int):
        if ticket_id == "ticket-unclaimed" and agent_id == 303:
            return {
                "ticket_id": ticket_id,
                "assigned_agent_id": agent_id,
                "status": "processing",
                "created_at": "2026-08-19T10:01:00",
            }
        return None

    async def update_customer_ticket(ticket_id: str, user_id: int, **kwargs):
        return ticket_id == "ticket-a" and user_id == 101

    async def update_agent_ticket(ticket_id: str, agent_id: int, **kwargs):
        return ticket_id == "ticket-owned-by-agent-a" and agent_id == 303

    return {
        "get_customer_ticket": AsyncMock(side_effect=get_customer_ticket),
        "list_customer_tickets": AsyncMock(side_effect=list_customer_tickets),
        "list_agent_tickets": AsyncMock(side_effect=list_agent_tickets),
        "get_agent_ticket": AsyncMock(side_effect=get_agent_ticket),
        "claim_ticket": AsyncMock(side_effect=claim_ticket),
        "update_customer_ticket": AsyncMock(side_effect=update_customer_ticket),
        "update_agent_ticket": AsyncMock(side_effect=update_agent_ticket),
    }


@pytest.fixture
def ticket_app():
    mocks = make_store_mocks()
    patch_targets = {f"api.tickets.{name}": mock for name, mock in mocks.items()}
    with ExitStack() as stack:
        for target, mock in patch_targets.items():
            stack.enter_context(patch(target, new=mock))
        yield make_app(), mocks


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
async def test_customer_can_read_own_ticket(ticket_app):
    app, mocks = ticket_app

    response = await request(app, "customer-a-token", "GET", "/api/v1/tickets/ticket-a")

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == "ticket-a"
    assert body["customer_name"] == "张三"
    assert body["phone"] == "13800138000"
    assert body["issue"] == "客户 A 的完整问题描述"
    mocks["get_customer_ticket"].assert_awaited_once_with("ticket-a", 101)


@pytest.mark.asyncio
async def test_customer_cannot_read_another_customers_ticket(ticket_app):
    app, mocks = ticket_app

    response = await request(app, "customer-b-token", "GET", "/api/v1/tickets/ticket-a")

    assert_resource_not_available(response)
    assert "张三" not in response.text
    assert "13800138000" not in response.text
    assert "客户 A 的完整问题描述" not in response.text
    mocks["get_customer_ticket"].assert_awaited_once_with("ticket-a", 202)


@pytest.mark.asyncio
async def test_customer_ticket_list_is_scoped_to_current_customer(ticket_app):
    app, mocks = ticket_app

    response = await request(app, "customer-a-token", "GET", "/api/v1/tickets")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [ticket["ticket_id"] for ticket in body["tickets"]] == ["ticket-a"]
    assert "ticket-b" not in response.text
    mocks["list_customer_tickets"].assert_awaited_once_with(101, None)


@pytest.mark.asyncio
async def test_customer_cannot_update_another_customers_ticket(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "customer-b-token",
        "PATCH",
        "/api/v1/tickets/ticket-a",
        json={"status": "closed"},
    )

    assert_resource_not_available(response)
    mocks["update_customer_ticket"].assert_awaited_once_with(
        "ticket-a",
        202,
        status="closed",
        urgency=None,
    )


@pytest.mark.asyncio
async def test_customer_cannot_claim_ticket(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "customer-a-token",
        "POST",
        "/api/v1/tickets/ticket-unclaimed/claim",
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    mocks["claim_ticket"].assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_sees_only_summary_for_unclaimed_ticket(ticket_app):
    app, _ = ticket_app

    response = await request(
        app,
        "agent-a-token",
        "GET",
        "/api/v1/tickets/ticket-unclaimed",
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket_id", "urgency", "status", "created_at"}
    assert body["ticket_id"] == "ticket-unclaimed"
    assert "张三" not in response.text
    assert "13800138000" not in response.text
    assert "完整问题描述" not in response.text


@pytest.mark.asyncio
async def test_agent_sees_full_details_for_own_ticket(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "agent-a-token",
        "GET",
        "/api/v1/tickets/ticket-owned-by-agent-a",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["customer_name"] == "李四"
    assert body["phone"] == "13900139000"
    assert body["issue"] == "客服 A 已认领的完整问题描述"
    assert body["assigned_agent_id"] == 303
    mocks["get_agent_ticket"].assert_awaited_once_with("ticket-owned-by-agent-a", 303)


@pytest.mark.asyncio
async def test_agent_ticket_list_uses_summary_field_whitelist(ticket_app):
    app, mocks = ticket_app

    response = await request(app, "agent-a-token", "GET", "/api/v1/tickets")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    for ticket in body["tickets"]:
        assert set(ticket) == {"ticket_id", "urgency", "status", "created_at"}
    assert "customer_name" not in response.text
    assert "13900139000" not in response.text
    assert "客服 A 已认领的完整问题描述" not in response.text
    mocks["list_agent_tickets"].assert_awaited_once_with(303, None)


@pytest.mark.asyncio
async def test_agent_cannot_read_ticket_owned_by_another_agent(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "agent-a-token",
        "GET",
        "/api/v1/tickets/ticket-owned-by-agent-b",
    )

    assert_resource_not_available(response)
    assert "客服 B 已认领的完整问题描述" not in response.text
    assert "13700137000" not in response.text
    assert "404" not in response.text
    mocks["get_agent_ticket"].assert_awaited_once_with("ticket-owned-by-agent-b", 303)


@pytest.mark.asyncio
async def test_agent_can_claim_unclaimed_ticket_with_own_agent_id(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "agent-a-token",
        "POST",
        "/api/v1/tickets/ticket-unclaimed/claim",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == "ticket-unclaimed"
    assert body["assigned_agent_id"] == 303
    mocks["claim_ticket"].assert_awaited_once_with("ticket-unclaimed", 303)


@pytest.mark.asyncio
async def test_agent_cannot_update_ticket_owned_by_another_agent(ticket_app):
    app, mocks = ticket_app

    response = await request(
        app,
        "agent-a-token",
        "PATCH",
        "/api/v1/tickets/ticket-owned-by-agent-b",
        json={"status": "closed"},
    )

    assert_resource_not_available(response)
    mocks["update_agent_ticket"].assert_awaited_once_with(
        "ticket-owned-by-agent-b",
        303,
        status="closed",
        urgency=None,
    )
