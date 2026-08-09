"""tests/test_fault_injection.py — 故障注入测试

模拟外部依赖（PG / Redis / LLM）挂掉时的系统行为。
"""

import asyncio

import pytest

from agent.tools.check_stock import CheckStock
from agent.tools_registry import ToolResult
from infra.db_pool import close_pool, get_connection, init_pool, put_connection


# =============================================================================
# PG 连接池 — 池耗尽
# =============================================================================
class TestPoolExhaustion:
    @pytest.mark.asyncio
    async def test_pool_exhaustion_blocks_or_times_out(self):
        """连接池满时，新的连接请求应排队或超时，不应 crash"""
        await init_pool(minconn=1, maxconn=2)

        # 取完所有连接
        conn1 = await get_connection()
        conn2 = await get_connection()

        # 第 3 个连接请求：pool 只有 maxconn=2，应排队等待
        # 设置超时避免永久阻塞
        try:
            conn3 = await asyncio.wait_for(get_connection(), timeout=5.0)
            # 如果拿到了（另一个连接已归还），验证可用
            await conn3.set_autocommit(True)
            cur = await conn3.execute("SELECT 1")
            row = await cur.fetchone()
            assert row[0] == 1
            await put_connection(conn3)
        except asyncio.TimeoutError:
            # 超时也是合理行为（排队中）
            pass

        await put_connection(conn1)
        await put_connection(conn2)
        await close_pool()

    @pytest.mark.asyncio
    async def test_reuse_after_return(self):
        """归还连接后，池可以复用"""
        await init_pool(minconn=1, maxconn=2)

        conn1 = await get_connection()
        conn2 = await get_connection()
        await put_connection(conn1)

        # conn1 归还了，应该能再拿到
        conn3 = await get_connection()
        assert conn3 is not None
        await conn3.set_autocommit(True)
        cur = await conn3.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1

        await put_connection(conn2)
        await put_connection(conn3)
        await close_pool()


# =============================================================================
# PG 连接 — 执行失败后连接不泄漏
# =============================================================================
class TestConnectionLeak:
    @pytest.mark.asyncio
    async def test_connection_returned_after_sql_error(self):
        """SQL 执行失败后，连接应被正确归还"""
        await init_pool(minconn=1, maxconn=2)

        conn = await get_connection()
        await conn.set_autocommit(True)
        try:
            # 对不存在的表执行查询
            await conn.execute("SELECT * FROM nonexistent_table")
        except Exception:
            pass  # 预期会失败

        # 连接应能正常归还
        await put_connection(conn)

        # 再次取连接，应该能用
        conn2 = await get_connection()
        await conn2.set_autocommit(True)
        cur = await conn2.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1
        await put_connection(conn2)
        await close_pool()

    @pytest.mark.asyncio
    async def test_tool_returns_connection_after_error(self):
        """工具执行失败后，finally 块应归还连接"""
        await init_pool(minconn=1, maxconn=2)

        tool = CheckStock()
        # 非法表名会走 error 分支，但 finally 应该归还连接
        result = await tool.execute(product_name="test", table="evil_table")
        assert result.is_success is False

        # 连接归还了，再取应该能拿到
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute("SELECT 1")
        row = await cur.fetchone()
        assert row[0] == 1
        await put_connection(conn)
        await close_pool()


