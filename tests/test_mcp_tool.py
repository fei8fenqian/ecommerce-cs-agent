import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.mcp_tool import (
    MCPClientManager,
    MCPTool,
    MCPToolError,
    MCPUnavailableError,
)
from agent.tools_registry import ToolResult
from infra.circuit_breaker import CircuitBreaker, CircuitState


def mcp_result(text: str = '{"ok": true}', *, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        isError=is_error,
        content=[SimpleNamespace(text=text)],
    )


class _AsyncContext:
    def __init__(self, value=None, exit_error: Exception | None = None):
        self.value = value
        self.exit_error = exit_error

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, tb):
        if self.exit_error:
            raise self.exit_error
        return False


@pytest.mark.asyncio
async def test_connect_timeout_is_converted_to_safe_error():
    class SlowContext(_AsyncContext):
        async def __aenter__(self):
            await asyncio.sleep(0.05)
            return (object(), object())

    manager = MCPClientManager("https://mcp.example/sse", connect_timeout_seconds=0.01)
    with patch("agent.mcp_tool.sse_client", return_value=SlowContext()):
        with pytest.raises(MCPUnavailableError) as raised:
            await manager.connect()

    assert str(raised.value) == "MCP 工具暂时不可用"
    assert "mcp.example" not in str(raised.value)
    assert manager._session is None


@pytest.mark.asyncio
async def test_connect_and_initialize_success():
    sse_context = _AsyncContext((object(), object()))
    session = MagicMock()
    session.initialize = AsyncMock()
    session_context = _AsyncContext(session)

    with (
        patch("agent.mcp_tool.sse_client", return_value=sse_context),
        patch("agent.mcp_tool.ClientSession", return_value=session_context),
    ):
        manager = MCPClientManager("https://mcp.example/sse")
        await manager.connect()

    session.initialize.assert_awaited_once()
    assert manager._session is session
    assert manager._circuit_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_list_tools_timeout_is_safe():
    manager = MCPClientManager("https://mcp.example/sse", list_tools_timeout_seconds=0.01)
    session = MagicMock()

    async def slow_list_tools():
        await asyncio.sleep(0.05)

    session.list_tools = slow_list_tools
    manager._session = session

    with pytest.raises(MCPUnavailableError) as raised:
        await manager.list_tools()

    assert str(raised.value) == "MCP 工具暂时不可用"


@pytest.mark.asyncio
async def test_call_tool_timeout_returns_safe_error_and_counts_failure():
    breaker = CircuitBreaker(failure_threshold=2)
    manager = MCPClientManager(
        "https://mcp.example/sse",
        call_timeout_seconds=0.01,
        circuit_breaker=breaker,
    )
    session = MagicMock()

    async def slow_call(name, arguments):
        await asyncio.sleep(0.05)

    session.call_tool = slow_call
    manager._session = session

    with pytest.raises(MCPUnavailableError):
        await manager.call_tool("check", {})

    assert breaker.failure_count == 1


@pytest.mark.asyncio
async def test_open_circuit_does_not_call_mcp_and_recovers_after_cooldown():
    now = 0.0
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=10, clock=lambda: now)
    manager = MCPClientManager("https://mcp.example/sse", circuit_breaker=breaker)
    session = MagicMock()
    session.call_tool = AsyncMock(side_effect=[asyncio.TimeoutError(), mcp_result()])
    manager._session = session

    with pytest.raises(MCPUnavailableError):
        await manager.call_tool("check", {})
    with pytest.raises(MCPUnavailableError):
        await manager.call_tool("check", {})
    assert session.call_tool.await_count == 1
    assert breaker.state == CircuitState.OPEN

    now = 10.0
    result = await manager.call_tool("check", {})
    assert result == '{"ok": true}'
    assert session.call_tool.await_count == 2
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_mcp_tool_never_returns_success_for_remote_error_or_invalid_json(caplog):
    manager = MagicMock()
    manager.call_tool = AsyncMock(side_effect=[MCPToolError("remote secret"), "not-json"])
    tool = MCPTool(
        manager,
        {"name": "check", "description": "check", "inputSchema": {"type": "object"}},
    )

    remote_error = await tool.execute()
    invalid_json = await tool.execute()

    assert isinstance(remote_error, ToolResult)
    assert remote_error.status == "error"
    assert "remote secret" not in remote_error.error
    assert invalid_json.status == "error"
    assert "not-json" not in invalid_json.error
    assert "remote secret" not in caplog.text


@pytest.mark.asyncio
async def test_mcp_tool_hides_raw_dependency_exception():
    manager = MagicMock()
    manager.call_tool = AsyncMock(side_effect=RuntimeError("url=https://mcp?token=secret"))
    tool = MCPTool(
        manager,
        {"name": "check", "description": "check", "inputSchema": {"type": "object"}},
    )

    result = await tool.execute()

    assert result.status == "error"
    assert result.error == "MCP 工具暂时不可用"
    assert "secret" not in result.error


@pytest.mark.asyncio
async def test_disconnect_handles_half_initialized_contexts():
    manager = MCPClientManager("https://mcp.example/sse")
    manager._session_ctx = _AsyncContext(exit_error=RuntimeError("session secret"))
    manager._sse_ctx = _AsyncContext(exit_error=RuntimeError("transport secret"))
    manager._session = object()
    manager._read = object()
    manager._write = object()

    await manager.disconnect()

    assert manager._session is None
    assert manager._session_ctx is None
    assert manager._sse_ctx is None
    assert manager._read is None
    assert manager._write is None


@pytest.mark.asyncio
async def test_lifespan_skips_failed_mcp_server(monkeypatch):
    import tiktoken

    with patch.object(tiktoken, "get_encoding", return_value=MagicMock()):
        import main as app_main

    class FakeManager:
        instances = []

        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.disconnected = False
            self.__class__.instances.append(self)

        async def connect(self):
            if self.url == "bad-server":
                raise MCPUnavailableError("MCP 工具暂时不可用")

        async def list_tools(self):
            return [
                {
                    "name": "check",
                    "description": "check",
                    "inputSchema": {"type": "object"},
                }
            ]

        async def disconnect(self):
            self.disconnected = True

        async def call_tool(self, name, arguments):
            return '{"ok": true}'

    monkeypatch.setattr(app_main.settings, "mcp_servers", ["bad-server", "good-server"])
    monkeypatch.setattr(app_main, "MCPClientManager", FakeManager)
    monkeypatch.setattr(app_main, "setup_logging", MagicMock())
    monkeypatch.setattr(app_main, "init_pool", AsyncMock())
    monkeypatch.setattr(app_main, "close_pool", AsyncMock())
    monkeypatch.setattr(app_main, "init_redis", MagicMock())
    monkeypatch.setattr(app_main, "close_redis", AsyncMock())
    monkeypatch.setattr(app_main, "health_check", AsyncMock())
    monkeypatch.setattr(app_main, "_seed_demo_users_if_enabled", AsyncMock())
    monkeypatch.setattr(app_main, "init_casbin", MagicMock())
    monkeypatch.setattr(app_main, "LLMClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app_main, "IntentRouter", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app_main, "PlanAndExecuteAgent", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app_main, "AgentLoop", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(app_main, "SessionManager", MagicMock(return_value=MagicMock()))

    async with app_main.lifespan(app_main.app):
        managers = app_main.app.state.mcp_managers
        assert [manager.url for manager in managers] == ["good-server"]
        assert FakeManager.instances[0].disconnected is True

    assert FakeManager.instances[-1].disconnected is True
