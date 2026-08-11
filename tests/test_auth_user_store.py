"""tests/test_auth_user_store.py — user_store 数据访问层测试"""

import pytest
import pytest_asyncio

from infra.db_pool import close_pool, get_connection, init_pool, put_connection
from store.user_store import (
    get_user_by_id,
    get_user_by_username,
    init_user_table,
    insert_users,
    seed_users,
    update_users,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _setup():
    """每个测试前建表 + 清空"""
    await init_pool(minconn=1, maxconn=2)
    await init_user_table()
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM users")
    await put_connection(conn)
    yield
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM users")
    await put_connection(conn)
    await close_pool()


class TestInitTable:
    async def test_init_idempotent(self):
        await init_user_table()
        await init_user_table()

    async def test_table_exists_after_init(self):
        await insert_users("test", "hash123", "customer")
        user = await get_user_by_username("test")
        assert user["username"] == "test"
        assert user["role"] == "customer"


class TestInsertAndGet:
    async def test_insert_and_get_by_username(self):
        await insert_users("alice", "$2b$" + "x" * 57, "admin")
        user = await get_user_by_username("alice")
        assert user["username"] == "alice"
        assert user["role"] == "admin"
        assert "password_hash" in user

    async def test_insert_duplicate_ignored(self):
        """ON CONFLICT DO NOTHING — 第二次 insert 无影响"""
        await insert_users("bob", "hash1", "agent")
        await insert_users("bob", "hash2", "admin")  # 被忽略

        user = await get_user_by_username("bob")
        assert user["role"] == "agent"  # 保持不变

    async def test_get_nonexistent_returns_empty(self):
        assert await get_user_by_username("ghost") == {}
        assert await get_user_by_id(99999) == {}


class TestGetById:
    async def test_get_existing_user(self):
        await insert_users("carol", "hash_c", "operator")
        # 先通过 username 拿到 id，再测 get_user_by_id
        user = await get_user_by_username("carol")
        user2 = await get_user_by_id(user["id"])
        assert user2["username"] == "carol"
        assert user2["role"] == "operator"


class TestUpdateUsers:
    async def test_update_role(self):
        await insert_users("dave", "hash_d", "agent")
        user = await get_user_by_username("dave")
        await update_users(user["id"], {"role": "admin"})

        updated = await get_user_by_username("dave")
        assert updated["role"] == "admin"

    async def test_update_password(self):
        await insert_users("eve", "old_hash", "customer")
        user = await get_user_by_username("eve")
        await update_users(user["id"], {"password_hash": "new_hash"})

        updated = await get_user_by_username("eve")
        assert updated["password_hash"] == "new_hash"


class TestSeedUsers:
    async def test_seed_idempotent(self):
        """seed 两次不报错"""
        await seed_users()
        await seed_users()

    async def test_all_four_users_exist(self):
        await seed_users()
        for name in ("admin", "agent", "operator", "customer"):
            user = await get_user_by_username(name)
            assert user != {}, f"{name} should exist"
            assert user["username"] == name
            assert "password_hash" in user
