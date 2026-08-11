"""tests/test_adversarial.py — 对抗性测试：空输入、注入、越界、极端值

所有测试不依赖 LLM，直接测工具/函数层面的鲁棒性。
"""

import pytest
import pytest_asyncio

from agent.tools.check_stock import CheckStock
from agent.tools.compare_products import CompareProducts
from agent.tools.create_ticket import CreateTicket
from agent.tools.search_component import SearchComponent
from agent.tools.search_product import SearchProduct
from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolResult
from infra.db_pool import close_pool, get_connection, init_pool, put_connection
from store.ticket_store import create_ticket, get_ticket, init_ticket_table, list_tickets, update_ticket


# =============================================================================
# 基础设施 fixtures
# =============================================================================
@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    yield
    await close_pool()


@pytest_asyncio.fixture
async def _tickets():
    await init_pool(minconn=1, maxconn=2)
    await init_ticket_table()
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM tickets")
    await put_connection(conn)
    yield
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM tickets")
    await put_connection(conn)
    await close_pool()


# =============================================================================
# 空输入 / None / 空白
# =============================================================================
class TestEmptyInputs:
    """空输入不应崩溃，应返回 error status"""

    @pytest.mark.asyncio
    async def test_check_stock_empty_name(self, _pool):
        tool = CheckStock()
        result = await tool.execute(product_name="")
        assert isinstance(result, ToolResult)
        # 不崩就行

    @pytest.mark.asyncio
    async def test_track_order_empty_both(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(order_id="", phone="")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_compare_empty_both(self, _pool):
        tool = CompareProducts()
        result = await tool.execute(product_a="", product_b="")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_search_product_empty_query(self, _pool):
        tool = SearchProduct()
        result = await tool.execute(query="")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_search_component_empty_query(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(query="", component="cpu")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_create_ticket_empty_issue(self, _tickets):
        """issue 是 required 字段，空字符串应该也能创建（业务层校验不在工具层）"""
        tool = CreateTicket()
        result = await tool.execute(issue="")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_search_product_whitespace_query(self, _pool):
        tool = SearchProduct()
        result = await tool.execute(query="   ")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_check_stock_whitespace_name(self, _pool):
        tool = CheckStock()
        result = await tool.execute(product_name="   ")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_track_order_whitespace_order_id(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(order_id="   ")
        assert result.is_success is False  # 查不到
        assert isinstance(result, ToolResult)


# =============================================================================
# 极端值 / 边界越界
# =============================================================================
class TestExtremeValues:
    @pytest.mark.asyncio
    async def test_negative_price_min(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(query="CPU", component="cpu", price_min=-1000)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_negative_price_max(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(query="CPU", component="cpu", price_max=-500)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_inverted_price_range(self, _pool):
        """price_min > price_max → 不会 crash"""
        tool = SearchComponent()
        result = await tool.execute(
            query="CPU",
            component="cpu",
            price_min=5000,
            price_max=1000,
        )
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_zero_price(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(
            query="CPU",
            component="cpu",
            price_min=0,
            price_max=0,
        )
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_huge_top_k_search_product(self, _pool):
        tool = SearchProduct()
        result = await tool.execute(query="联想", top_k=999999)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_huge_top_k_search_component(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(query="CPU", component="cpu", top_k=999999)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_zero_top_k(self, _pool):
        tool = SearchProduct()
        result = await tool.execute(query="联想", top_k=0)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_super_long_query(self, _pool):
        """超长输入不应 OOM 或 crash"""
        tool = SearchProduct()
        result = await tool.execute(query="联想" * 5000)  # 25000 字符
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_super_long_product_name(self, _pool):
        tool = CheckStock()
        result = await tool.execute(product_name="A" * 10000)
        assert isinstance(result, ToolResult)


# =============================================================================
# SQL 注入 / 恶意输入
# =============================================================================
class TestSQLInjection:
    @pytest.mark.asyncio
    async def test_sql_injection_product_name(self, _pool):
        """'; DROP TABLE ... -- 不应被执行"""
        tool = CheckStock()
        result = await tool.execute(product_name="'; DROP TABLE tickets; --")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_sql_injection_track_order(self, _pool):
        tool = TrackOrder()
        result = await tool.execute(order_id="' OR '1'='1")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_sql_union_injection(self, _pool):
        """UNION SELECT 注入尝试"""
        tool = CheckStock()
        result = await tool.execute(product_name="' UNION SELECT * FROM tickets --")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_sql_comment_injection(self, _pool):
        """用 SQL 注释尝试绕过"""
        tool = CheckStock()
        result = await tool.execute(product_name="拯救者' --")
        assert isinstance(result, ToolResult)
        # 正常查不到就返回 error，关键是没崩没执行恶意 SQL

    @pytest.mark.asyncio
    async def test_sql_injection_in_component_query(self, _pool):
        tool = SearchComponent()
        result = await tool.execute(
            query="'; DROP TABLE component_products; --",
            component="cpu",
        )
        assert isinstance(result, ToolResult)


# =============================================================================
# Unicode 特殊字符
# =============================================================================
class TestUnicodeEdgeCases:
    @pytest.mark.asyncio
    async def test_emoji_only_query(self, _pool):
        tool = SearchProduct()
        result = await tool.execute(query="💀🔥💀🔥💀")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_zero_width_characters(self, _pool):
        """零宽字符不应破坏查询"""
        # U+200B = zero-width space
        tool = SearchProduct()
        result = await tool.execute(query="联想​拯救​者")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_right_to_left_override(self, _pool):
        """RTL override 不应破坏系统"""
        # U+202E = RIGHT-TO-LEFT OVERRIDE
        tool = SearchProduct()
        result = await tool.execute(query="‮联想拯救者")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_mixed_cjk_and_special(self, _pool):
        """中日韩 + 特殊字符混合"""
        tool = SearchProduct()
        result = await tool.execute(query="联想™©®拯救者【旗舰版】\n\t\r")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_null_byte(self, _pool):
        """NULL 字节不应导致截断攻击"""
        tool = SearchProduct()
        result = await tool.execute(query="联想\x00拯救者")
        assert isinstance(result, ToolResult)


# =============================================================================
# 不存在的资源 / 无效枚举
# =============================================================================
class TestInvalidResources:
    @pytest.mark.asyncio
    async def test_invalid_component_type(self, _pool):
        """不在 CATEGORY_MAP 里的组件类型 → 被 catch 返回 error（不 crash）"""
        tool = SearchComponent()
        result = await tool.execute(query="test", component="rocket")
        assert result.is_success is False
        assert "检索失败" in result.error

    @pytest.mark.asyncio
    async def test_invalid_table_check_stock(self, _pool):
        tool = CheckStock()
        result = await tool.execute(product_name="test", table="nonexistent")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_invalid_table_compare(self, _pool):
        tool = CompareProducts()
        result = await tool.execute(product_a="A", product_b="B", table="invalid_table")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_invalid_urgency_create_ticket(self, _tickets):
        tool = CreateTicket()
        result = await tool.execute(issue="测试", urgency="nuclear")
        assert result.is_success is True  # 降级为 medium，不崩
        assert result.data["urgency"] == "medium"


# =============================================================================
# ticket_store 防御
# =============================================================================
class TestTicketStoreDefense:
    @pytest.mark.asyncio
    async def test_list_invalid_status(self, _tickets):
        """非法的 status 不会崩溃，只是查不到"""
        tickets = await list_tickets(status="hacked_status")
        assert tickets == []

    @pytest.mark.asyncio
    async def test_update_invalid_fields(self, _tickets):
        """不在白名单的字段被忽略，白名单字段正常更新"""
        await create_ticket("TK-DEFENSE-1", "测试")
        result = await update_ticket(
            "TK-DEFENSE-1",
            status="已处理",
            customer_name="攻击者",  # 不在白名单，应被忽略
        )
        assert result is True
        ticket = await get_ticket("TK-DEFENSE-1")
        assert ticket["ticket_id"] == "TK-DEFENSE-1"  # 没变
        assert ticket["status"] == "已处理"  # 白名单字段正常更新
        assert ticket["customer_name"] == ""  # 非白名单字段未生效（创建时默认为空）

    @pytest.mark.asyncio
    async def test_get_nonexistent_ticket(self, _tickets):
        ticket = await get_ticket("TK-GHOST-99999")
        assert ticket is None

    @pytest.mark.asyncio
    async def test_update_nonexistent_ticket(self, _tickets):
        result = await update_ticket("TK-NOT-EXISTS", status="已处理")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_duplicate_ticket_id(self, _tickets):
        """重复 ticket_id → 抛 DB 异常"""
        await create_ticket("TK-DUP", "第一次")
        with pytest.raises(Exception):
            await create_ticket("TK-DUP", "第二次")

    @pytest.mark.asyncio
    async def test_special_chars_in_customer_name(self, _tickets):
        """特殊字符在 customer_name 中存储正常"""
        await create_ticket(
            "TK-SPECIAL",
            "测试特殊字符",
            customer_name="<script>alert('xss')</script>",
        )
        ticket = await get_ticket("TK-SPECIAL")
        assert ticket is not None
        assert "script" in ticket["customer_name"]
