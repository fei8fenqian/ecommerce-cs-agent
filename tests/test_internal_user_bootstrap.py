from unittest.mock import AsyncMock, patch

import pytest

from store.user_store import create_initial_internal_user


@pytest.mark.asyncio
async def test_create_internal_user_hashes_password_and_uses_role():
    insert_cursor = AsyncMock()
    insert_cursor.fetchone.return_value = (101,)
    connection = AsyncMock()
    connection.execute.return_value = insert_cursor

    with (
        patch("store.user_store.get_connection", AsyncMock(return_value=connection)),
        patch("store.user_store.put_connection", AsyncMock()),
        patch("store.user_store.generate_hashed_password", return_value=b"hashed"),
    ):
        created = await create_initial_internal_user("finance-demo", "secret", "finance")

    assert created is True
    assert connection.execute.call_args.args[1] == ("finance-demo", "hashed", "finance")


@pytest.mark.asyncio
async def test_existing_same_role_does_not_replace_password():
    insert_cursor = AsyncMock()
    insert_cursor.fetchone.return_value = None
    lookup_cursor = AsyncMock()
    lookup_cursor.fetchone.return_value = ("finance",)
    connection = AsyncMock()
    connection.execute.side_effect = [insert_cursor, lookup_cursor]

    with (
        patch("store.user_store.get_connection", AsyncMock(return_value=connection)),
        patch("store.user_store.put_connection", AsyncMock()),
        patch("store.user_store.generate_hashed_password", return_value=b"new-hash"),
    ):
        created = await create_initial_internal_user("finance-demo", "secret", "finance")

    assert created is False


@pytest.mark.asyncio
async def test_existing_other_role_is_rejected():
    insert_cursor = AsyncMock()
    insert_cursor.fetchone.return_value = None
    lookup_cursor = AsyncMock()
    lookup_cursor.fetchone.return_value = ("customer",)
    connection = AsyncMock()
    connection.execute.side_effect = [insert_cursor, lookup_cursor]

    with (
        patch("store.user_store.get_connection", AsyncMock(return_value=connection)),
        patch("store.user_store.put_connection", AsyncMock()),
        patch("store.user_store.generate_hashed_password", return_value=b"new-hash"),
    ):
        with pytest.raises(ValueError, match="其他角色"):
            await create_initial_internal_user("finance-demo", "secret", "finance")


@pytest.mark.asyncio
async def test_unknown_internal_role_is_rejected_before_database_access():
    get_connection = AsyncMock()
    with patch("store.user_store.get_connection", get_connection):
        with pytest.raises(ValueError, match="不允许初始化"):
            await create_initial_internal_user("demo", "secret", "customer")
    get_connection.assert_not_awaited()