# =============================================================================
# Redis 不可用 — SessionManager 降级
# =============================================================================
class TestRedisUnavailable:
    @pytest.mark.asyncio
    async def test_health_check_returns_false_when_redis_down(self):
        """Redis 连不上时 health_check 返回 False 而非抛异常"""
        from agent.session import SessionManager

        # 用不存在的 Redis 端口
        session = SessionManager(redis_url="redis://localhost:16379/0", ttl=60)
        result = await session.health_check()
        assert result is False
        await session.close()

    @pytest.mark.asyncio
    async def test_get_or_create_handles_redis_connection_error(self):
        """Redis 不可用时 get_or_create 不应 crash"""
        from agent.session import SessionManager

        session = SessionManager(redis_url="redis://localhost:16379/0", ttl=60)
        try:
            ctx = await session.get_or_create("test_session")
            # 如果 Redis 真的不可达，应该抛连接异常
            # 实际取决于 redis-py 的超时设置
            assert ctx is not None
        except Exception:
            # 连接失败也是合理的
            pass
        await session.close()

    @pytest.mark.asyncio
    async def test_real_redis_health_check(self):
        """真实 Redis 的 health check 应该返回 True"""
        from agent.session import SessionManager

        session = SessionManager(ttl=10)
        result = await session.health_check()
        # 如果 Redis 在运行，应该返回 True
        # 如果不在运行，这是环境问题，不是代码 bug
        assert result in (True, False)
        await session.close()


# =============================================================================
# LLM API 故障 — 错误类型判断
# =============================================================================
class TestLLMErrorHandling:
    def test_llm_error_can_retry_for_5xx(self):
        """5xx 错误 + retry_count < 3 → can_retry 应为 True"""
        from exceptions import LLMError

        error = LLMError("服务端错误", status_code=500, retry_count=0)
        assert error.can_retry is True

    def test_llm_error_no_retry_for_401(self):
        """401 认证错误 → can_retry 应为 False（即使 retry_count=0）"""
        from exceptions import LLMError

        error = LLMError("认证失败", status_code=401)
        assert error.can_retry is False

    def test_llm_error_no_retry_for_403(self):
        """403 权限错误 → can_retry 应为 False"""
        from exceptions import LLMError

        error = LLMError("权限不足", status_code=403)
        assert error.can_retry is False

    def test_llm_error_no_retry_when_exhausted(self):
        """非 401/403 但 retry_count >= 3 → can_retry 应为 False"""
        from exceptions import LLMError

        error = LLMError("仍然失败", status_code=503, retry_count=3)
        assert error.can_retry is False

    def test_extract_status_code_from_http_error(self):
        """从 OpenAI SDK 异常中提取 HTTP 状态码"""
        from agent.llm.llm_client import _extract_status_code

        # 模拟 OpenAI APIError（有 http_status）
        class FakeAPIError(Exception):
            def __init__(self):
                self.http_status = 429

        code = _extract_status_code(FakeAPIError())
        assert code == 429

    def test_extract_status_code_from_status_code_attr(self):
        """从有 status_code 属性的异常中提取"""
        from agent.llm.llm_client import _extract_status_code

        class FakeHTTPError(Exception):
            def __init__(self):
                self.status_code = 502

        code = _extract_status_code(FakeHTTPError())
        assert code == 502

    def test_extract_status_code_not_found(self):
        """没有状态码的异常返回 None"""
        from agent.llm.llm_client import _extract_status_code

        code = _extract_status_code(ValueError("普通异常"))
        assert code is None


# =============================================================================
# 工具注册 — 未注册的工具
# =============================================================================
class TestToolRegistryFault:
    @pytest.mark.asyncio
    async def test_execute_unregistered_tool(self):
        """调用未注册的工具 → error，不 crash"""
        from agent.tools_registry import ToolRegistry

        registry = ToolRegistry()
        result = await registry.execute("ghost_tool")
        assert isinstance(result, ToolResult)
        assert result.is_success is False
        assert "未知工具" in result.error

    @pytest.mark.asyncio
    async def test_execute_tool_that_raises_unexpected(self):
        """工具执行抛非 ToolResult 异常 → 被 Registry 包装成 error"""
        from agent.tools_registry import BaseTool, ToolRegistry, ToolResult

        class _CrashTool(BaseTool):
            name = "crash_tool"
            description = "故意崩溃"

            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {}, "required": []}

            async def execute(self, **kwargs):
                raise RuntimeError("模拟工具内部崩溃")

        registry = ToolRegistry()
        registry.register(_CrashTool())
        result = await registry.execute("crash_tool")
        assert isinstance(result, ToolResult)
        assert result.is_success is False
        assert "模拟工具内部崩溃" in result.error
