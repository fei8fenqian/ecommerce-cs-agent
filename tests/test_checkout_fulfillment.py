"""支付成功后创建履约记录的回归测试；不连接真实 PostgreSQL。"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from store.checkout_store import apply_alipay_trade_query


@pytest.mark.asyncio
async def test_successful_payment_creates_one_pending_fulfillment_in_same_transaction():
    """支付查询确认成功时，订单、支付和履约初始化必须一起提交。"""
    payment_id = uuid4()
    sales_order_id = uuid4()
    cursor = AsyncMock()
    cursor.fetchone.return_value = (payment_id, sales_order_id, 299900, "PENDING")
    connection = AsyncMock()
    connection.execute.side_effect = [None, cursor, None, None, None]

    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()),
    ):
        changed = await apply_alipay_trade_query(
            merchant_payment_no="PM202608240001",
            provider_trade_no="202608240001",
            amount_cents=299900,
            trade_status="TRADE_SUCCESS",
        )

    assert changed is True
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("INSERT INTO fulfillments" in statement for statement in statements)
    assert any("ON CONFLICT (sales_order_id) DO NOTHING" in statement for statement in statements)
    connection.commit.assert_awaited_once()
