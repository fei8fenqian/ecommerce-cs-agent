"""查询当前客户自己的商城退款状态。"""

import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from service.checkout_refund_service import customer_visible_refund_status
from store.checkout_store import list_customer_checkout_orders

logger = logging.getLogger(__name__)


class QueryRefundStatus(BaseTool):
    """只读 checkout 退款查询，不触发支付网关刷新或任何写操作。"""

    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "query_refund_status"

    @property
    def description(self) -> str:
        return (
            "查询当前登录客户自己的商城退款记录和退款状态。"
            "用户询问退款是否到账、退款处理到哪了时使用；order_id 可选，"
            "为空时只返回本人有退款记录的订单。此工具只读取本地已记录状态，"
            "不会提交退款、刷新网关或修改金额。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "SO 开头的商城订单号；未知时传空字符串。",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        order_id: str = "",
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        if tool_context is None or tool_context.role != "customer":
            return ToolResult(name=self.name, status="error", error="只有登录客户可以查询自己的退款状态")

        selected_order_id = order_id.strip()
        if selected_order_id and not selected_order_id.startswith("SO"):
            return ToolResult(name=self.name, status="error", error="退款状态只支持查询商城订单")

        try:
            orders = await list_customer_checkout_orders(tool_context.user_id, limit=30)
            if selected_order_id:
                orders = [order for order in orders if order.order_no == selected_order_id]

            refunds = [
                {
                    "order_id": order.order_no,
                    "refund_id": order.refund_id,
                    "status": customer_visible_refund_status(order.refund_status),
                    # 退款记录金额优先；只有旧的无金额退款摘要才回退到订单总额。
                    # 当前 checkout_refunds 已提供 amount_cents，后续部分退款不会被
                    # 错误解释成整笔订单金额。
                    "amount_cents": (
                        order.refund_amount_cents if order.refund_amount_cents is not None else order.total_amount_cents
                    ),
                    "created_at": order.created_at,
                }
                for order in orders
                if order.refund_id or order.refund_status
            ]
            return ToolResult(
                name=self.name,
                status="success",
                data={
                    "count": len(refunds),
                    "refunds": refunds,
                    "selection_required": len(refunds) > 1,
                    "refund_lookup": "found" if refunds else "no_record",
                },
            )
        except Exception:
            logger.error("query_refund_status 查询失败")
            return ToolResult(name=self.name, status="error", error="退款状态查询失败")
