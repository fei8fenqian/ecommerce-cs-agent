import logging

from mcp.server.fastmcp import FastMCP

from infra.db_pool import get_connection, init_pool, put_connection

logger = logging.getLogger(__name__)
mcp = FastMCP("payment-server", host="0.0.0.0", port=8081)
_pool = None


async def _get_order(order_id: str) -> dict | None:
    try:
        await _ensure_pool()
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            """
            select o.order_id, o.customer_id, o.customer_name, o.order_date,
            o.status, o.total_amount, o.paid_amount, o.discount,
            o.payment_method, o.payment_time, o.tracking_company, o.tracking_number,
            o.shipping_address, o.phone, o.created_at,
            oi.product_name, oi.category, oi.brand, oi.price, oi.quantity
            from orders o left join order_items oi
            on o.order_id=oi.order_id
            where o.order_id = %s
        """,
            (order_id,),
        )
        rows = await cur.fetchall()
        if not rows:
            return None

        first = rows[0]
        # 聚合商品列表（LEFT JOIN 可能多行）
        items = []
        for row in rows:
            if row[15] is not None:  # product_name
                items.append(
                    {
                        "product_name": row[15],
                        "category": row[16],
                        "brand": row[17],
                        "price": float(row[18]) if row[18] else 0.0,
                        "quantity": row[19],
                    }
                )

        return {
            "order_id": first[0],
            "customer_id": first[1],
            "customer_name": first[2],
            "order_date": str(first[3]) if first[3] else "",
            "status": first[4],
            "total_amount": float(first[5]) if first[5] else 0.0,
            "paid_amount": float(first[6]) if first[6] else 0.0,
            "discount": float(first[7]) if first[7] else 0.0,
            "payment_method": first[8],
            "payment_time": str(first[9]) if first[9] else None,
            "tracking_company": first[10],
            "tracking_number": first[11],
            "shipping_address": first[12],
            "phone": first[13],
            "created_at": str(first[14]) if first[14] else "",
            "items": items,
        }
    finally:
        await put_connection(conn)


@mcp.tool()
async def check_payment(order_id: str) -> dict:
    """查询订单支付状态。

    适用场景：用户问"付过钱了吗""支付成功没有"。

    Args:
        order_id: 订单号，如 ORD2026070100138

    Returns:
        payment_status, amount, method, paid_at,
    """
    try:
        order = await _get_order(order_id)
        if order is None:
            return {"found": False, "order_id": order_id, "error": "订单不存在"}

        result = {
            "found": True,
            "order_id": order.get("order_id"),
            "status": order.get("status"),
            "paid_amount": order.get("paid_amount"),
            "payment_method": order.get("payment_method"),
            "payment_time": order.get("payment_time"),
            "items": order.get("items"),
        }

        return result

    except Exception as e:
        logger.exception("check_payment 查询失败 order_id=%s", order_id)
        return {"found": False, "order_id": order_id, "error": str(e)}


@mcp.tool()
async def request_refund(order_id: str, reason: str = "") -> dict:
    """申请退款。

        适用场景：用户说"我要退款""不想要了帮我退一下""申请退款"等。

        Args:
            order_id: 订单号，如 ORD2026070100138
            reason: 退款原因，用户描述或系统提取，如"商品不满意""买错了""质量问题"

        Returns:
            {"success": True/False, "order_id": "...", "message": "...",
    "refund_amount": 5999}
    """
    try:
        await _ensure_pool()
        order = await _get_order(order_id)
        if order is None:
            return {
                "success": False,
                "order_id": order_id,
                "message": "订单不存在，无法退款",
                "refund_amount": 0,
            }

        if order.get("status") in ("已退款", "退款中"):
            return {
                "success": False,
                "order_id": order_id,
                "message": "该订单已退款或正在退款中",
                "refund_amount": 0,
            }

        if order.get("status") == "已取消":
            return {
                "success": False,
                "order_id": order_id,
                "message": "已取消的订单无法退款",
                "refund_amount": 0,
            }

        if order["paid_amount"] <= 0:
            return {
                "success": False,
                "order_id": order_id,
                "message": "该订单未支付，无法退款",
                "refund_amount": 0,
            }

        refund_amount = order["paid_amount"]

        try:
            conn = await get_connection()
            await conn.set_autocommit(True)
            await conn.execute("update orders set status = '已退款' where order_id = %s", (order_id,))
        except Exception as e:
            logger.exception("request_refund 退款失败 order_id=%s", order_id)
            return {
                "success": False,
                "order_id": order_id,
                "message": f"退款失败，原因：{str(e)}",
                "refund_amount": 0,
            }
        finally:
            await put_connection(conn)

        return {
            "success": True,
            "order_id": order_id,
            "message": f"退款申请已提交，退款金额 ¥{refund_amount:.2f}，预计 2 小时内到账",
            "refund_amount": refund_amount,
            "reason": reason,
        }

    except Exception:
        logger.exception("request_refund 退款失败 order_id=%s", order_id)
        return {
            "success": False,
            "order_id": order_id,
            "message": "退款失败",
            "refund_amount": 0,
        }


async def _ensure_pool():
    global _pool
    if _pool is None:
        await init_pool()


if __name__ == "__main__":
    mcp.run(transport="sse")
