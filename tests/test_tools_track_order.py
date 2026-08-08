"""tests/test_tools_track_order.py — 订单追踪工具测试"""

import pytest
import pytest_asyncio

from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolResult
from core.db_pool import close_pool, get_connection, init_pool, put_connection


@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    yield
    await close_pool()


async def _get_first_order_id() -> str | None:
    conn = await get_connection()
    await conn.set_autocommit(True)
    try:
        cur = await conn.execute("SELECT order_id FROM orders LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        await put_connection(conn)


async def _get_first_phone() -> str | None:
    conn = await get_connection()
    await conn.set_autocommit(True)
    try:
        cur = await conn.execute("SELECT phone FROM orders WHERE phone != '' LIMIT 1")
        row = await cur.fetchone()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        await put_connection(conn)


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


# =============================================================================
# execute — 正常流程
# =============================================================================
class TestTrackOrderExecute:
    @pytest.mark.asyncio
    async def test_search_by_order_id(self, _pool):
        """用订单号精确查询"""
        order_id = await _get_first_order_id()
        if not order_id:
            pytest.skip("orders 表为空，跳过集成测试")

        tool = TrackOrder()
        result = await tool.execute(order_id=order_id)

        assert isinstance(result, ToolResult)
        assert result.is_success is True
        data = result.data
        assert data["order_id"] == order_id
        assert "status" in data
        assert "tracking" in data
        assert "items" in data
        assert isinstance(data["items"], list)

    @pytest.mark.asyncio
    async def test_search_by_phone(self, _pool):
        """用手机号查订单列表"""
        phone = await _get_first_phone()
        if not phone:
            pytest.skip("orders 表中没有带手机号的订单，跳过")

        tool = TrackOrder()
        result = await tool.execute(phone=phone)

        assert isinstance(result, ToolResult)
        if result.is_success:
            assert "count" in result.data
            assert "orders" in result.data
            assert result.data["count"] >= 1
            for order in result.data["orders"]:
                assert "order_id" in order
                assert "status" in order

    @pytest.mark.asyncio
    async def test_order_items_have_expected_fields(self, _pool):
        """订单商品包含完整字段"""
        order_id = await _get_first_order_id()
        if not order_id:
            pytest.skip("orders 表为空")

        tool = TrackOrder()
        result = await tool.execute(order_id=order_id)
        if result.is_success and result.data.get("items"):
            item = result.data["items"][0]
            assert "product_name" in item
            assert "price" in item
            assert "quantity" in item


# =============================================================================
# execute — 异常路径
# =============================================================================
class TestTrackOrderErrors:
    @pytest.mark.asyncio
    async def test_no_params_returns_error(self, _pool):
        """不传 order_id 也不传 phone → error"""
        tool = TrackOrder()
        result = await tool.execute()
        assert result.is_success is False
        assert "订单号" in result.error or "手机号" in result.error

    @pytest.mark.asyncio
    async def test_nonexistent_order(self, _pool):
        """不存在的订单号 → error"""
        tool = TrackOrder()
        result = await tool.execute(order_id="ORDER-NOT-EXISTS-99999")
        assert result.is_success is False
        assert "不存在" in result.error

    @pytest.mark.asyncio
    async def test_phone_with_no_orders(self, _pool):
        """没有订单的手机号 → error"""
        tool = TrackOrder()
        result = await tool.execute(phone="00000000000")
        assert result.is_success is False
        assert "没有订单" in result.error
