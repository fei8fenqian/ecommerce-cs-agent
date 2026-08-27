"""客户售后进度查询工具。"""

from typing import Any, cast

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from store.after_sale_store import list_customer_after_sale_summaries
from store.ticket_store import get_customer_ticket, list_customer_tickets


class CheckAfterSales(BaseTool):
    """让客户查询自己售后工单的当前状态。"""

    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "check_after_sales"

    @property
    def description(self) -> str:
        return (
            "查询当前登录客户自己的售后申请或售后工单进度。"
            "用户询问退货、换货、退款审核、取件、售后申请、工单状态或人工处理到哪了时使用。"
            "如果用户没有提供工单号，ticket_id 传空字符串，工具会返回最近的本人售后工单。"
            "结果中的 after_sales 是售后申请，tickets 是人工工单；两者不是同一状态。"
            "只能查询，不能创建、认领、修改或关闭任何申请。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "string",
                    "description": "售后工单号；未知时传空字符串。",
                }
            },
            "required": [],
        }

    async def execute(
        self,
        ticket_id: str = "",
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        """读取当前客户的售后状态，不返回客户联系方式或工单正文。

        Args:
            ticket_id: 可选工单号；为空时读取当前客户最近的工单。
            tool_context: 服务端注入的当前客户身份。

        Returns:
            当前客户可见的工单编号、状态、紧急程度和创建时间。
        """
        if tool_context is None or tool_context.role != "customer":
            return ToolResult(name=self.name, status="error", error="只有登录客户可以查询售后进度")

        if ticket_id.strip():
            ticket = await get_customer_ticket(ticket_id.strip(), tool_context.user_id)
            tickets = [ticket] if ticket is not None else []
        else:
            tickets = await list_customer_tickets(tool_context.user_id)

        after_sales: list[dict[str, object]] = []
        if not ticket_id.strip() and not tickets:
            try:
                after_sales = await list_customer_after_sale_summaries(tool_context.user_id)
            except Exception:
                # 旧环境可能还未部署售后申请表；不能把依赖故障说成“没有申请”。
                return ToolResult(name=self.name, status="error", error="售后状态暂时无法查询")

        if not tickets and not after_sales:
            return ToolResult(name=self.name, status="error", error="当前没有可查询的售后工单")

        visible_tickets = [
            {
                "ticket_id": str(ticket["ticket_id"]),
                "status": str(ticket.get("status") or "处理中"),
                "urgency": str(ticket.get("urgency") or "medium"),
                "created_at": str(ticket.get("created_at") or ""),
            }
            for ticket in tickets
        ]
        visible_after_sales = [
            {
                "after_sale_id": str(item.get("after_sale_id") or ""),
                "order_id": str(item.get("order_id") or ""),
                "status": str(item.get("status") or "UNKNOWN"),
                "reason_code": str(item.get("reason_code") or "UNKNOWN"),
                "refund_amount_cents": int(cast(int, item.get("refund_amount_cents") or 0)),
                "submitted_at": str(item.get("submitted_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            for item in after_sales
        ]
        data: dict[str, object] = {
            "count": len(visible_tickets) + len(visible_after_sales),
            "tickets": visible_tickets,
        }
        # 保持原有工单响应契约；只有查到售后申请时才增加新字段。
        if visible_after_sales:
            data["after_sales"] = visible_after_sales
        return ToolResult(name=self.name, status="success", data=data)
