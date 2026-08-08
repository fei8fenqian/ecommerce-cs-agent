"""tests/test_tools_search.py — search_product + search_component 工具测试"""

import pytest
import pytest_asyncio

from agent.tools.search_component import CATEGORY_MAP, SearchComponent
from agent.tools.search_product import SearchProduct
from agent.tools_registry import ToolResult
from core.db_pool import close_pool, init_pool


@pytest_asyncio.fixture
async def _pool():
    await init_pool(minconn=1, maxconn=2)
    yield
    await close_pool()


# =============================================================================
# SearchProduct — 元数据（sync）
# =============================================================================
class TestSearchProductMeta:
    def test_name(self):
        tool = SearchProduct()
        assert tool.name == "search_product"

    def test_description(self):
        tool = SearchProduct()
        assert "搜索" in tool.description

    def test_parameters_require_query(self):
        tool = SearchProduct()
        assert "query" in tool.parameters["required"]

    def test_table_enum(self):
        tool = SearchProduct()
        tables = tool.parameters["properties"]["table"]["enum"]
        assert "laptop_products" in tables
        assert "phone_products" in tables
        assert "knowledge_chunks" in tables

    def test_to_openai_schema(self):
        tool = SearchProduct()
        schema = tool.to_openai_function()
        assert schema["function"]["name"] == "search_product"


# =============================================================================
# SearchProduct — execute
# =============================================================================
class TestSearchProductExecute:
    @pytest.mark.asyncio
    async def test_search_laptop(self, _pool):
        """搜索笔记本 → 返回结果"""
        tool = SearchProduct()
        result = await tool.execute(query="联想拯救者", table="laptop_products", top_k=5)
        assert isinstance(result, ToolResult)
        if result.is_success:
            assert result.data["count"] > 0
            for r in result.data["results"]:
                assert "title" in r
                assert "content" in r
                assert "score" in r

    @pytest.mark.asyncio
    async def test_search_phone(self, _pool):
        """搜索手机"""
        tool = SearchProduct()
        result = await tool.execute(query="iPhone", table="phone_products", top_k=5)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_search_knowledge(self, _pool):
        """搜索知识库"""
        tool = SearchProduct()
        result = await tool.execute(query="退货政策", table="knowledge_chunks", top_k=5)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_default_table_is_laptop(self, _pool):
        """默认 table 是 laptop_products"""
        tool = SearchProduct()
        result = await tool.execute(query="拯救者")
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_content_truncation(self, _pool):
        """content 字段不超过 200 字符"""
        tool = SearchProduct()
        result = await tool.execute(query="联想")
        if result.is_success and result.data["results"]:
            for r in result.data["results"]:
                assert len(r["content"]) <= 203


# =============================================================================
# SearchComponent — 元数据（sync）
# =============================================================================
class TestSearchComponentMeta:
    def test_name(self):
        tool = SearchComponent()
        assert tool.name == "search_component"

    def test_description(self):
        tool = SearchComponent()
        assert "配件" in tool.description or "台式" in tool.description

    def test_parameters_require_query_and_component(self):
        tool = SearchComponent()
        required = tool.parameters["required"]
        assert "query" in required
        assert "component" in required

    def test_component_enum(self):
        tool = SearchComponent()
        components = tool.parameters["properties"]["component"]["enum"]
        for c in ("cpu", "gpu", "motherboard", "ram", "ssd", "psu", "case", "cooler"):
            assert c in components

    def test_price_filter_params(self):
        tool = SearchComponent()
        props = tool.parameters["properties"]
        assert "price_min" in props
        assert "price_max" in props

    def test_category_map_covers_all(self):
        """CATEGORY_MAP 覆盖了所有 enum 值"""
        tool = SearchComponent()
        enum_vals = set(tool.parameters["properties"]["component"]["enum"])
        assert enum_vals == set(CATEGORY_MAP.keys())


# =============================================================================
# SearchComponent — execute
# =============================================================================
class TestSearchComponentExecute:
    @pytest.mark.asyncio
    async def test_search_cpu(self, _pool):
        """搜索 CPU"""
        tool = SearchComponent()
        result = await tool.execute(query="锐龙", component="cpu", top_k=5)
        assert isinstance(result, ToolResult)
        if result.is_success:
            assert result.data["count"] > 0
            for r in result.data["results"]:
                assert "title" in r
                assert "category" in r
                assert "price" in r
                assert "normalized" in r

    @pytest.mark.asyncio
    async def test_search_gpu(self, _pool):
        """搜索显卡"""
        tool = SearchComponent()
        result = await tool.execute(query="RTX 4060", component="gpu", top_k=5)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_search_all_component_types(self, _pool):
        """每种组件类型都能搜"""
        tool = SearchComponent()
        for comp_type in CATEGORY_MAP:
            result = await tool.execute(query="", component=comp_type, top_k=3)
            assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_price_filter(self, _pool):
        """价格过滤"""
        tool = SearchComponent()
        result = await tool.execute(
            query="CPU",
            component="cpu",
            top_k=10,
            price_min=500,
            price_max=2000,
        )
        assert isinstance(result, ToolResult)
        if result.is_success:
            for r in result.data["results"]:
                price = r["price"]
                assert price is None or price >= 500
                assert price is None or price <= 2000

    @pytest.mark.asyncio
    async def test_price_min_only(self, _pool):
        """只设最低价"""
        tool = SearchComponent()
        result = await tool.execute(query="", component="cpu", price_min=1000, top_k=5)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_price_max_only(self, _pool):
        """只设最高价"""
        tool = SearchComponent()
        result = await tool.execute(query="", component="cpu", price_max=500, top_k=5)
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_content_truncation(self, _pool):
        """content 不超过 200 字符"""
        tool = SearchComponent()
        result = await tool.execute(query="Intel", component="cpu", top_k=3)
        if result.is_success and result.data["results"]:
            for r in result.data["results"]:
                assert len(r["content"]) <= 203
