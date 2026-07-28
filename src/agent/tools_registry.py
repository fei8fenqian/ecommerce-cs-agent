"""
src/agent/tools_registry.py — 工具注册中心

Agent 的所有"手"都在这里：注册、发现、执行。
Phase 3 写 Agent Loop 之前必须先有这个文件。

积木：
  ToolResult  — 工具跑完的结果（成功/失败）
  BaseTool    — 工具抽象基类（ABC），新增工具只需写子类
  ToolRegistry — 工具箱（注册/查找/执行）

用法：
  registry = ToolRegistry()

  class CheckStock(BaseTool):
      name = "check_stock"
      description = "查询商品库存"
      parameters = {
          "type": "object",
          "properties": {
              "product_name": {"type": "string", "description": "商品名"},
          },
          "required": ["product_name"],
      }

      def execute(self, product_name: str) -> ToolResult:
          return ToolResult(name=self.name, status="success", data={"stock": 5})

  registry.register(CheckStock())

MCP 扩展：
  未来接 MCP 时，写一个 MCPTool(BaseTool) 包装 MCP 工具定义，
  注册进同一个 Registry，Agent Loop 完全无感。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# =============================================================================
# ToolResult —— 工具执行结果
# =============================================================================
# 不管你调的是什么工具，返回的一定是 ToolResult。
# Agent Loop 不关心工具内部怎么实现的，它只看 ToolResult 的两个字段：
#   status="success" → 用 data 生成回答
#   status="error"   → 用 error 告诉用户"这个功能暂时用不了"
# =============================================================================
@dataclass
class ToolResult:
    name: str  # 工具名
    status: str  # "success" 或 "error"
    data: dict[str, Any] = field(default_factory=dict)  # 成功时放数据
    error: str = ""  # 失败时放错误信息

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    def to_observation(self) -> str:
        """
        把结果转成自然语言，Agent Loop 会把它塞回 LLM 的上下文。

        LLM 读文本比读 JSON 准，所以这里不直接丢 JSON 给它，
        而是格式化成 "[工具名 结果] key1: val1, key2: val2"。
        """
        if self.is_success:
            items = [f"{k}: {v}" for k, v in self.data.items()]
            detail = ", ".join(items)
            return f"[{self.name} 结果] {detail}"
        else:
            return f"[{self.name} 错误] {self.error}"


# =============================================================================
# BaseTool —— 工具抽象基类
# =============================================================================
# 所有工具必须继承 BaseTool，实现 name/description/parameters/execute。
# 面试点：面向接口编程，新增工具只需加一个子类，不改 Registry 和 Loop。
# 以后接 MCP：写 MCPTool(BaseTool) 包装远程工具，注册到同一个 Registry。
# =============================================================================
class BaseTool(ABC):
    """工具基类。每个工具 = 名字 + 描述 + 参数定义 + 执行逻辑"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名，LLM 通过这个名字调用。如 'check_stock'"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述，LLM 靠这个判断什么时候该用哪个工具"""
        ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]:
        """
        参数定义，JSON Schema 格式。
        告诉 LLM：这个工具需要什么参数、什么类型、必填还是可选
        例如: {
            "type": "object",
            "properties": {"product_name": {"type": "string", "description": "商品名"}},
            "required": ["product_name"]
        }
        """
        ...

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """真正执行工具的代码。参数从 kwargs 里取，必须返回 ToolResult"""
        ...

    def to_openai_function(self) -> dict[str, Any]:
        """把工具转成 OpenAI function calling 格式。子类不需要重写。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# =============================================================================
# ToolRegistry —— 工具箱
# =============================================================================
class ToolRegistry:
    """
    管理所有工具的注册、查找、执行。

    两个核心方法：
      execute(name, **kwargs) → ToolResult   ← Agent Loop 调这个
      to_openai_schemas() → list[dict]       ← 生成 LLM API 的 tools 参数
    """

    def __init__(self):
        # {工具名: BaseTool 实例}，用 dict 不用 list 是因为按名字查是 O(1)
        self._tools: dict[str, BaseTool] = {}

    # -- 注册 ----------------------------------------------------------------
    def register(self, tool: BaseTool) -> None:
        """
        注册工具实例。同名工具会报错，防止意外覆盖。
        不是装饰器模式——传工具实例而非函数，更符合面向接口编程。
        """
        if tool.name in self._tools:
            raise ValueError(f"工具 '{tool.name}' 已注册，不允许重复注册")
        self._tools[tool.name] = tool

    # -- 查找 ----------------------------------------------------------------
    def get(self, name: str) -> BaseTool | None:
        """按名字查工具，查不到返回 None"""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """列出所有已注册的工具"""
        return list(self._tools.values())

    # -- 执行 ----------------------------------------------------------------
    async def execute(self, name: str, **kwargs: Any) -> ToolResult:
        """
        Agent Loop 唯一需要调的执行入口。

        传入工具名 + 参数 → 返回 ToolResult。
        永远不抛异常——工具不存在 / 执行出错都包装成 ToolResult(error)。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                name=name,
                status="error",
                error=f"未知工具: {name}。可用: {list(self._tools.keys())}",
            )
        try:
            result = await tool.execute(**kwargs)
            if isinstance(result, ToolResult):
                return result
            return ToolResult(name=name, status="success", data={"result": result})
        except Exception as e:
            return ToolResult(name=name, status="error", error=str(e))

    # -- OpenAI 格式导出 tool schema ------------------------------------------------------
    def to_openai_schemas(self) -> list[dict[str, Any]]:
        """
        生成 OpenAI function calling 的 tools 参数列表。

        Phase 3 里调 LLM 时直接：
          response = client.chat.completions.create(
              model="deepseek-chat",
              messages=messages,
              tools=registry.to_openai_schemas(),
          )
        """
        return [tool.to_openai_function() for tool in self._tools.values()]

    # -- 便捷方法 ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
