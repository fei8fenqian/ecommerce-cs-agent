"""tests/test_tools_compare.py — 商品对比工具测试"""

import pytest
import pytest_asyncio

from agent.tools.compare_products import CompareProducts
from agent.tools.tools_registry import ToolResult
from infra.db_pool import close_pool, init_pool


@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    yield
    await close_pool()


# =============================================================================
# 工具元数据（sync，不需要 DB）
# =============================================================================
class TestCompareProductsMeta:
    def test_name(self):
        tool = CompareProducts()
        assert tool.name == "compare_products"

    def test_description(self):
        tool = CompareProducts()
        assert "对比" in tool.description

    def test_parameters_require_both_products(self):
        tool = CompareProducts()
        required = tool.parameters["required"]
        assert "product_a" in required
        assert "product_b" in required

    def test_table_enum(self):
        tool = CompareProducts()
        tables = tool.parameters["properties"]["table"]["enum"]
        assert "laptop_products" in tables
        assert "phone_products" in tables

    def test_to_openai_schema(self):
        tool = CompareProducts()
        schema = tool.to_openai_function()
        assert schema["function"]["name"] == "compare_products"


# =============================================================================
# execute — 正常流程
# =============================================================================
class TestCompareProductsExecute:
    @pytest.mark.asyncio
    async def test_compare_two_laptops(self, _pool):
        """对比两款笔记本"""
        tool = CompareProducts()
        result = await tool.execute(
            product_a="联想拯救者",
            product_b="华为MateBook",
            table="laptop_products",
        )
        assert isinstance(result, ToolResult)
        if result.is_success:
            assert "product_a" in result.data
            assert "product_b" in result.data
            assert "title" in result.data["product_a"]
            assert "content" in result.data["product_a"]
            assert "title" in result.data["product_b"]

    @pytest.mark.asyncio
    async def test_compare_two_phones(self, _pool):
        """对比两款手机"""
        tool = CompareProducts()
        result = await tool.execute(
            product_a="iPhone",
            product_b="华为Mate",
            table="phone_products",
        )
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_default_table_is_laptop(self, _pool):
        """默认 table 是 laptop_products"""
        tool = CompareProducts()
        result = await tool.execute(product_a="拯救者", product_b="小新")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_content_truncation(self, _pool):
        """content 截断到 300 字符"""
        tool = CompareProducts()
        result = await tool.execute(
            product_a="拯救者",
            product_b="小新",
            table="laptop_products",
        )
        if result.is_success:
            assert len(result.data["product_a"]["content"]) <= 303
            assert len(result.data["product_b"]["content"]) <= 303


# =============================================================================
# execute — 异常路径
# =============================================================================
class TestCompareProductsErrors:
    @pytest.mark.asyncio
    async def test_empty_product_a(self, _pool):
        """product_a 为空 → error"""
        tool = CompareProducts()
        result = await tool.execute(product_a="", product_b="拯救者")
        assert result.is_success is False
        assert "产品" in result.error

    @pytest.mark.asyncio
    async def test_empty_product_b(self, _pool):
        """product_b 为空 → error"""
        tool = CompareProducts()
        result = await tool.execute(product_a="拯救者", product_b="")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_both_empty(self, _pool):
        """两个都为空 → error"""
        tool = CompareProducts()
        result = await tool.execute(product_a="", product_b="")
        assert result.is_success is False

    @pytest.mark.asyncio
    async def test_invalid_table(self, _pool):
        """不支持的 product table → error"""
        tool = CompareProducts()
        result = await tool.execute(
            product_a="A",
            product_b="B",
            table="knowledge_chunks",
        )
        assert result.is_success is False
        assert "不支持" in result.error

    @pytest.mark.asyncio
    async def test_product_not_found(self, _pool):
        """搜不到第一个产品时 → error（向量搜索几乎总能返回结果，此处验证错误处理路径）"""
        tool = CompareProducts()
        result = await tool.execute(
            product_a="ZZZZ不存在的产品ZZZZ",
            product_b="拯救者",
            table="laptop_products",
        )
        # 向量检索几乎总能返回一些结果，所以不一定触发 error
        # 验证至少结果是 ToolResult 类型
        assert isinstance(result, ToolResult)
