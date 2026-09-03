"""
src/agent/tools_registry.py — 工具注册中心

Agent 的所有工具都在这里：注册、发现、执行。

积木：
  ToolResult  — 工具执行结果（成功/失败）
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

from agent.support_control import extract_decision_context, extract_decision_facts

_SUBJECT_BOUND_TOOL_NAMES = frozenset(
    {
        "track_order",
        "query_refund_status",
        "check_refund_eligibility",
        "check_payment_status",
        "check_after_sales",
    }
)


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
    # Registry 在工具边界统一填充；调用方不需要按具体工具名重新解析 data。
    decision_facts: dict[str, Any] = field(default_factory=dict)
    # 每组事实保留可信订单 subject 和 current/historical provenance。
    decision_contexts: list[dict[str, Any]] = field(default_factory=list)

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


@dataclass(frozen=True)
class ToolContext:
    """由服务端注入的工具调用上下文，不暴露给大模型。"""

    user_id: int
    role: str
    # 同一用户在不同业务入口拥有不同的临时能力。默认不限制，以保持内部确定性
    # 服务和旧调用兼容；客户聊天入口会显式阻止模型直接调用高风险工具。
    blocked_tools: frozenset[str] = field(default_factory=frozenset)
    # 若提供则是本次调用的能力上限。以后新增退款、改址、取消订单等工具时，未加入
    # 白名单的工具默认不可见且不可执行。
    allowed_tools: frozenset[str] | None = None
    # 仅由服务端聊天编排注入；模型和客户端不能修改工单初始队列。
    ticket_queue_status: str | None = None
    # 当前 Support Case 已经确定的订单 subject。只由服务端注入，Registry 会把它绑定到
    # 订单读取工具，拒绝模型改查另一笔订单。
    selected_order_id: str | None = None
    # Customer-support operator calls may discover orders with track_order,
    # but cannot turn an order id observed in that result into a subject by
    # directly querying it.  The workflow's controlled subject resolver must
    # bind it first.
    require_bound_subject: bool = False


# =============================================================================
# BaseTool —— 工具抽象基类
# =============================================================================
# 所有工具必须继承 BaseTool，实现 name/description/parameters/execute。
# 新增工具只需加一个子类，不改 Registry 和 Loop。
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

    @property
    def requires_tool_context(self) -> bool:
        """工具是否必须由服务端提供当前用户上下文。"""
        return False

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
    async def execute(
        self,
        name: str,
        *,
        tool_context: ToolContext | None = None,
        **kwargs: Any,
    ) -> ToolResult:
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
        if tool.requires_tool_context and tool_context is None:
            return ToolResult(
                name=name,
                status="error",
                error="缺少当前用户身份，无法执行该工具",
            )
        if tool_context is not None and name in tool_context.blocked_tools:
            return ToolResult(
                name=name,
                status="error",
                error="当前流程未授予调用该工具的权限",
            )
        if (
            tool_context is not None
            and tool_context.allowed_tools is not None
            and name not in tool_context.allowed_tools
        ):
            return ToolResult(
                name=name,
                status="error",
                error="当前流程未授予调用该工具的权限",
            )
        if (
            tool_context is not None
            and tool_context.require_bound_subject
            and not tool_context.selected_order_id
            and name in _SUBJECT_BOUND_TOOL_NAMES - {"track_order"}
        ):
            return ToolResult(name=name, status="error", error="subject_not_bound")
        if (
            tool_context is not None
            and tool_context.selected_order_id
            and name
            in {
                "track_order",
                "query_refund_status",
                "check_refund_eligibility",
                "check_payment_status",
                "check_after_sales",
            }
        ):
            bound_order_id = tool_context.selected_order_id
            requested_order_id = kwargs.get("order_id")
            if requested_order_id not in (None, "", bound_order_id):
                return ToolResult(
                    name=name,
                    status="error",
                    error="当前 Case 已绑定其他订单，不能改查未选择的订单",
                )
            kwargs["order_id"] = bound_order_id
        try:
            if tool.requires_tool_context:
                kwargs["tool_context"] = tool_context
            result = await tool.execute(**kwargs)
            if isinstance(result, ToolResult):
                if result.is_success:
                    result.decision_facts = extract_decision_facts(result.name, result.data)
                    context = extract_decision_context(
                        result.name,
                        result.data,
                        requested_order_id=kwargs.get("order_id"),
                    )
                    if (
                        result.name in _SUBJECT_BOUND_TOOL_NAMES
                        and isinstance(kwargs.get("order_id"), str)
                        and kwargs["order_id"].startswith("SO")
                        and result.decision_facts
                        and context is None
                    ):
                        return ToolResult(
                            name=result.name,
                            status="error",
                            error="工具返回的订单主体无法与当前查询订单核验一致",
                        )
                    result.decision_contexts = [context] if context else []
                return result
            wrapped = ToolResult(name=name, status="success", data={"result": result})
            wrapped.decision_facts = extract_decision_facts(wrapped.name, wrapped.data)
            context = extract_decision_context(
                wrapped.name,
                wrapped.data,
                requested_order_id=kwargs.get("order_id"),
            )
            if (
                name in _SUBJECT_BOUND_TOOL_NAMES
                and isinstance(kwargs.get("order_id"), str)
                and kwargs["order_id"].startswith("SO")
                and wrapped.decision_facts
                and context is None
            ):
                return ToolResult(
                    name=name,
                    status="error",
                    error="工具返回的订单主体无法与当前查询订单核验一致",
                )
            wrapped.decision_contexts = [context] if context else []
            return wrapped
        except Exception as e:
            return ToolResult(name=name, status="error", error=str(e))

    # -- OpenAI 格式导出 tool schema ------------------------------------------------------
    def to_openai_schemas(self, tool_context: ToolContext | None = None) -> list[dict[str, Any]]:
        """
        生成 OpenAI function calling 的 tools 参数列表。

        调用方式：
          response = client.chat.completions.create(
              model="deepseek-chat",
              messages=messages,
              tools=registry.to_openai_schemas(),
          )
        """
        blocked_tools = tool_context.blocked_tools if tool_context is not None else frozenset()
        allowed_tools = tool_context.allowed_tools if tool_context is not None else None
        return [
            tool.to_openai_function()
            for tool in self._tools.values()
            if tool.name not in blocked_tools and (allowed_tools is None or tool.name in allowed_tools)
        ]

    # -- 便捷方法 ------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
