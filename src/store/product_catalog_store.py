"""面向 Web 商品目录的只读查询，不承担成交、库存扣减或价格快照职责。"""

from typing import Any, Literal

from infra.db_pool import get_connection, put_connection

ProductCategory = Literal["laptops", "phones"]

_PRODUCT_TABLES: dict[ProductCategory, str] = {
    "laptops": "laptop_products",
    "phones": "phone_products",
}


def _image_url(metadata: Any) -> str | None:
    """从已有商品元数据取可选图片链接；没有时让前端使用统一占位视觉。"""
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("image_url") or metadata.get("thumbnail_url")
    return str(value) if value else None


async def list_products(
    category: ProductCategory,
    query: str = "",
    limit: int = 24,
) -> list[dict[str, Any]]:
    """返回浏览器可展示的最小商品目录字段。

    Args:
        category: 受白名单限制的商品类别，决定固定数据表。
        query: 按商品名或品牌进行的可选模糊筛选。
        limit: 受 API 上层限制的返回数量。

    Returns:
        商品目录行；不包含 embedding、完整内部元数据或任何成交事实。
    """
    table = _PRODUCT_TABLES[category]
    normalized_query = query.strip()
    sql = f"""
        SELECT id, product_name, brand, price, description, product_type,
               status, stock, warehouse, metadata
        FROM {table}
        WHERE (%s = '' OR product_name ILIKE %s OR brand ILIKE %s)
        ORDER BY product_name ASC
        LIMIT %s
    """
    pattern = f"%{normalized_query}%"
    connection = await get_connection()
    try:
        cursor = await connection.execute(sql, (normalized_query, pattern, pattern, limit))
        products: list[dict[str, Any]] = []
        async for row in cursor:
            (
                product_id,
                product_name,
                brand,
                price,
                description,
                product_type,
                status,
                stock,
                warehouse,
                metadata,
            ) = row
            products.append(
                {
                    "id": str(product_id),
                    "product_name": str(product_name or "未命名商品"),
                    "brand": str(brand or ""),
                    "price": float(price) if price is not None else None,
                    "description": str(description or ""),
                    "product_type": str(product_type or ""),
                    "status": str(status or ""),
                    "stock": int(stock or 0),
                    "warehouse": str(warehouse or ""),
                    "image_url": _image_url(metadata),
                }
            )
        return products
    finally:
        await put_connection(connection)
