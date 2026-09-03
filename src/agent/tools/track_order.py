import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from store.order_store import find_orders

logger = logging.getLogger(__name__)


class TrackOrder(BaseTool):
    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "track_order"

    @property
    def description(self) -> str:
        return """查询订单状态与物流信息。
        适用场景：用户询问"我的订单到哪了""帮我查一下订单""这个手机号下的订单"等。
        查单规则：优先用订单号精确查询；若无订单号则查询当前登录客户最近订单；
        手机号仅用于兼容已确认归属的历史订单。
        不传订单号时可以返回当前客户的多个订单。多结果本身不是失败，也不必然要求客户选择；
        只有当前业务目标必须绑定单一订单且服务端不能从客户原话唯一解析时，才会要求选择。
        你不得自行把其中一笔当成“最近买的某商品”。履约阶段以 delivery_state 为准：
        NOT_SHIPPED(未发货)、IN_TRANSIT(运输中)、DELIVERED(已签收)、
        NOT_APPLICABLE(已取消/已退款，不进入物流) 或 UNKNOWN(无法核验)。
        返回结果中的 order_source=checkout 表示当前商城新交易，
        order_source=legacy 表示只读历史订单；不要把两者的支付或退款事实混为一谈。"""

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

    async def execute(
        self,
        order_id: str = "",
        phone: str = "",
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        if tool_context is None:
            return ToolResult(
                name=self.name,
                status="error",
                error="缺少当前用户身份，无法追踪订单",
            )
        try:
            orders = await find_orders(
                tool_context.user_id,
                order_id=order_id,
                phone=phone,
            )

            if not orders:
                msg = "订单不存在" if order_id else "当前没有订单可查询"
                return ToolResult(name=self.name, status="error", error=msg)

            # 单号查询返回单个订单，手机号查询返回列表
            if order_id:
                data: dict[str, Any] = _with_delivery_facts(orders[0])
            else:
                visible_orders = []
                for recency_rank, order in enumerate(orders, start=1):
                    projected = _with_delivery_facts(order)
                    # The rank is calculated from the server-owned order
                    # ordering.  It is semantic metadata for subject
                    # resolution, never an order identifier or a transaction
                    # fact chosen by the model.
                    if "created_at" in projected or "order_date" in projected:
                        projected["recency_rank"] = recency_rank
                    visible_orders.append(projected)
                data = {
                    "count": len(visible_orders),
                    "orders": visible_orders,
                    "multiple_results": len(visible_orders) > 1,
                    # Multiple results are a valid order.list observation.  A
                    # singular workflow may ask the Control Plane for a
                    # choice, but the discovery tool cannot decide that from
                    # its own result shape.
                    "selection_required": False,
                }

            return ToolResult(name=self.name, status="success", data=data)

        except Exception:
            logger.error("track_order 查询失败")
            return ToolResult(name=self.name, status="error", error="订单查询失败")


def _with_delivery_facts(order: dict[str, Any]) -> dict[str, Any]:
    """补充不依赖模型推断的履约阶段，保留原订单字段兼容已有调用方。"""
    result = dict(order)
    status = str(order.get("status") or "").upper()
    refund = order.get("refund")
    refund_status = str(refund.get("status") or "").upper() if isinstance(refund, dict) else ""
    tracking = order.get("tracking")
    tracking_number = tracking.get("number") if isinstance(tracking, dict) else None

    if status in {"CANCELLED", "CANCELED", "REFUNDED"} or refund_status == "SUCCEEDED":
        delivery_state = "NOT_APPLICABLE"
    elif order.get("delivered_at") or status in {"DELIVERED", "SIGNED"}:
        delivery_state = "DELIVERED"
    elif tracking_number or status in {"SHIPPED", "IN_TRANSIT", "DELIVERING", "PICKED_UP"}:
        delivery_state = "IN_TRANSIT"
    elif status in {
        "PENDING",
        "PAID",
        "PENDING_PAYMENT",
        "PENDING_FULFILLMENT",
        "UNSHIPPED",
        "PROCESSING",
    }:
        delivery_state = "NOT_SHIPPED"
    else:
        delivery_state = "UNKNOWN"

    result["delivery_state"] = delivery_state
    return result
