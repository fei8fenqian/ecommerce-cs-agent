"""新支付订单进入既有 Agent 查单工具的轻量回归测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.support_subjects import match_subject_identity_choices
from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolContext
from store.checkout_store import CustomerCheckoutOrder, CustomerCheckoutOrderItem
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
    assert orders[0]["order_source"] == "checkout"
    assert orders[0]["status"] == "PENDING_FULFILLMENT"
    assert orders[0]["order_status"] == "PAID"
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
    # TrackOrder owns the ordering of the authenticated customer's result set
    # and now exposes that trusted rank to semantic subject resolution.  Keep
    # the regression aligned with the public tool observation instead of
    # treating the added recency metadata as an unexpected payload change.
    expected_order = {**order, "delivery_state": "NOT_SHIPPED", "recency_rank": 1}
    assert result.data == {
        "count": 1,
        "orders": [expected_order],
        "multiple_results": False,
        "selection_required": False,
    }


@pytest.mark.asyncio
async def test_checkout_component_metadata_flows_to_real_subject_identity_candidates():
    """Production shape keeps catalogue table=components and adds component type."""
    ssd = CustomerCheckoutOrder(
        order_no="SO-SSD", status="PAID", total_amount_cents=66900,
        product_name="Acer宏碁N3500 NVME协议", quantity=1, payment_status="SUCCEEDED",
        fulfillment_status="PENDING_FULFILLMENT", tracking_company=None, tracking_number=None,
        created_at="2026-09-02T00:00:00+00:00", provider="unionpay_test",
        items=(
            CustomerCheckoutOrderItem(
                "Acer宏碁N3500 NVME协议", "components", "ssd-n3500", 1, 66900, "solid_state_drive"
            ),
        ),
    )
    ram = CustomerCheckoutOrder(
        order_no="SO-RAM", status="PAID", total_amount_cents=39900,
        product_name="金士顿 Fury Beast", quantity=1, payment_status="SUCCEEDED",
        fulfillment_status="PENDING_FULFILLMENT", tracking_company=None, tracking_number=None,
        created_at="2026-09-01T00:00:00+00:00", provider="alipay_sandbox",
        items=(CustomerCheckoutOrderItem("金士顿 Fury Beast", "components", "ram-fury", 1, 39900, "memory"),),
    )
    laptop = CustomerCheckoutOrder(
        order_no="SO-LAPTOP", status="PAID", total_amount_cents=599900,
        product_name="联想小新 Pro 16", quantity=1, payment_status="SUCCEEDED",
        fulfillment_status="PENDING_FULFILLMENT", tracking_company=None, tracking_number=None,
        created_at="2026-08-31T00:00:00+00:00",
        items=(CustomerCheckoutOrderItem("联想小新 Pro 16", "laptops", "lenovo-pro16", 1, 599900),),
    )
    with (
        patch("store.order_store.list_customer_orders", new=AsyncMock(return_value=[])),
        patch("store.order_store.list_customer_checkout_orders", new=AsyncMock(return_value=[ssd, ram, laptop])),
    ):
        orders = await find_orders(101)

    assert orders[0]["items"][0]["component_category"] == "solid_state_drive"
    assert [item["order_id"] for item in match_subject_identity_choices("我想退 SSD", orders)] == ["SO-SSD"]
    assert [item["order_id"] for item in match_subject_identity_choices("我想把内存退了", orders)] == ["SO-RAM"]
    assert [item["order_id"] for item in match_subject_identity_choices("刚买的电脑", orders)] == ["SO-LAPTOP"]


@pytest.mark.asyncio
async def test_legacy_order_is_marked_as_read_only_history():
    """旧订单仍可兼容查询，但结果必须显式标记为 legacy 来源。"""
    legacy_order = {
        "order_id": "legacy-001",
        "status": "shipped",
        "tracking": {"company": "测试物流", "number": "TEST-001"},
        "total_amount": 100.0,
        "paid_amount": 100.0,
        "payment_method": "历史订单",
        "order_date": "2026-08-01T12:00:00+00:00",
        "delivered_at": None,
        "items": [],
    }
    with (
        patch("store.order_store.list_customer_orders", new=AsyncMock(return_value=[legacy_order])),
        patch("store.order_store.list_customer_checkout_orders", new=AsyncMock(return_value=[])),
    ):
        orders = await find_orders(101)

    assert orders[0]["order_id"] == "legacy-001"
    assert orders[0]["order_source"] == "legacy"
