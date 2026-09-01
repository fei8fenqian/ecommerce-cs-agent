"""支付状态查询工具的身份范围与支付宝查询编排测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.check_payment_status import CheckPaymentStatus
from agent.tools_registry import ToolContext
from service.checkout_service import PaymentNotCreatedError, PaymentStatusUnavailableError


def _pending_order(order_id: str = "SO202608250001") -> dict[str, object]:
    return {
        "order_id": order_id,
        "status": "PENDING_PAYMENT",
        "items": [{"product_name": "测试笔记本", "quantity": 1}],
    }


@pytest.mark.asyncio
async def test_customer_payment_check_refreshes_only_own_order() -> None:
    """工具把当前客户身份交给受控结算服务，而不是信任模型提供的用户信息。"""
    tool = CheckPaymentStatus()
    refresh = AsyncMock(return_value=True)
    orders = AsyncMock(return_value=[{**_pending_order(), "status": "PAID"}])

    with (
        patch("agent.tools.check_payment_status.refresh_customer_payment_status", new=refresh),
        patch("agent.tools.check_payment_status.find_orders", new=orders),
    ):
        result = await tool.execute(
            order_id="SO202608250001",
            tool_context=ToolContext(user_id=42, role="customer"),
        )

    assert result.is_success
    assert result.data["payment_result"] == "PAID"
    refresh.assert_awaited_once_with(customer_user_id=42, order_no="SO202608250001")
    assert orders.await_count == 2
    assert orders.await_args_list == [
        ((42,), {"order_id": "SO202608250001"}),
        ((42,), {"order_id": "SO202608250001"}),
    ]


@pytest.mark.asyncio
async def test_payment_check_uses_existing_paid_fact_without_refreshing_gateway() -> None:
    """已支付订单没有 pending payment 时也必须返回已支付，而不是误报未支付。"""
    tool = CheckPaymentStatus()
    paid_order = {**_pending_order(), "status": "PAID", "payment_status": "SUCCEEDED"}
    orders = AsyncMock(return_value=[paid_order])
    refresh = AsyncMock(return_value=False)

    with (
        patch("agent.tools.check_payment_status.find_orders", new=orders),
        patch("agent.tools.check_payment_status.refresh_customer_payment_status", new=refresh),
    ):
        result = await tool.execute(
            order_id="SO202608250001",
            tool_context=ToolContext(user_id=42, role="customer"),
        )

    assert result.is_success
    assert result.data["payment_result"] == "PAID"
    refresh.assert_not_awaited()
    orders.assert_awaited_once_with(42, order_id="SO202608250001")


@pytest.mark.asyncio
async def test_payment_check_without_order_uses_current_users_latest_pending_checkout() -> None:
    """“刚刚那笔”只会解析成当前用户自己的最近待支付商城订单。"""
    tool = CheckPaymentStatus()
    pending = _pending_order()
    orders = AsyncMock(side_effect=[[pending], [{**pending, "status": "PAID"}]])
    refresh = AsyncMock(return_value=True)

    with (
        patch("agent.tools.check_payment_status.find_orders", new=orders),
        patch("agent.tools.check_payment_status.refresh_customer_payment_status", new=refresh),
    ):
        result = await tool.execute(tool_context=ToolContext(user_id=42, role="customer"))

    assert result.is_success
    refresh.assert_awaited_once_with(customer_user_id=42, order_no="SO202608250001")


@pytest.mark.asyncio
async def test_payment_check_does_not_query_non_customer_or_non_checkout_orders() -> None:
    """客服、运营和 legacy 单号不能借此调用客户支付宝查询。"""
    tool = CheckPaymentStatus()
    refresh = AsyncMock()

    with patch("agent.tools.check_payment_status.refresh_customer_payment_status", new=refresh):
        staff_result = await tool.execute(order_id="SO202608250001", tool_context=ToolContext(user_id=7, role="agent"))
        legacy_result = await tool.execute(order_id="ORD-LEGACY", tool_context=ToolContext(user_id=42, role="customer"))

    assert not staff_result.is_success
    assert not legacy_result.is_success
    refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (PaymentNotCreatedError("not found"), "PAYMENT_NOT_CREATED"),
        (PaymentStatusUnavailableError("unavailable"), "PAYMENT_STATUS_UNAVAILABLE"),
    ],
)
async def test_payment_check_returns_controlled_unknown_or_not_created_fact(
    exception: Exception,
    expected_status: str,
) -> None:
    """网关不可用是 UNKNOWN，不让后续 LLM 自由解释为支付失败。"""
    tool = CheckPaymentStatus()
    refresh = AsyncMock(side_effect=exception)

    with (
        patch("agent.tools.check_payment_status.find_orders", new=AsyncMock(return_value=[_pending_order()])),
        patch("agent.tools.check_payment_status.refresh_customer_payment_status", new=refresh),
    ):
        result = await tool.execute(order_id="SO202608250001", tool_context=ToolContext(user_id=42, role="customer"))

    assert result.is_success
    assert result.data["payment_result"] == expected_status
    assert result.data["order"]["order_id"] == "SO202608250001"
