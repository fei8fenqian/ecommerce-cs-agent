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
                msg = "订单不存在" if order_id else "当前没有可查询订单"
                return ToolResult(name=self.name, status="error", error=msg)

            # 单号查询返回单个订单，手机号查询返回列表
            if order_id:
                data: dict[str, Any] = orders[0]
            else:
                data = {"count": len(orders), "orders": orders}

            return ToolResult(name=self.name, status="success", data=data)

        except Exception:
            logger.error("track_order 查询失败")
            return ToolResult(name=self.name, status="error", error="订单查询失败")
