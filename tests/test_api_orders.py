"""客户订单 HTTP 入口只读、归属范围测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.orders import order_router


def make_app(user: dict) -> FastAPI:
    """构造携带最小登录身份的 API 测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def set_user(request, call_next):
        request.state.user = user
        return await call_next(request)

    app.include_router(order_router)
    return app


async def get(app: FastAPI, path: str) -> httpx.Response:
    """以异步 ASGI transport 调接口，规避旧同步测试桥接阻塞。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def order() -> dict:
    """返回一个最小订单 DTO，模拟 Store 已过滤资源归属后的结果。"""
    return {
        "order_id": "ORDER-1001",
        "status": "已支付",
        "tracking": {"company": "顺丰", "number": "SF-001"},
        "total_amount": 5999.0,
        "paid_amount": 5999.0,
        "payment_method": "mock",
        "order_date": "2026-08-24",
        "delivered_at": None,
        "items": [{"product_name": "示例笔记本", "brand": "示例", "price": 5999.0, "quantity": 1}],
    }


@pytest.mark.asyncio
async def test_customer_can_list_only_their_orders():
    """客户列表只以服务端身份 ID 作为查询范围。"""
    with patch("api.orders.list_customer_orders", new=AsyncMock(return_value=[order()])) as mocked:
        response = await get(make_app({"id": 101, "role": "customer"}), "/api/v1/orders/my")

    assert response.status_code == 200
    assert response.json()["orders"][0]["order_id"] == "ORDER-1001"
    mocked.assert_awaited_once_with(101, 30)


@pytest.mark.asyncio
async def test_customer_order_detail_returns_404_when_store_has_no_owned_order():
    """不存在和非本人订单都由 Store 返回空列表，HTTP 层不泄露差异。"""
    with patch("api.orders.find_orders", new=AsyncMock(return_value=[])):
        response = await get(make_app({"id": 101, "role": "customer"}), "/api/v1/orders/my/OTHER-ORDER")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_internal_role_cannot_use_customer_order_api():
    """客服不能借客户入口绕过其订单访问范围。"""
    response = await get(make_app({"id": 201, "role": "agent"}), "/api/v1/orders/my")

    assert response.status_code == 403
