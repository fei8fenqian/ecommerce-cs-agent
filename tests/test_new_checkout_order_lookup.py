"""新支付订单进入既有 Agent 查单工具的轻量回归测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolContext
from store.checkout_store import CustomerCheckoutOrder
from store.order_store import find_orders


@pytest.mark.asyncio
async def test_current_customer_lookup_includes_new_checkout_orders():
    """用户说“我的订单”时，Agent 能看到自己的新支付订单而非仅 legacy 订单。"""
    checkout_order = CustomerCheckoutOrder(
        order_no="SO202608240001",
        status="PAID",
        total_amount_cents=299900,
        product_name="测试笔记本",
        quantity=1,
        payment_status="SUCCEEDED",
        fulfillment_status="PENDING_FULFILLMENT",
        tracking_company=None,
        tracking_number=None,
        created_at="2026-08-24T12:00:00+00:00",
    )
    with (
        patch("store.order_store.list_customer_orders", new=AsyncMock(return_value=[])),
        patch("store.order_store.list_customer_checkout_orders", new=AsyncMock(return_value=[checkout_order])),
    ):
        orders = await find_orders(101)

    assert orders[0]["order_id"] == "SO202608240001"
    assert orders[0]["status"] == "PENDING_FULFILLMENT"
    assert orders[0]["paid_amount"] == 2999.0


@pytest.mark.asyncio
async def test_track_order_without_parameters_reads_current_customer_orders():
    """模型没有提取到订单号时仍可按受控身份读取当前客户自己的最近订单。"""
    order = {
        "order_id": "SO202608240001",
        "status": "PAID",
        "tracking": {"company": None, "number": None},
        "total_amount": 2999.0,
        "paid_amount": 2999.0,
        "payment_method": "支付宝沙箱",
        "order_date": "2026-08-24T12:00:00+00:00",
        "delivered_at": None,
        "items": [],
    }
    with patch("agent.tools.track_order.find_orders", new=AsyncMock(return_value=[order])):
        result = await TrackOrder().execute(tool_context=ToolContext(user_id=101, role="customer"))

    assert result.is_success is True
    assert result.data == {"count": 1, "orders": [order]}
