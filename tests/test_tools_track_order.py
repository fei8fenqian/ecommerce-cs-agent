"""tests/test_tools_track_order.py — 订单追踪工具测试"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolContext, ToolResult
from infra.db_pool import close_pool, get_connection, init_pool, put_connection


@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    conn = await get_connection()
    await conn.set_autocommit(True)

    suffix = uuid.uuid4().hex[:8]
    order_a = f"TSTORD-A-{suffix}"
    order_b = f"TSTORD-B-{suffix}"
    unmatched_order = f"TSTORD-U-{suffix}"
    phone = "13900000000"

    user_rows = []
    for prefix in ("a", "b"):
        cur = await conn.execute(
            """
            INSERT INTO users (username, password_hash, role)
            VALUES (%s, %s, 'customer')
            RETURNING id
            """,
            (f"track-order-{prefix}-{suffix}", "test-hash"),
        )
        user_rows.append((await cur.fetchone())[0])

    owner_id, other_id = user_rows
    await conn.execute(
        """
        INSERT INTO orders
            (order_id, customer_user_id, customer_id, customer_name,
             order_date, status, phone, total_amount, paid_amount)
        VALUES
            (%s, %s, 'legacy-a', '测试客户A', '2026-08-01', 'shipped', %s, 100, 100),
            (%s, %s, 'legacy-b', '测试客户B', '2026-08-02', 'pending', %s, 200, 0),
            (%s, NULL, 'legacy-u', '未匹配客户', '2026-08-03', 'pending', %s, 300, 0)
        """,
        (order_a, owner_id, phone, order_b, other_id, phone, unmatched_order, phone),
    )
    await conn.execute(
        """
        INSERT INTO order_items (order_id, product_name, brand, price, quantity)
        VALUES (%s, '测试商品', '测试品牌', 100, 1)
        """,
        (order_a,),
    )
    await put_connection(conn)

    yield {
        "owner": ToolContext(user_id=owner_id, role="customer"),
        "other": ToolContext(user_id=other_id, role="customer"),
        "order_a": order_a,
        "order_b": order_b,
        "unmatched_order": unmatched_order,
        "phone": phone,
    }

    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute(
        "DELETE FROM order_items WHERE order_id IN (%s, %s, %s)",
        (order_a, order_b, unmatched_order),
    )
    await conn.execute(
        "DELETE FROM orders WHERE order_id IN (%s, %s, %s)",
        (order_a, order_b, unmatched_order),
    )
    await conn.execute("DELETE FROM users WHERE id IN (%s, %s)", (owner_id, other_id))
    await put_connection(conn)
    await close_pool()


# =============================================================================
# 工具元数据（sync，不需要 DB）
# =============================================================================
class TestTrackOrderMeta:
    def test_name(self):
        tool = TrackOrder()
        assert tool.name == "track_order"

    def test_description(self):
        tool = TrackOrder()
        assert "订单" in tool.description

    def test_parameters_not_required(self):
        tool = TrackOrder()
        assert tool.parameters["required"] == []

    def test_to_openai_schema(self):
        tool = TrackOrder()
        schema = tool.to_openai_function()
        assert schema["function"]["name"] == "track_order"
        assert "tool_context" not in schema["function"]["parameters"]["properties"]


# =============================================================================
# execute — 正常流程和归属隔离
# =============================================================================
class TestTrackOrderExecute:
    @pytest.mark.asyncio
    async def test_customer_can_search_own_order(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(
            order_id=_pool["order_a"],
            tool_context=_pool["owner"],
        )

        assert isinstance(result, ToolResult)
        assert result.is_success is True
        assert result.data["order_id"] == _pool["order_a"]
        assert result.data["items"][0]["product_name"] == "测试商品"

    @pytest.mark.asyncio
    async def test_customer_can_search_own_orders_by_phone(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(phone=_pool["phone"], tool_context=_pool["owner"])

        assert result.is_success is True
        assert result.data["count"] == 1
        assert result.data["orders"][0]["order_id"] == _pool["order_a"]

    @pytest.mark.asyncio
    async def test_customer_cannot_search_other_customers_order(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(
            order_id=_pool["order_b"],
            tool_context=_pool["owner"],
        )

        assert result.is_success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_unmatched_order_is_not_visible(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(
            order_id=_pool["unmatched_order"],
            tool_context=_pool["owner"],
        )

        assert result.is_success is False
        assert "不存在" in result.error


# =============================================================================
# execute — 异常路径
# =============================================================================
class TestTrackOrderErrors:
    @pytest.mark.asyncio
    async def test_failure_log_does_not_include_order_id(self, caplog):
        order_id = "ORDER-SECRET-12345"
        tool = TrackOrder()

        with patch(
            "agent.tools.track_order.find_orders",
            new=AsyncMock(side_effect=RuntimeError(f"database failed for order_id={order_id}")),
        ):
            with caplog.at_level("ERROR", logger="agent.tools.track_order"):
                result = await tool.execute(
                    order_id=order_id,
                    tool_context=ToolContext(user_id=101, role="customer"),
                )

        assert result.is_success is False
        assert order_id not in caplog.text
        assert order_id not in result.error

    @pytest.mark.asyncio
    async def test_missing_context_returns_error(self, _pool):
        result = await TrackOrder().execute(order_id=_pool["order_a"])
        assert result.is_success is False
        assert "当前用户身份" in result.error

    @pytest.mark.asyncio
    async def test_no_params_returns_error(self, _pool):
        result = await TrackOrder().execute(tool_context=_pool["owner"])
        assert result.is_success is False
        assert "订单号" in result.error or "手机号" in result.error

    @pytest.mark.asyncio
    async def test_nonexistent_order(self, _pool):
        result = await TrackOrder().execute(
            order_id="ORDER-NOT-EXISTS-99999",
            tool_context=_pool["owner"],
        )
        assert result.is_success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_phone_with_no_orders(self, _pool):
        result = await TrackOrder().execute(
            phone="00000000000",
            tool_context=_pool["owner"],
        )
        assert result.is_success is False
        assert "没有订单" in result.error
