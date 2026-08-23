"""客户直接创建工单 API 测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.tickets import ticket_router


def make_app(user: dict) -> FastAPI:
    """创建带最小已认证身份的工单 API 测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(ticket_router)
    return app


async def create(app: FastAPI, issue: str) -> httpx.Response:
    """以异步 ASGI transport 创建工单。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/tickets", json={"issue": issue})


@pytest.mark.asyncio
async def test_customer_can_create_ticket_bound_to_their_identity():
    """API 不接受客户 ID 或 urgency，身份范围由服务端上下文派生。"""
    created = {
        "ticket_id": "TKABC123",
        "customer_name": "",
        "phone": "",
        "issue": "设备无法开机",
        "urgency": "medium",
        "status": "待处理",
        "created_at": "2026-08-24T10:00:00",
    }
    with (
        patch("api.tickets.create_ticket", new=AsyncMock()) as mocked_create,
        patch("api.tickets.get_customer_ticket", new=AsyncMock(return_value=created)),
    ):
        response = await create(make_app({"id": 101, "role": "customer"}), "设备无法开机")

    assert response.status_code == 201
    assert response.json()["ticket_id"] == "TKABC123"
    assert mocked_create.await_args.kwargs["customer_user_id"] == 101
    assert mocked_create.await_args.kwargs["urgency"] == "medium"


@pytest.mark.asyncio
async def test_internal_user_cannot_create_customer_ticket():
    """客服不能通过客户入口伪造客户工单。"""
    response = await create(make_app({"id": 201, "role": "agent"}), "请帮我创建")

    assert response.status_code == 403
