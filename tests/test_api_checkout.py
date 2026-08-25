"""Checkout API 权限和请求边界测试；不连接 PostgreSQL。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request

from api.checkout import checkout_router
from service.checkout_service import CheckoutCancellationUnavailableError, CheckoutSession, PaymentNotCreatedError
from store.checkout_store import CustomerCheckoutOrder


def _app(role: str) -> FastAPI:
    """创建注入最小登录身份的测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = {"id": 101, "role": role, "username": "checkout-user"}
        return await call_next(request)

    app.include_router(checkout_router)
    return app


@pytest.mark.asyncio
async def test_customer_checkout_returns_signed_redirect_session():
    """客户确认购买后只收到订单号、金额和支付跳转 URL。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    session = CheckoutSession("SO202608240001", 449900, "https://sandbox.example/pay")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.create_checkout_session", new=AsyncMock(return_value=session)) as create:
            response = await client.post(
                "/api/v1/checkout/orders",
                json={
                    "category": "laptops",
                    "product_id": "laptop-1",
                    "quantity": 1,
                    "return_origin": "http://127.0.0.1:5173",
                },
            )

    assert response.status_code == 201
    assert response.json()["payment_url"] == "https://sandbox.example/pay"
    create.assert_awaited_once_with(
        customer_user_id=101,
        category="laptops",
        product_id="laptop-1",
        quantity=1,
        return_origin="http://127.0.0.1:5173",
    )


@pytest.mark.asyncio
async def test_customer_can_start_component_checkout():
    """配件走同一结算服务，不在 API 层被旧类别白名单拒绝。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    session = CheckoutSession("SOCOMPONENT", 66900, "https://sandbox.example/pay")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.create_checkout_session", new=AsyncMock(return_value=session)) as create:
            response = await client.post(
                "/api/v1/checkout/orders",
                json={"category": "components", "product_id": "memory-1"},
            )

    assert response.status_code == 201
    create.assert_awaited_once_with(
        customer_user_id=101,
        category="components",
        product_id="memory-1",
        quantity=1,
        return_origin=None,
    )


@pytest.mark.asyncio
async def test_non_customer_cannot_create_checkout_order():
    """客服角色不能借商品接口直接发起资金动作。"""
    transport = httpx.ASGITransport(app=_app("agent"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/v1/checkout/orders", json={"category": "phones", "product_id": "phone-1"})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_customer_can_only_list_their_new_checkout_orders():
    """新结算订单按 customer_user_id 查询，避免读取历史订单。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    checkout_order = CustomerCheckoutOrder(
        order_no="SO202608240001",
        status="PENDING_PAYMENT",
        total_amount_cents=449900,
        product_name="测试电脑",
        quantity=1,
        payment_status="PENDING",
        fulfillment_status=None,
        tracking_company=None,
        tracking_number=None,
        created_at="2026-08-24T10:00:00+00:00",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.list_customer_checkout_orders",
            new=AsyncMock(return_value=[checkout_order]),
        ) as listed:
            response = await client.get("/api/v1/checkout/orders/my")

    assert response.status_code == 200
    assert response.json()["orders"][0]["order_no"] == "SO202608240001"
    listed.assert_awaited_once_with(101)


@pytest.mark.asyncio
async def test_refresh_payment_explains_when_old_order_never_reached_alipay():
    """旧的本地待支付订单不能把“支付宝无此交易”伪装成服务故障。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.refresh_customer_payment_status",
            new=AsyncMock(side_effect=PaymentNotCreatedError()),
        ):
            response = await client.post("/api/v1/checkout/orders/SO202608240001/refresh-payment")

    assert response.status_code == 409
    assert "未在支付宝侧创建交易" in response.json()["detail"]


@pytest.mark.asyncio
async def test_customer_can_cancel_their_pending_checkout():
    """取消入口必须交给 checkout 服务关闭网关交易并收敛本地状态。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.cancel_checkout_session", new=AsyncMock()) as cancel:
            response = await client.post("/api/v1/checkout/orders/SO202608240001/cancel")

    assert response.status_code == 200
    assert response.json() == {"order_no": "SO202608240001", "cancelled": True}
    cancel.assert_awaited_once_with(customer_user_id=101, order_no="SO202608240001")


@pytest.mark.asyncio
async def test_cannot_cancel_paid_or_changed_checkout():
    """过期的支付页面不能把已变化订单再次取消。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.cancel_checkout_session",
            new=AsyncMock(side_effect=CheckoutCancellationUnavailableError()),
        ):
            response = await client.post("/api/v1/checkout/orders/SO202608240001/cancel")

    assert response.status_code == 409
