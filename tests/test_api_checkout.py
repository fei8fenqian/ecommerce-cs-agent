"""Checkout API 权限和请求边界测试；不连接 PostgreSQL。"""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI, Request

from api.checkout import checkout_router
from service.checkout_refund_service import CustomerRefundResult
from service.checkout_service import (
    CheckoutCancellationUnavailableError,
    CheckoutSession,
    PaymentNotCreatedError,
    UnionPayCancellationUnavailableError,
)
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
        payment_provider="alipay_sandbox",
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
        payment_provider="alipay_sandbox",
    )


@pytest.mark.asyncio
async def test_customer_can_explicitly_select_unionpay_checkout() -> None:
    """UnionPay 选择由请求显式携带，并原样投影服务端 checkout session。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    session = CheckoutSession(
        "SO202608240002",
        449900,
        "",
        payment_form_action="https://gateway.test.95516.com/gateway/api/frontTransReq.do",
        payment_form_fields={"orderId": "PMV2TEST"},
        payment_provider="unionpay_test",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.create_checkout_session", new=AsyncMock(return_value=session)) as create:
            response = await client.post(
                "/api/v1/checkout/orders",
                json={
                    "category": "laptops",
                    "product_id": "laptop-1",
                    "payment_provider": "unionpay_test",
                },
            )

    assert response.status_code == 201
    assert response.json()["payment_provider"] == "unionpay_test"
    create.assert_awaited_once_with(
        customer_user_id=101,
        category="laptops",
        product_id="laptop-1",
        quantity=1,
        return_origin=None,
        payment_provider="unionpay_test",
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
        refund_id="11111111-1111-4111-8111-111111111111",
        refund_status="PENDING_FINANCE_APPROVAL",
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.list_customer_checkout_orders",
            new=AsyncMock(return_value=[checkout_order]),
        ) as listed:
            response = await client.get("/api/v1/checkout/orders/my")

    assert response.status_code == 200
    assert response.json()["orders"][0]["order_no"] == "SO202608240001"
    assert response.json()["orders"][0]["refund_status"] == "PENDING_MERCHANT_REVIEW"
    listed.assert_awaited_once_with(101)


@pytest.mark.asyncio
async def test_customer_refund_response_hides_internal_finance_review_state() -> None:
    """客户只知道商家正在核实，不会收到内部财务审批状态。"""
    refund_id = "33333333-3333-4333-8333-333333333333"
    result = CustomerRefundResult(
        refund_id=UUID(refund_id),
        order_no="SO202608250003",
        status="PENDING_FINANCE_APPROVAL",
        amount_cents=300000,
        currency="CNY",
        reason="测试退款",
        requested_at="2026-08-25T10:00:00+00:00",
        idempotent_replay=False,
    )
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.confirm_customer_refund", new=AsyncMock(return_value=result)):
            response = await client.post(
                f"/api/v1/checkout/refunds/{refund_id}/confirm",
                headers={"Idempotency-Key": "customer-confirm-0001"},
            )

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING_MERCHANT_REVIEW"


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
    assert "尚未创建可查询的支付交易" in response.json()["detail"]


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


@pytest.mark.asyncio
async def test_unionpay_pending_cancel_is_explicitly_rejected() -> None:
    """U1 不把银联消费撤销误当作支付宝 close。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.cancel_checkout_session",
            new=AsyncMock(side_effect=UnionPayCancellationUnavailableError()),
        ):
            response = await client.post("/api/v1/checkout/orders/SO202608240001/cancel")

    assert response.status_code == 409
    assert "银联测试订单" in response.json()["detail"]


@pytest.mark.asyncio
async def test_unionpay_refund_request_returns_pending_confirmation() -> None:
    """UnionPay 退款申请只创建待确认记录，不在申请阶段调用网关。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    refund_id = UUID("22222222-2222-4222-8222-222222222222")
    result = CustomerRefundResult(
        refund_id=refund_id,
        order_no="SO202608240001",
        status="PENDING_CONFIRMATION",
        amount_cents=449900,
        currency="CNY",
        reason="测试",
        requested_at="2026-08-25T10:00:00+00:00",
        idempotent_replay=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.request_customer_refund", new=AsyncMock(return_value=result)) as request_refund:
            response = await client.post(
                "/api/v1/checkout/orders/SO202608240001/refunds",
                headers={"Idempotency-Key": "unionpay-refund-api-1"},
                json={"reason": "测试"},
            )

    assert response.status_code == 201
    assert response.json()["status"] == "PENDING_CONFIRMATION"
    request_refund.assert_awaited_once()


@pytest.mark.asyncio
async def test_only_finance_can_approve_refund() -> None:
    """财务审批接口走统一服务，客服不能借路由触碰资金动作。"""
    refund_id = "11111111-1111-4111-8111-111111111111"
    result = CustomerRefundResult(
        refund_id=UUID(refund_id),
        order_no="SO202608250001",
        status="PROCESSING",
        amount_cents=300000,
        currency="CNY",
        reason="测试退款",
        requested_at="2026-08-25T10:00:00+00:00",
        idempotent_replay=False,
    )
    transport = httpx.ASGITransport(app=_app("finance"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.approve_finance_refund", new=AsyncMock(return_value=result)) as approve:
            response = await client.post(
                f"/api/v1/checkout/finance/refunds/{refund_id}/approve",
                headers={"Idempotency-Key": "finance-approve-0001"},
                json={"decision_note": "已核对"},
            )

    assert response.status_code == 200
    assert response.json()["status"] == "PROCESSING"
    approve.assert_awaited_once()

    transport = httpx.ASGITransport(app=_app("agent"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/checkout/finance/refunds/{refund_id}/approve",
            headers={"Idempotency-Key": "finance-approve-0002"},
            json={"decision_note": "越权"},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_finance_can_reject_refund_without_gateway_call() -> None:
    """驳回命令返回确定性结果，支付网关调用由服务层保证为零。"""
    refund_id = "22222222-2222-4222-8222-222222222222"
    result = CustomerRefundResult(
        refund_id=UUID(refund_id),
        order_no="SO202608250002",
        status="REJECTED",
        amount_cents=300000,
        currency="CNY",
        reason="测试退款",
        requested_at="2026-08-25T10:00:00+00:00",
        idempotent_replay=False,
    )
    transport = httpx.ASGITransport(app=_app("finance"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.reject_finance_refund_request", new=AsyncMock(return_value=result)) as reject:
            response = await client.post(
                f"/api/v1/checkout/finance/refunds/{refund_id}/reject",
                headers={"Idempotency-Key": "finance-reject-0001"},
                json={"decision_note": "不符合退款条件"},
            )

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    reject.assert_awaited_once()
