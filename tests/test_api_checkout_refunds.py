"""退款 API 的身份、幂等键和服务边界测试；不连接 PostgreSQL。"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from api.checkout import checkout_router
from service.checkout_refund_service import CustomerRefundResult, RefundNotEligibleError


def _app(role: str) -> FastAPI:
    """创建仅注入当前身份的退款 API 测试应用。"""
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.user = {"id": 101, "role": role, "username": "refund-user"}
        return await call_next(request)

    app.include_router(checkout_router)
    return app


def _result(*, status: str = "PENDING_CONFIRMATION") -> CustomerRefundResult:
    """构造稳定退款服务响应。"""
    return CustomerRefundResult(
        refund_id=uuid4(),
        order_no="SO202608250001",
        status=status,
        amount_cents=529900,
        currency="CNY",
        reason="不需要了",
        requested_at="2026-08-25T10:00:00+00:00",
        idempotent_replay=False,
    )


@pytest.mark.asyncio
async def test_customer_can_request_refund_with_idempotency_key() -> None:
    """退款申请需要客户身份和客户端重放键，但不允许客户端传金额。"""
    response_data = _result()
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.request_customer_refund", new=AsyncMock(return_value=response_data)) as requested:
            response = await client.post(
                "/api/v1/checkout/orders/SO202608250001/refunds",
                headers={"Idempotency-Key": "request-key-0001"},
                json={"reason": "不需要了", "amount_cents": 1},
            )

    assert response.status_code == 201
    assert response.json()["amount_cents"] == 529900
    requested.assert_awaited_once_with(
        customer_user_id=101,
        order_no="SO202608250001",
        reason="不需要了",
        request_idempotency_key="request-key-0001",
    )


@pytest.mark.asyncio
async def test_refund_request_hides_unavailable_order_as_conflict() -> None:
    """客户不能据此枚举不属于自己的可退款订单。"""
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.checkout.request_customer_refund", new=AsyncMock(side_effect=RefundNotEligibleError())):
            response = await client.post(
                "/api/v1/checkout/orders/SO202608250001/refunds",
                headers={"Idempotency-Key": "request-key-0001"},
                json={},
            )

    assert response.status_code == 409
    assert "不满足退款条件" in response.json()["detail"]


@pytest.mark.asyncio
async def test_only_customer_can_confirm_refund() -> None:
    """客服、运营或模型侧调用都不能绕过客户确认边界。"""
    transport = httpx.ASGITransport(app=_app("agent"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/checkout/refunds/{uuid4()}/confirm",
            headers={"Idempotency-Key": "confirm-key-0001"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_customer_can_refresh_own_refund_without_an_idempotency_key() -> None:
    """退款刷新是只读查询，不会触发第二笔资金动作，也不要求命令幂等键。"""
    response_data = _result(status="PROCESSING")
    transport = httpx.ASGITransport(app=_app("customer"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch(
            "api.checkout.refresh_customer_refund_status",
            new=AsyncMock(return_value=response_data),
        ) as refreshed:
            response = await client.post(f"/api/v1/checkout/refunds/{response_data.refund_id}/refresh")

    assert response.status_code == 200
    assert response.json()["status"] == "PROCESSING"
    refreshed.assert_awaited_once_with(customer_user_id=101, refund_id=response_data.refund_id)
