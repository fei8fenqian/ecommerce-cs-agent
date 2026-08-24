"""运营履约接口的权限和状态迁移边界测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request

from api.fulfillments import fulfillment_router
from service.fulfillment_service import FulfillmentUnavailableError
from store.checkout_store import OperatorFulfillment


def _app(role: str) -> FastAPI:
    """创建只注入当前角色的最小 API 测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = {"id": 101, "role": role}
        return await call_next(request)

    app.include_router(fulfillment_router)
    return app


def _fulfillment(status: str = "PENDING_FULFILLMENT") -> OperatorFulfillment:
    """构造不含真实客户信息的履约记录。"""
    return OperatorFulfillment(
        order_no="SO202608240001",
        product_name="测试笔记本",
        quantity=1,
        status=status,
        carrier="顺丰速运" if status == "SHIPPED" else None,
        tracking_number="SF1234567890" if status == "SHIPPED" else None,
        created_at="2026-08-24T12:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_operator_can_view_application_fulfillment_queue():
    """运营只能读取应用自有履约记录。"""
    transport = httpx.ASGITransport(app=_app("operator"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.fulfillments.list_operator_fulfillments", new=AsyncMock(return_value=[_fulfillment()])):
            response = await client.get("/api/v1/fulfillments")

    assert response.status_code == 200
    assert response.json()["fulfillments"][0]["status"] == "PENDING_FULFILLMENT"


@pytest.mark.asyncio
async def test_customer_cannot_register_shipment():
    """客户不能将自己的订单直接改成已发货。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/fulfillments/SO202608240001/ship",
            json={"carrier": "顺丰速运", "tracking_number": "SF1234567890"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_operator_can_ship_once_and_duplicate_is_conflict():
    """发货使用确定性 PENDING_FULFILLMENT → SHIPPED 迁移，不允许重复推进。"""
    transport = httpx.ASGITransport(app=_app("operator"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.fulfillments.ship_order", new=AsyncMock(return_value=_fulfillment("SHIPPED"))) as ship_order:
            response = await client.post(
                "/api/v1/fulfillments/SO202608240001/ship",
                json={"carrier": "顺丰速运", "tracking_number": "SF1234567890"},
            )
        with patch(
            "api.fulfillments.ship_order",
            new=AsyncMock(side_effect=FulfillmentUnavailableError()),
        ):
            duplicate = await client.post(
                "/api/v1/fulfillments/SO202608240001/ship",
                json={"carrier": "顺丰速运", "tracking_number": "SF1234567890"},
            )

    assert response.status_code == 200
    assert response.json()["status"] == "SHIPPED"
    ship_order.assert_awaited_once()
    assert duplicate.status_code == 409
