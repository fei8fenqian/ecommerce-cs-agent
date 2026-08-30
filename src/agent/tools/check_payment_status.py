"""面向客户的支付宝沙箱支付状态查询工具。"""

import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from service.checkout_service import (
    PaymentNotCreatedError,
    PaymentStatusUnavailableError,
    refresh_customer_payment_status,
)
from store.order_store import find_orders

logger = logging.getLogger(__name__)


class CheckPaymentStatus(BaseTool):
    """让 Agent 在客户确认付款后，主动向支付宝收敛订单状态。"""

    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "check_payment_status"

    @property
    def description(self) -> str:
        return (
            "查询当前登录客户的新订单是否已在支付宝沙箱支付成功。"
            "当用户说“我已经付了”“支付成功了吗”“刚刚那笔付款怎么样”时使用。"
            "order_id 优先传 SO 开头的商城订单号；用户未提供时传空字符串，"
            "工具会只从当前用户自己的最近待支付订单中选择。"
            "此工具只查询和核验支付结果，不能创建支付、退款或修改金额。"
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
        """查询当前客户的一笔商城支付，并返回已核验后的本地订单事实。

        Args:
            order_id: 可选的商城订单号；为空时仅从当前客户订单中选择待支付单。
            tool_context: 服务端注入的已认证身份，不能由模型或浏览器伪造。

        Returns:
            支付结果和订单摘要；不会返回支付宝密钥、签名或其他用户订单。
        """
        if tool_context is None or tool_context.role != "customer":
            return ToolResult(name=self.name, status="error", error="只有登录客户可以查询自己的支付状态")

        try:
            selected_order_id = order_id.strip()
            current_order: dict[str, Any] | None = None
            if not selected_order_id:
                recent = await find_orders(tool_context.user_id)
                pending = next(
                    (
                        item
                        for item in recent
                        if str(item.get("order_id", "")).startswith("SO") and item.get("status") == "PENDING_PAYMENT"
                    ),
                    None,
                )
                selected_order_id = str(pending.get("order_id", "")) if pending else ""
                current_order = pending

            if not selected_order_id.startswith("SO"):
                return ToolResult(name=self.name, status="error", error="当前没有可查询的待支付商城订单")

            # 已有本地成功支付事实时，不要因为没有 pending payment 可刷新而把
            # 已支付订单误报成未支付。刷新服务的 bool 只表示“本次是否从网关刷新
            # 成功”，不是订单当前支付状态。
            if current_order is None:
                current_orders = await find_orders(tool_context.user_id, order_id=selected_order_id)
                if not current_orders:
                    return ToolResult(name=self.name, status="error", error="订单不存在或无法核验")
                current_order = current_orders[0]
            if str(current_order.get("payment_status", "")).upper() == "SUCCEEDED":
                return ToolResult(
                    name=self.name,
                    status="success",
                    data={
                        "payment_checked": True,
                        "payment_result": "支付成功",
                        "order": current_order,
                    },
                )

            paid = await refresh_customer_payment_status(
                customer_user_id=tool_context.user_id,
                order_no=selected_order_id,
            )
            refreshed = await find_orders(tool_context.user_id, order_id=selected_order_id)
            if not refreshed:
                return ToolResult(name=self.name, status="error", error="订单不存在或无法核验")

            return ToolResult(
                name=self.name,
                status="success",
                data={
                    "payment_checked": True,
                    "payment_result": "支付成功" if paid else "订单尚未支付",
                    "order": refreshed[0],
                },
            )
        except PaymentNotCreatedError:
            return ToolResult(name=self.name, status="error", error="该订单尚未打开支付宝付款页，请从订单页继续付款")
        except PaymentStatusUnavailableError:
            return ToolResult(name=self.name, status="error", error="支付宝暂时无法确认支付状态，请稍后重试")
        except Exception:
            logger.error("支付状态查询失败")
            return ToolResult(name=self.name, status="error", error="支付状态查询失败")
