import logging
from typing import Any, Sequence

from psycopg.sql import SQL, Identifier

from infra.db_pool import get_connection, put_connection
from utils.password_utils import generate_hashed_password

logger = logging.getLogger(__name__)


async def init_user_table():
    """建表（幂等）"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute("""
            create table if not exists users (
                id serial primary key,
                username varchar(64) unique not null,
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
    sql = SQL("update users set {} where id = %s").format(SQL(",").join(set_parts))
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
    """通过用户名获取用户信息，用于验证用户信息"""
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


async def get_user_by_id(user_id: int) -> dict:
    """通过id获取用户信息"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            "select id, username, password_hash, role from users where id = %s",
            (user_id,),
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


async def create_initial_admin(username: str, password: str) -> bool:
    """创建首个管理员；已存在时不覆盖密码。

    Returns:
        True: 创建了新管理员。
        False: 同名管理员已存在，保持原密码不变。
    """
    username = username.strip()
    if not username or not password:
        raise ValueError("管理员用户名和密码不能为空")

    password_hash = generate_hashed_password(password).decode()
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            """
            INSERT INTO public.users (username, password_hash, role)
            VALUES (%s, %s, 'admin')
            ON CONFLICT (username) DO NOTHING
            RETURNING id
            """,
            (username, password_hash),
        )
        row = await cursor.fetchone()
        if row is not None:
            logger.info("首个管理员创建成功")
            return True

        cursor = await conn.execute(
            "SELECT role FROM public.users WHERE username = %s",
            (username,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise RuntimeError("管理员初始化失败：用户状态未知")
        if existing[0] != "admin":
            raise ValueError(f"用户名 {username!r} 已被非管理员账号占用")
        logger.info("管理员已存在，保持原密码不变")
        return False
    finally:
        if conn is not None:
            await put_connection(conn)


async def seed_users(users: Sequence[tuple[str, str, str]]) -> None:
    """显式插入开发/测试 demo 用户，不覆盖已存在账号。

    密码由调用方从环境变量注入，不能在此函数或源码中写默认密码。
    """
    if not users:
        raise ValueError("demo 用户列表不能为空")
    for username, password, role in users:
        if not username or not password or not role:
            raise ValueError("demo 用户的用户名、密码和角色都不能为空")
        password_hash = generate_hashed_password(password).decode()
        await insert_users(username, password_hash, role)
