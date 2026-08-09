"""tests/test_tools_registry.py — 工具注册中心单元测试"""

import pytest

from agent.tools_registry import BaseTool, ToolRegistry, ToolResult


# =============================================================================
# 假工具 — 测试用
# =============================================================================
class _FakeSuccess(BaseTool):
    """总是成功的工具"""

    @property
    def name(self) -> str:
        return "fake_success"

    @property
    def description(self) -> str:
        return "always succeeds"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"input": {"type": "string"}},
            "required": ["input"],
        }

    async def execute(self, **kwargs):
        return ToolResult(name=self.name, status="success", data={"echo": kwargs})


class _FakeFail(BaseTool):
    """总是抛异常的工具"""

    @property
    def name(self) -> str:
        return "fake_fail"

    @property
    def description(self) -> str:
        return "always fails"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        raise RuntimeError("boom")


class _FakeNoResult(BaseTool):
    """返回非 ToolResult 类型的工具（Registry 应自动包装）"""

    @property
    def name(self) -> str:
        return "fake_no_result"

    @property
    def description(self) -> str:
        return "returns raw str"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return "raw string"


# =============================================================================
# ToolResult 单元测试
# =============================================================================
class TestToolResult:
    def test_success(self):
        r = ToolResult(name="t", status="success", data={"x": 1})
        assert r.is_success is True
        assert "x: 1" in r.to_observation()

    def test_error(self):
        r = ToolResult(name="t", status="error", error="broken")
        assert r.is_success is False
        assert "broken" in r.to_observation()

    def test_observation_default_status(self):
        r = ToolResult(name="t", status="whatever")
        assert r.is_success is False

    def test_empty_data(self):
        r = ToolResult(name="t", status="success")
        assert r.is_success is True
        assert r.to_observation() == "[t 结果] "


# =============================================================================
# BaseTool.to_openai_function 测试
# =============================================================================
class TestBaseToolOpenAISchema:
    def test_returns_correct_structure(self):
        tool = _FakeSuccess()
        schema = tool.to_openai_function()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "fake_success"
        assert schema["function"]["description"] == "always succeeds"
        assert schema["function"]["parameters"]["properties"]["input"]["type"] == "string"
        assert "input" in schema["function"]["parameters"]["required"]


# =============================================================================
# ToolRegistry 单元测试
# =============================================================================
class TestToolRegistry:
    @pytest.fixture
    def registry(self):
        r = ToolRegistry()
        r.register(_FakeSuccess())
        return r

    # -- register -------------------------------------------------------------
    def test_register(self, registry):
        assert len(registry) == 1
        assert "fake_success" in registry

    def test_register_duplicate_raises(self, registry):
        with pytest.raises(ValueError, match="已注册"):
            registry.register(_FakeSuccess())

    def test_register_multiple(self):
        registry = ToolRegistry()
        registry.register(_FakeSuccess())
        registry.register(_FakeFail())
        registry.register(_FakeNoResult())
        assert len(registry) == 3

    # -- get ------------------------------------------------------------------
    def test_get_existing(self, registry):
        tool = registry.get("fake_success")
        assert tool is not None
        assert tool.name == "fake_success"

    def test_get_missing_returns_none(self, registry):
        assert registry.get("nobody") is None

    # -- execute --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_execute_success(self, registry):
        result = await registry.execute("fake_success", input="hello")
        assert result.is_success is True
        assert result.data["echo"]["input"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_missing_tool(self):
        registry = ToolRegistry()
        result = await registry.execute("nobody")
        assert result.is_success is False
        assert "未知工具" in result.error

    @pytest.mark.asyncio
    async def test_execute_tool_raises(self):
        registry = ToolRegistry()
        registry.register(_FakeFail())
        result = await registry.execute("fake_fail")
        assert result.is_success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_execute_non_toolresult_wrapped(self):
        registry = ToolRegistry()
        registry.register(_FakeNoResult())
        result = await registry.execute("fake_no_result")
        assert result.is_success is True
        assert result.data["result"] == "raw string"

    # -- to_openai_schemas ----------------------------------------------------
    def test_to_openai_schemas_empty(self):
        registry = ToolRegistry()
        assert registry.to_openai_schemas() == []

    def test_to_openai_schemas_two_tools(self):
        registry = ToolRegistry()
        registry.register(_FakeSuccess())
        registry.register(_FakeFail())
        schemas = registry.to_openai_schemas()
        assert len(schemas) == 2
        names = [s["function"]["name"] for s in schemas]
        assert set(names) == {"fake_success", "fake_fail"}

    # -- list_tools -----------------------------------------------------------
    def test_list_tools(self):
        registry = ToolRegistry()
        registry.register(_FakeSuccess())
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "fake_success"
