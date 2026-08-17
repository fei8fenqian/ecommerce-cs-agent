"""订单数据访问层。

订单查询的 SQL 和 customer_user_id 数据范围限制集中在这里。
"""

from typing import Any

from infra.db_pool import get_connection, put_connection

_ORDER_QUERY = """
    SELECT
        o.order_id,
        o.status,
        o.tracking_company,
        o.tracking_number,
        o.total_amount,
        o.paid_amount,
        o.payment_method,
        o.order_date,
        o.delivered_at,
        oi.product_name,
        oi.brand,
        oi.price,
        oi.quantity
    FROM public.orders AS o
    LEFT JOIN public.order_items AS oi ON o.order_id = oi.order_id
    WHERE {where_clause}
    ORDER BY o.order_date DESC
"""


async def find_orders(
    customer_user_id: int,
    *,
    order_id: str = "",
    phone: str = "",
) -> list[dict[str, Any]]:
    """查询当前客户拥有的订单。

    order_id 和 phone 只是查询条件，customer_user_id 才是数据范围条件。
    未匹配的历史订单 customer_user_id 为 NULL，因此不会被返回。
    """
    if order_id:
        where_clause = "o.customer_user_id = %s AND o.order_id = %s"
        params = (customer_user_id, order_id)
    else:
        where_clause = "o.customer_user_id = %s AND o.phone = %s"
        params = (customer_user_id, phone)

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(_ORDER_QUERY.format(where_clause=where_clause), params)
        rows = await cursor.fetchall()

        orders_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            current_order_id = row[0]
            if current_order_id not in orders_by_id:
                orders_by_id[current_order_id] = {
                    "order_id": current_order_id,
                    "status": row[1],
                    "tracking": {"company": row[2], "number": row[3]},
                    "total_amount": float(row[4]) if row[4] else 0.0,
                    "paid_amount": float(row[5]) if row[5] else 0.0,
                    "payment_method": row[6],
                    "order_date": str(row[7]),
                    "delivered_at": str(row[8]) if row[8] else None,
                    "items": [],
                }

            if row[9] is not None:
                orders_by_id[current_order_id]["items"].append(
                    {
                        "product_name": row[9],
                        "brand": row[10],
                        "price": float(row[11]) if row[11] else 0.0,
                        "quantity": row[12],
                    }
                )

        return list(orders_by_id.values())
    finally:
        if conn is not None:
            await put_connection(conn)
