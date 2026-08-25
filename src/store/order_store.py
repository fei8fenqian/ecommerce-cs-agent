"""订单数据访问层。

订单查询的 SQL 和 customer_user_id 数据范围限制集中在这里。
"""

from typing import Any

from infra.db_pool import get_connection, put_connection
from store.checkout_store import list_customer_checkout_orders

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


def _to_orders(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    """将订单与订单项的连接查询结果组装为前端和工具共享的订单结构。"""
    orders_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        current_order_id = row[0]
        if current_order_id not in orders_by_id:
            orders_by_id[current_order_id] = {
                "order_id": current_order_id,
                "order_source": "legacy",
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
    if order_id.startswith("SO"):
        return await _find_customer_checkout_orders(customer_user_id, order_id)
    if order_id:
        where_clause = "o.customer_user_id = %s AND o.order_id = %s"
        params = (customer_user_id, order_id)
    elif phone:
        where_clause = "o.customer_user_id = %s AND o.phone = %s"
        params = (customer_user_id, phone)
    else:
        legacy_orders = [
            {**order, "order_source": "legacy"} for order in await list_customer_orders(customer_user_id, limit=10)
        ]
        checkout_orders = await list_customer_checkout_orders(customer_user_id, limit=10)
        merged_orders = [_checkout_order_to_tool_order(order) for order in checkout_orders] + legacy_orders
        return sorted(merged_orders, key=lambda item: str(item.get("order_date", "")), reverse=True)

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(_ORDER_QUERY.format(where_clause=where_clause), params)
        rows = await cursor.fetchall()

        return _to_orders(rows)
    finally:
        if conn is not None:
            await put_connection(conn)


def _checkout_order_to_tool_order(order: object) -> dict[str, Any]:
    """把应用自有支付订单映射为现有 Agent 查单工具的统一输出。

    已退款订单的退款事实优先于履约事实。退款成功后即使历史履约记录仍是
    ``PENDING_FULFILLMENT``，也不能让 Agent 把它解释为“正在等待发货”。
    """
    order_no = str(getattr(order, "order_no"))
    amount_cents = int(getattr(order, "total_amount_cents"))
    payment_status = str(getattr(order, "payment_status"))
    fulfillment_status = getattr(order, "fulfillment_status")
    tracking_company = getattr(order, "tracking_company")
    tracking_number = getattr(order, "tracking_number")
    paid_amount = amount_cents / 100 if payment_status == "SUCCEEDED" else 0.0
    refund_status = getattr(order, "refund_status", None)
    result: dict[str, Any] = {
        "order_id": order_no,
        "order_source": "checkout",
        "status": str(fulfillment_status or getattr(order, "status")),
        "tracking": {"company": tracking_company, "number": tracking_number},
        "total_amount": amount_cents / 100,
        "paid_amount": paid_amount,
        "payment_method": "支付宝沙箱",
        "order_date": str(getattr(order, "created_at")),
        "delivered_at": None,
        "items": [
            {
                "product_name": str(getattr(order, "product_name")),
                "brand": None,
                "price": amount_cents / 100,
                "quantity": int(getattr(order, "quantity")),
            }
        ],
    }

    if refund_status is None:
        return result

    refund_status_text = str(refund_status)
    result["refund"] = {"status": refund_status_text}
    if refund_status_text == "SUCCEEDED":
        result.update(
            {
                "status": "REFUNDED",
                "tracking": {"company": None, "number": None},
                "fulfillment_status": "NOT_APPLICABLE",
                "refund": {
                    "status": "SUCCEEDED",
                    "message": "退款已完成；该订单不会进入发货或物流流程。",
                },
            }
        )
    elif refund_status_text == "PROCESSING":
        result.update(
            {
                "status": "REFUND_PROCESSING",
                "refund": {
                    "status": "PROCESSING",
                    "message": "退款正在处理；请等待退款结果，不要按物流状态解释。",
                },
            }
        )
    elif refund_status_text == "PENDING_CONFIRMATION":
        result.update(
            {
                "status": "REFUND_CONFIRMATION_REQUIRED",
                "refund": {
                    "status": "PENDING_CONFIRMATION",
                    "message": "退款申请已创建，正等待客户确认。",
                },
            }
        )
    return result


async def _find_customer_checkout_orders(customer_user_id: int, order_no: str) -> list[dict[str, Any]]:
    """按客户范围精确查找应用自有支付订单，避免落回 legacy 表。"""
    orders = await list_customer_checkout_orders(customer_user_id, limit=30)
    return [_checkout_order_to_tool_order(order) for order in orders if order.order_no == order_no]


async def list_customer_orders(customer_user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """读取当前客户的已归属订单列表，不接受手机号等跨资源查询条件。"""
    sql = _ORDER_QUERY.format(where_clause="o.customer_user_id = %s") + " LIMIT %s"
    connection = await get_connection()
    try:
        await connection.set_autocommit(True)
        cursor = await connection.execute(sql, (customer_user_id, limit))
        return _to_orders(await cursor.fetchall())
    finally:
        await put_connection(connection)
