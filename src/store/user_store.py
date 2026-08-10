import logging
from typing import Any

from psycopg.sql import SQL, Identifier

from infra.db_pool import get_connection, put_connection

logger = logging.getLogger(__name__)


async def init_table():
    """建表（幂等）"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute("""
            create table if not exists users (
                id serial primary key,
                username unique not null,
                password_hash varchar(60),
                role varchar(32)
            )
        """)
    finally:
        await put_connection(conn)


async def insert_users(username: str, password_hash: str, role: str):
    """新增用户"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute(
            """
            insert into users (
                username, password_hash, role
            )
            values (
                %s, %s, %s
            ) on conflict (username) do nothing
        """,
            (username, password_hash, role),
        )
    finally:
        await put_connection(conn)


async def update_users(id: int, fields: dict[str, Any]):
    """修改用户信息"""
    set_parts: list = []
    values: list = []
    for key, val in fields.items():
        set_parts.append(SQL("{} = %s").format(Identifier(key)))
        values.append(val)
    sql = SQL("update users set {} where id = %s".format(SQL(",").join(set_parts)))
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute(
            sql,
            (*values, id),
        )
    finally:
        await put_connection(conn)


async def get_user_by_username(username: str) -> dict:
    """获取用户信息，用于验证用户信息"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            "select id, username, password_hash, role from users where username = %s",
            (username,),
        )
        row = await cur.fetchone()
        if not row:
            return {}
        return {
            "id": row[0],
            "username": row[1],
            "password_hash": row[2],
            "role": row[3],
        }
    finally:
        await put_connection(conn)
