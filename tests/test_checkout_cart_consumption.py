"""付款成功后消费购物车快照的边界测试；不连接真实 PostgreSQL。"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from store.checkout_store import _consume_paid_cart_lines


@pytest.mark.asyncio
async def test_paid_snapshot_does_not_consume_cart_item_changed_after_checkout():
    """旧订单晚到付款时，不应消费下单后修改过的同款购物车行。"""
    snapshot_created_at = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
    snapshot_cursor = MagicMock()
    snapshot_cursor.fetchall = AsyncMock(return_value=[("components", "memory-1", 1, snapshot_created_at)])
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=[snapshot_cursor, None, None])

    await _consume_paid_cart_lines(connection, uuid4())

    delete_params = connection.execute.await_args_list[1].args[1]
    update_params = connection.execute.await_args_list[2].args[1]
    assert delete_params[-1] == snapshot_created_at
    assert update_params[-1] == snapshot_created_at
    assert "i.updated_at <= %s" in connection.execute.await_args_list[1].args[0]
    assert "i.updated_at <= %s" in connection.execute.await_args_list[2].args[0]
