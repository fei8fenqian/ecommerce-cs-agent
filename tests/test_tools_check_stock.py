"""tests/test_tools_check_stock.py — 库存查询工具测试"""

import pytest
import pytest_asyncio

from agent.tools.check_stock import CheckStock
from agent.tools_registry import ToolResult
from infra.db_pool import close_pool, init_pool


@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    yield
    await close_pool()


# =============================================================================
# 工具元数据（sync，不需要 DB）
# =============================================================================
class TestCheckStockMeta:
    def test_name(self):
        tool = CheckStock()
        assert tool.name == "check_stock"

    def test_description(self):
        tool = CheckStock()
        assert "库存" in tool.description

    def test_parameters_require_product_name(self):
        tool = CheckStock()
        assert "product_name" in tool.parameters["required"]

    def test_parameters_table_enum(self):
        tool = CheckStock()
        tables = tool.parameters["properties"]["table"]["enum"]
        assert "laptop_products" in tables
        assert "phone_products" in tables
        assert "component_products" in tables

    def test_to_openai_schema(self):
        tool = CheckStock()
        schema = tool.to_openai_function()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "check_stock"


# =============================================================================
# execute — 正常流程
# =============================================================================
class TestCheckStockExecute:
    @pytest.mark.asyncio
    async def test_search_laptop_by_name(self, _pool):
        """模糊搜索笔记本库存"""
        tool = CheckStock()
        result = await tool.execute(product_name="拯救者", table="laptop_products")
        assert isinstance(result, ToolResult)
        assert result.is_success is True
        assert result.data["count"] > 0
        for item in result.data["results"]:
            assert "name" in item
            assert "stock" in item
            assert "warehouse" in item
            assert "price" in item

    @pytest.mark.asyncio
    async def test_search_phone_by_name(self, _pool):
        """模糊搜索手机库存"""
        tool = CheckStock()
        result = await tool.execute(product_name="iPhone", table="phone_products")
        assert isinstance(result, ToolResult)
        assert result.status in ("success", "error")

    @pytest.mark.asyncio
    async def test_search_component_by_name(self, _pool):
        """搜索组件库存"""
        tool = CheckStock()
        result = await tool.execute(product_name="RTX", table="component_products")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_default_table_is_laptop(self, _pool):
        """不传 table 默认查 laptop_products"""
        tool = CheckStock()
        result = await tool.execute(product_name="拯救者")
        assert "不支持的表" not in (result.error or "")

    @pytest.mark.asyncio
    async def test_result_fields_types(self, _pool):
        """返回数据字段类型正确"""
        tool = CheckStock()
        result = await tool.execute(product_name="拯救者")
        if result.is_success:
            item = result.data["results"][0]
            assert isinstance(item["name"], str)
            assert isinstance(item["stock"], int)
            assert isinstance(item["price"], (int, float))
            assert isinstance(item["warehouse"], str)


# =============================================================================
# execute — 异常路径
# =============================================================================
class TestCheckStockErrors:
    @pytest.mark.asyncio
    async def test_invalid_table(self, _pool):
        """不支持的表名 → error"""
        tool = CheckStock()
        result = await tool.execute(product_name="test", table="evil_table")
        assert result.is_success is False
        assert "不支持的表" in result.error

    @pytest.mark.asyncio
    async def test_no_results(self, _pool):
        """搜不到 → error"""
        tool = CheckStock()
        result = await tool.execute(product_name="ZZZZZ不存在的产品名ZZZZZ")
        assert result.is_success is False
        assert "未找到" in result.error
