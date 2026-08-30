"""查询当前客户订单的退款资格，不创建退款或修改订单。"""

import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from store.checkout_refund_store import get_customer_refund_eligibility

logger = logging.getLogger(__name__)


class CheckRefundEligibility(BaseTool):
    """只读退款资格查询；真实退款写入时仍由确定性服务二次校验。"""

    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "check_refund_eligibility"

    @property
    def description(self) -> str:
        return (
            "查询当前登录客户指定商城订单是否满足全额退款资格。只返回资格结论，不会创建退款、确认退款或调用支付渠道。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "SO 开头的商城订单号；有多笔订单时必须先让客户选择。",
                },
            },
            "required": ["order_id"],
        }

    async def execute(
        self,
        order_id: str,
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        if tool_context is None or tool_context.role != "customer":
            return ToolResult(name=self.name, status="error", error="只有登录客户可以查询自己的退款资格")

        selected_order_id = order_id.strip()
        if not selected_order_id.startswith("SO"):
            return ToolResult(name=self.name, status="error", error="退款资格只支持查询商城订单")

        try:
            eligible = await get_customer_refund_eligibility(
                customer_user_id=tool_context.user_id,
                order_no=selected_order_id,
            )
            return ToolResult(
                name=self.name,
                status="success",
                data={"order_id": selected_order_id, "refund_eligibility": eligible},
            )
        except Exception:
            logger.error("check_refund_eligibility 查询失败")
            return ToolResult(name=self.name, status="error", error="退款资格查询失败")
