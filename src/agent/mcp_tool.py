"""MCP Client — 让 Agent 通过 MCP 协议调用远程服务。

架构角色：
    Agent (客服) ──MCP──→ Payment Server (支付团队)
                ──MCP──→ Logistics Server (物流团队)

一个 MCPClientManager 管一个 MCP Server 的连接生命周期。
一个 MCPTool 包装一个远程 tool，实现 BaseTool 接口，注册进 ToolRegistry。
"""

import json
import logging

from mcp.client.session import ClientSession
from mcp.client.sse import sse_client

from agent.tools_registry import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class MCPClientManager:
    """管理与一个 MCP Server 的连接。

    生命周期：
        manager = MCPClientManager("http://host:port/mcp/sse")
        await manager.connect()           # 建立 SSE 长连接 + 初始化会话
        tools = await manager.list_tools()  # 获取该 server 暴露的所有工具
        result = await manager.call_tool("check_payment", {"order_id": "..."})
        await manager.disconnect()        # 关闭连接

    一个 server 一个 manager。多 server 就多个 manager 实例。
    """

    def __init__(self, url: str):
        # MCP Server 的 SSE 端点，如 "http://localhost:8000/mcp/sse"
        self._url = url
        # sse_client 上下文
        self._sse_ctx = None
        # MCP 协议会话上下文
        self._session_ctx = None
        # SSE 连接的两个方向：_read 接收 server 推送，_write 发送请求
        # 类型是 mcp 库内部的 MemoryObjectStream，我们不直接操作
        self._read: object | None = None
        self._write: object | None = None
        # MCP 协议会话，所有操作（list_tools / call_tool）都通过它
        self._session: ClientSession | None = None

    async def connect(self):
        """建立与 MCP Server 的连接。

        1. sse_client(url) 打开 SSE 长连接，返回 (read_stream, write_stream)
        2. ClientSession(read, write) 包装成 MCP 协议会话
        3. session.initialize() 握手 —— 交换协议版本和能力
        """
        # 创建 sse 上下文管理器
        self._sse_ctx = sse_client(self._url)
        # 建 TCP 连接、开 SSE 流、返回 read 和 write
        self._read, self._write = await self._sse_ctx.__aenter__()

        self._session_ctx = ClientSession(self._read, self._write)
        self._session = await self._session_ctx.__aenter__()

        await self._session.initialize()

    async def list_tools(self) -> list[dict]:
        """获取 MCP Server 暴露的所有工具列表。

        返回格式（MCP 协议标准）：
        [
            {
                "name": "check_payment",
                "description": "查询订单支付状态...",
                "inputSchema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"]
                }
            },
            ...
        ]

        这个返回的 dict 可以直接喂给 MCPTool.__init__ 的 tool_info 参数。
        """
        assert self._session is not None, "connect() 必须先于 list_tools() 调用"
        tool_res = await self._session.list_tools()
        tools = tool_res.tools
        res: list[dict] = []
        for tool in tools:
            res.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                },
            )
        return res

    async def call_tool(self, name: str, arguments: dict) -> str:
        """调用 MCP Server 上的一个工具，返回结果文本。

        Args:
            name: 工具名，如 "check_payment"
            arguments: 工具参数，如 {"order_id": "ORD123"}

        Returns:
            MCP 返回的 content[0].text，通常是 JSON 字符串
            Agent 拿到后解析成 dict 放进 ToolResult.data
        """
        assert self._session is not None, "connect() 必须先于 call_tool() 调用"
        call_tool_res = await self._session.call_tool(name, arguments)
        return getattr(call_tool_res.content[0], "text", str(call_tool_res.content[0]))

    async def disconnect(self):
        """关闭 SSE 连接，释放资源。在 FastAPI shutdown 时调用。"""
        await self._session_ctx.__aexit__(None, None, None)
        await self._sse_ctx.__aexit__(None, None, None)


class MCPTool(BaseTool):
    """把 MCP 远程工具包装成 BaseTool，让 Agent 的 ToolRegistry 能统一管理。

    execute() 是 async 的 —— BaseTool 已改为 async 接口，
    MCPTool 直接用 await 调 MCP，不需要 asyncio.run() 桥接。
    """

    def __init__(self, manager: MCPClientManager, tool_info: dict):
        # 持有 manager 引用，execute 时通过它发起 MCP 调用
        self._manager = manager
        # 以下三个字段从 MCP Server 的 list_tools 返回值中直接取
        # tool_info = {"name": "...", "description": "...", "inputSchema": {...}}
        self._name: str = tool_info["name"]
        self._description: str = tool_info["description"]
        # inputSchema 就是 OpenAI function-calling 格式的 parameters
        self._parameters: dict = tool_info["inputSchema"]

    @property
    def name(self) -> str:
        """工具名，对应 MCP Server 端的 @mcp.tool() 名字"""
        return self._name

    @property
    def description(self) -> str:
        """工具描述，LLM 根据它决定是否调用"""
        return self._description

    @property
    def parameters(self) -> dict:
        """参数 schema，OpenAI function-calling 格式，LLM 据此填参数"""
        return self._parameters

    async def execute(self, **kwargs) -> ToolResult:
        """执行 MCP 工具调用。直接 await MCP 异步调用，无需 asyncio.run() 桥接。"""
        try:
            result_text = await self._manager.call_tool(self._name, kwargs)
            return ToolResult(
                name=self._name,
                status="success",
                data=json.loads(result_text),
            )
        except Exception as e:
            logger.exception("MCPTool %s 执行失败", self._name)
            return ToolResult(
                name=self._name,
                status="error",
                error=str(e),
            )
