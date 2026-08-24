"""历史商品表差异的目录查询回归测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from store.product_catalog_store import list_products


class _EmptyCursor:
    """没有数据但可供 async for 消费的游标。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_phone_catalog_uses_display_type_literal_not_missing_column() -> None:
    """phone_products 没有 product_type，目录查询必须使用展示别名。"""
    connection = type("Connection", (), {"execute": AsyncMock(return_value=_EmptyCursor())})()

    with (
        patch("store.product_catalog_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.product_catalog_store.put_connection", new=AsyncMock()),
    ):
        assert await list_products("phones") == []

    sql = str(connection.execute.await_args.args[0])
    assert "'手机' AS product_type" in sql


@pytest.mark.asyncio
async def test_laptop_catalog_keeps_its_native_product_type() -> None:
    """笔记本目录仍读取自身的具体产品类型。"""
    connection = type("Connection", (), {"execute": AsyncMock(return_value=_EmptyCursor())})()

    with (
        patch("store.product_catalog_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.product_catalog_store.put_connection", new=AsyncMock()),
    ):
        assert await list_products("laptops") == []

    sql = str(connection.execute.await_args.args[0])
    assert "description, product_type AS product_type" in sql
