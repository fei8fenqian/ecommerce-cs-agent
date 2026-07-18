"""
src/agent/tools_registry.py — 工具注册中心

Agent 的所有"手"都在这里：注册、发现、执行。
Phase 3 写 Agent Loop 之前必须先有这个文件。

三块积木：
ToolResult  — 工具跑完的结果（成功/失败）
ToolSpec    — 工具的说明书（名字/描述/参数/函数）
ToolRegistry — 工具箱（注册/查找/执行）

用法：
registry = ToolRegistry()

@registry.register(
    name="check_stock",
    description="查询商品库存",
    parameters={
        "type": "object",
        "properties": {
            "product_name": {"type": "string", "description": "商品名"},
            "variant": {"type": "string", "description":
"规格，如'32GB+1TB'"},
        },
        "required": ["product_name"],
    },
)
def check_stock(product_name: str, variant: str = "") -> ToolResult:
    # 真正查数据库的逻辑
    return ToolResult(name="check_stock", status="success", data={"stock": 5})
"""

from dataclasses import dataclass, field
from typing import Any, Callable


# =============================================================================
# ToolResult —— 工具执行结果
# =============================================================================
# 不管你调的是什么工具，返回的一定是 ToolResult。
# Agent Loop 不关心工具内部怎么实现的，它只看 ToolResult 的两个字段：
#   status="success" → 用 data 生成回答
#   status="error"   → 用 error 告诉用户"这个功能暂时用不了"
# =============================================================================
# 工具调用结果 (dataclass 是函数，不是类，不能放括号里继承)
@dataclass
class ToolResult:
    # 工具名
    name: str
    # "success" 或 "error"
    status: str
    # 成功时放数据
    data: dict = field(default_factory=dict)  # 每次创建新实例，造一个新的空 dict
    # 失败时放错误信息
    error: str = ""

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    # 转成 LLM 能读的自然语言
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
# ToolSpec —— 工具说明书
# =============================================================================
# 注册工具时填的信息全在这。Agent Loop 不直接调函数，
# 而是通过 ToolSpec 知道：这工具叫啥、干嘛的、要什么参数、怎么调。
# =============================================================================
@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable  # 实际要调用的函数
    parameters: dict[str, Any] = field(default_factory=dict)  # JSON Schema

    def to_openai_function(self) -> dict:
        """
        转成 OpenAI function calling 格式。
        DeepSeek 的 API 兼容这个格式，所以直接用。

        调用方式（Phase 3 写 LLM 调用时会用）：
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[...],
            tools=[spec.to_openai_function() for spec in
            registry.list_tools()],
        )
        """
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

    两个核心方法你后面会频繁用到：
    execute(name, **kwargs) → ToolResult   ← Agent Loop 调这个
    to_openai_schemas() → list[dict]       ← 生成 LLM API 的 tools 参数
    """

    def __init__(self):
        # {工具名: ToolSpec}，用 dict 不用 list 是因为按名字查是 O(1)
        self._tools: dict[str, ToolSpec] = {}

    # 注册工具的装饰器参数
    def register(
        self, *, name: str, description: str, parameters: dict[str, Any] | None = None
    ) -> Callable:
        """
        装饰器：把普通函数注册为工具。

        * 后面的参数必须用关键字传，防止手滑写错位置。

        装饰器做了两件事：
        1. 用 wrapper 包住原函数 → 函数抛异常时自动转 ToolResult(error)
        2. 把 wrapper 存入 self._tools → 后续 execute 调的是包装后的版本
        """
        params = parameters or {}

        # 装饰器接收原函数，并注册
        def decorator(fn: Callable) -> Callable:
            # wrapper 是真正被注册并执行的函数
            # 它包了一层 try/except，保证永远不会把异常抛到 Agent Loop
            def wrapper(**kwargs: Any) -> ToolResult:
                try:
                    result = fn(**kwargs)
                    if isinstance(result, ToolResult):
                        return result
                    return ToolResult(name=name, status="success", data={"result": result})
                except Exception as e:
                    return ToolResult(name=name, status="error", error=str(e))

            # 保存原函数引用（方便测试时直接调原函数，不走 wrapper）
            wrapper.__wrapped__ = fn  # type: ignore[attr-defined]

            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                func=wrapper,  # 存 wrapper，不是原函数
                parameters=params,
            )
            return wrapper

        return decorator

    def get(self, name) -> ToolSpec | None:
        """按名字查工具，查不到返回 None"""
        return self._tools.get(name)

    def list_tools(self) -> list[ToolSpec]:
        """列出所有已注册的工具"""
        return [tool for tool in self._tools.values()]

    def execute(self, name, **kwargs) -> ToolResult:
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

        # 调函数，把返回值包成 ToolResult
        return tool.func(**kwargs)

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

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
