"""客户售后进度查询工具。"""

from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
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
            "查询当前登录客户自己的售后工单进度。"
            "用户询问售后进度、工单状态、人工处理到哪了、刚才报修有没有结果时使用。"
            "如果用户没有提供工单号，ticket_id 传空字符串，工具会返回最近的本人售后工单。"
            "只能查询，不能创建、认领、修改或关闭工单。"
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

        if not tickets:
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
        return ToolResult(
            name=self.name,
            status="success",
            data={"count": len(visible_tickets), "tickets": visible_tickets},
        )
