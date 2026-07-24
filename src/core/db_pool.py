"""src/core/db_pool.py — PostgreSQL 线程连接池

解决每次工具调用都 connect + close 的问题（PLAN_V3 已知差距 #3）。
psycopg2 自带 ThreadedConnectionPool，FastAPI 多线程友好。

用法：
    from core.db_pool import get_connection, put_connection, init_pool,
close_pool

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(...)
    finally:
        put_connection(conn)
"""

import logging

from psycopg2 import pool as pg_pool

from config import settings

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None


def init_pool(minconn: int = 2, maxconn: int = 10) -> None:
    """在 lifespan startup 里调用一次"""
    global _pool
    _pool = pg_pool.ThreadedConnectionPool(
        minconn=minconn,
        maxconn=maxconn,
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        dbname=settings.pg_dbname,
    )
    logger.info("连接池已初始化")


def get_connection():
    """从线程池取连接，池未初始化抛异常"""
    if _pool is None:
        raise RuntimeError("连接池未初始化，请先调用 init_pool()")
    return _pool.getconn()


def put_connection(conn) -> None:
    if _pool is not None:
        _pool.putconn(conn)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("连接池已关闭")
