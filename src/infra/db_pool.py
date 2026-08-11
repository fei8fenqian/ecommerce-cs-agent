"""src/core/db_pool.py — PostgreSQL 线程连接池

解决每次工具调用都 connect + close 的问题。
psycopg3 自带 AsyncConnectionPool，异步友好。

用法：
    from infra.db_pool import get_connection, put_connection, init_pool,
close_pool

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(...)
    finally:
        put_connection(conn)
"""

import asyncio
import logging

from psycopg_pool.pool_async import AsyncConnectionPool

from config import settings

logger = logging.getLogger(__name__)

_pool: AsyncConnectionPool | None = None


def get_dsn() -> str:
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


async def init_pool(minconn: int = 4, maxconn: int = 20) -> None:
    """在 lifespan startup 里调用一次（幂等：已初始化则跳过）"""
    global _pool
    if _pool is not None:
        return
    _pool = AsyncConnectionPool(conninfo=get_dsn, min_size=minconn, max_size=maxconn, open=False)
    try:
        await asyncio.wait_for(_pool.open(), timeout=10)
    except Exception:
        _pool = None
        raise
    logger.info("连接池已初始化")


async def get_connection():
    """从线程池取连接，池未初始化抛异常"""
    if _pool is None:
        raise RuntimeError("连接池未初始化，请先调用 init_pool()")
    return await _pool.getconn()


async def put_connection(conn) -> None:
    if _pool is not None:
        await _pool.putconn(conn)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        try:
            await _pool.close()
        except BaseException:
            pass  # psycopg3 worker 清理时可能抛 CancelledError，资源已释放
        _pool = None
        logger.info("连接池已关闭")


async def check_alive() -> bool:
    if _pool is None:
        return False
    try:
        await _pool.check()
        return True
    except Exception:
        return False
