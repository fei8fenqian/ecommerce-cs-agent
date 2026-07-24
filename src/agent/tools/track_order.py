import logging
from typing import Any

import psycopg2

from agent.tools_registry import BaseTool, ToolResult
from config import settings

logger = logging.getLogger(__name__)


class TrackOrder(BaseTool):
    @property
    def name(self) -> str:
        return "track_order"

    @property
    def description(self) -> str:
        return """查询订单状态与物流信息。
        适用场景：用户询问"我的订单到哪了""帮我查一下订单""这个手机号下的订单"等。
        查单规则：优先用订单号精确查询；若无订单号则用手机号查该号码下所有订单。
        两者至少提供一个。"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单号。如果用户没有提供订单号，传空字符串",
                },
                "phone": {
                    "type": "string",
                    "description": "手机号。如果用户没有提供手机号，传空字符串",
                },
            },
            "required": [],
        }

    _SQL = (
        "SELECT o.order_id, o.status, o.tracking_company, o.tracking_number, "
        "o.total_amount, o.paid_amount, o.payment_method, o.order_date, "
        "oi.product_name, oi.brand, oi.price, oi.quantity "
        "FROM orders o "
        "LEFT JOIN order_items oi ON o.order_id = oi.order_id "
        "WHERE {where} "
        "ORDER BY o.order_date DESC"
    )

    def execute(self, order_id: str = "", phone: str = "") -> ToolResult:  # noqa: C901
        if not order_id and not phone:
            return ToolResult(name=self.name, status="error", error="请提供订单号或手机号")

        where = "o.order_id = %s" if order_id else "o.phone = %s"
        param = order_id if order_id else phone
        label = "order_id" if order_id else "phone"

        try:
            conn = psycopg2.connect(
                host=settings.pg_host,
                port=settings.pg_port,
                user=settings.pg_user,
                password=settings.pg_password.get_secret_value(),
                dbname=settings.pg_dbname,
            )
            cur = conn.cursor()
            cur.execute(self._SQL.format(where=where), (param,))
            rows = cur.fetchall()

            if not rows:
                msg = "订单不存在" if order_id else "该手机号下没有订单"
                return ToolResult(name=self.name, status="error", error=msg)

            # 按 order_id 分组聚合
            orders: dict[str, dict[str, Any]] = {}
            for row in rows:
                oid = row[0]
                if oid not in orders:
                    orders[oid] = {
                        "order_id": oid,
                        "status": row[1],
                        "tracking": {"company": row[2], "number": row[3]},
                        "total_amount": float(row[4]) if row[4] else 0.0,
                        "paid_amount": float(row[5]) if row[5] else 0.0,
                        "payment_method": row[6],
                        "order_date": str(row[7]),
                        "items": [],
                    }
                if row[8] is not None:  # LEFT JOIN 有商品
                    orders[oid]["items"].append(
                        {
                            "product_name": row[8],
                            "brand": row[9],
                            "price": float(row[10]) if row[10] else 0.0,
                            "quantity": row[11],
                        }
                    )

            # 单号查询返回单个订单，手机号查询返回列表
            if order_id:
                data: dict[str, Any] = orders[order_id]
            else:
                data = {"count": len(orders), "orders": list(orders.values())}

            return ToolResult(name=self.name, status="success", data=data)

        except Exception as e:
            logger.error("track_order 查询失败: %s=%s error=%s", label, param, str(e))
            return ToolResult(name=self.name, status="error", error=f"订单查询失败: {str(e)}")

        finally:
            if "cur" in locals():
                cur.close()
            if "conn" in locals():
                conn.close()
