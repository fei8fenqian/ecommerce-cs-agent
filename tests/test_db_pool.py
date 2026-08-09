"""tests/test_db_pool.py — 数据库连接池单元测试"""

import pytest
from psycopg import AsyncConnection

from infra.db_pool import close_pool, get_connection, get_dsn, init_pool, put_connection


# =============================================================================
# get_dsn — DSN 生成
# =============================================================================
class TestGetDSN:
    def test_returns_string(self):
        dsn = get_dsn()
        assert isinstance(dsn, str)
        assert len(dsn) > 0

    def test_contains_host_and_port(self):
        dsn = get_dsn()
        assert "host=" in dsn
        assert "port=" in dsn
        assert "dbname=" in dsn
        assert "user=" in dsn
        assert "password=" in dsn

    def test_no_newlines(self):
        """DSN 里不能有换行符，否则 psycopg 连接报错"""
        dsn = get_dsn()
        assert "\n" not in dsn
        assert "\r" not in dsn


# =============================================================================
# init_pool / get_connection / put_connection / close_pool
# =============================================================================
class TestPoolLifecycle:
    @pytest.mark.asyncio
    async def test_init_and_close(self):
        """初始化连接池 → 关闭连接池"""
        await init_pool(minconn=1, maxconn=2)
        await close_pool()

    @pytest.mark.asyncio
    async def test_init_twice_no_error(self):
        """连续 init 两次不应抛异常（覆盖全局变量）"""
        await init_pool(minconn=1, maxconn=2)
        await init_pool(minconn=1, maxconn=2)
        await close_pool()

    @pytest.mark.asyncio
    async def test_get_connection_returns_async_connection(self):
        """get_connection 返回 psycopg AsyncConnection"""
        await init_pool(minconn=1, maxconn=2)
        conn = await get_connection()
        assert isinstance(conn, AsyncConnection)
        await put_connection(conn)
        await close_pool()

    @pytest.mark.asyncio
    async def test_get_connection_can_execute_sql(self):
        """从连接池拿到的连接能正常执行 SQL"""
        await init_pool(minconn=1, maxconn=2)
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute("SELECT 1 AS num")
        row = await cur.fetchone()
        assert row[0] == 1
        await put_connection(conn)
        await close_pool()

    @pytest.mark.asyncio
    async def test_multiple_connections(self):
        """并发取还多个连接"""
        await init_pool(minconn=1, maxconn=4)
        conns = []
        for _ in range(3):
            conns.append(await get_connection())

        for c in conns:
            await c.set_autocommit(True)
            cur = await c.execute("SELECT 1")
            row = await cur.fetchone()
            assert row[0] == 1

        for c in conns:
            await put_connection(c)
        await close_pool()


# =============================================================================
# 异常路径
# =============================================================================
class TestPoolErrors:
    @pytest.mark.asyncio
    async def test_get_connection_without_init_raises(self):
        """没 init_pool 直接 get_connection → RuntimeError"""
        # 确保池是空的
        await close_pool()
        with pytest.raises(RuntimeError, match="连接池未初始化"):
            await get_connection()

    @pytest.mark.asyncio
    async def test_put_connection_when_pool_none_does_not_crash(self):
        """池为 None 时 put_connection 静默成功（不抛异常）"""
        await close_pool()
        # 不应抛异常
        await put_connection(None)

    @pytest.mark.asyncio
    async def test_close_pool_when_already_closed(self):
        """重复 close 不抛异常"""
        await close_pool()
        await close_pool()


# =============================================================================
# 自定义参数
# =============================================================================
class TestPoolCustomParams:
    @pytest.mark.asyncio
    async def test_custom_min_max(self):
        """自定义 minconn / maxconn"""
        await init_pool(minconn=1, maxconn=3)
        conn = await get_connection()
        assert isinstance(conn, AsyncConnection)
        await put_connection(conn)
        await close_pool()
