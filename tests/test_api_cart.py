"""购物车 API 的权限与支付跳转边界测试；不连接数据库。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request

from api.cart import cart_router
from service.cart_service import CartItemView
from service.checkout_service import CheckoutSession


def _app(role: str) -> FastAPI:
    """创建注入最小认证身份的购物车测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = {"id": 101, "role": role, "username": "cart-user"}
        return await call_next(request)

    app.include_router(cart_router)
    return app


def _item(*, quantity: int = 1) -> CartItemView:
    """构造可结算的最小购物车展示条目。"""
    return CartItemView(12, "laptops", "laptop-1", "测试笔记本", "测试品牌", 2999.0, 8, quantity, True)


@pytest.mark.asyncio
async def test_customer_can_add_and_read_their_cart():
    """客户增加商品后，API 只返回当前条目的展示字段。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.cart.add_cart_item", new=AsyncMock(return_value=_item())) as add:
            response = await client.post("/api/v1/cart/items", json={"category": "laptops", "product_id": "laptop-1"})

    assert response.status_code == 200
    assert response.json()["item_id"] == 12
    add.assert_awaited_once_with(101, "laptops", "laptop-1", 1)


@pytest.mark.asyncio
async def test_customer_can_add_component_to_cart():
    """配件使用与整机相同的客户购物车 API。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    component = CartItemView(13, "components", "memory-1", "测试内存", "测试品牌", 669.0, 8, 1, True)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.cart.add_cart_item", new=AsyncMock(return_value=component)) as add:
            response = await client.post(
                "/api/v1/cart/items",
                json={"category": "components", "product_id": "memory-1"},
            )

    assert response.status_code == 200
    assert response.json()["category"] == "components"
    add.assert_awaited_once_with(101, "components", "memory-1", 1)


@pytest.mark.asyncio
async def test_internal_role_cannot_read_customer_cart():
    """客服不能把客户购物车接口当成内部商品读取入口。"""
    transport = httpx.ASGITransport(app=_app("agent"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/cart")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_cart_checkout_returns_one_payment_redirect():
    """购物车结算只返回受服务层控制的支付会话。"""
    session = CheckoutSession("SOCART", 899800, "https://sandbox.example/cart-payment")
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.cart.create_cart_checkout_session", new=AsyncMock(return_value=session)) as checkout:
            response = await client.post("/api/v1/cart/checkout", json={"return_origin": "http://localhost:5173"})

    assert response.status_code == 200
    assert response.json()["order_no"] == "SOCART"
    checkout.assert_awaited_once_with(101, "http://localhost:5173")
