"""客户购物车的数据访问层；不读取或写入 legacy orders。"""

from dataclasses import dataclass
from uuid import uuid4

from infra.db_pool import get_connection, put_connection
from store.checkout_store import CheckoutCategory


@dataclass(frozen=True)
class StoredCartItem:
    """购物车中尚未结算的一项商品引用。"""

    item_id: int
    category: CheckoutCategory
    product_id: str
    quantity: int


async def list_cart_items(customer_user_id: int) -> list[StoredCartItem]:
    """读取客户自己的购物车条目，按加入顺序返回。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT i.id, i.catalog_category, i.catalog_product_id, i.quantity
            FROM carts AS c
            JOIN cart_items AS i ON i.cart_id = c.id
            WHERE c.customer_user_id = %s
            ORDER BY i.created_at, i.id
            """,
            (customer_user_id,),
        )
        return [
            StoredCartItem(item_id=int(row[0]), category=row[1], product_id=str(row[2]), quantity=int(row[3]))
            for row in await cursor.fetchall()
        ]
    finally:
        await put_connection(connection)


async def find_cart_item(
    customer_user_id: int,
    category: CheckoutCategory,
    product_id: str,
) -> StoredCartItem | None:
    """读取一项现有购物车条目，用于服务层计算增量数量。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT i.id, i.catalog_category, i.catalog_product_id, i.quantity
            FROM carts AS c
            JOIN cart_items AS i ON i.cart_id = c.id
            WHERE c.customer_user_id = %s AND i.catalog_category = %s AND i.catalog_product_id = %s
            """,
            (customer_user_id, category, product_id),
        )
        row = await cursor.fetchone()
        return None if row is None else StoredCartItem(int(row[0]), row[1], str(row[2]), int(row[3]))
    finally:
        await put_connection(connection)


async def put_cart_item(
    customer_user_id: int,
    category: CheckoutCategory,
    product_id: str,
    quantity: int,
) -> int:
    """创建或更新客户的一项购物车条目，并返回其稳定条目 ID。"""
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            INSERT INTO carts (id, customer_user_id) VALUES (%s, %s)
            ON CONFLICT (customer_user_id) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            (uuid4(), customer_user_id),
        )
        cart_id = (await cursor.fetchone())[0]
        cursor = await connection.execute(
            """
            INSERT INTO cart_items (cart_id, catalog_category, catalog_product_id, quantity)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (cart_id, catalog_category, catalog_product_id)
            DO UPDATE SET quantity = EXCLUDED.quantity, updated_at = NOW()
            RETURNING id
            """,
            (cart_id, category, product_id, quantity),
        )
        item_id = int((await cursor.fetchone())[0])
        await connection.commit()
        return item_id
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def remove_cart_item(customer_user_id: int, item_id: int) -> bool:
    """删除客户自己的单个购物车条目，返回是否实际删除。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            DELETE FROM cart_items AS i
            USING carts AS c
            WHERE i.cart_id = c.id AND c.customer_user_id = %s AND i.id = %s
            RETURNING i.id
            """,
            (customer_user_id, item_id),
        )
        await connection.commit()
        return await cursor.fetchone() is not None
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def remove_cart_items(customer_user_id: int, item_ids: list[int]) -> None:
    """删除已生成订单的指定条目，不误删用户并发加入的其他商品。"""
    if not item_ids:
        return
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        for item_id in item_ids:
            await connection.execute(
                """
                DELETE FROM cart_items AS i USING carts AS c
                WHERE i.cart_id = c.id AND c.customer_user_id = %s AND i.id = %s
                """,
                (customer_user_id, item_id),
            )
        await connection.commit()
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)
