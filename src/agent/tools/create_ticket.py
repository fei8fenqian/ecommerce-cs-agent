import logging
import random
from datetime import datetime
from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult
from store.ticket_store import create_ticket as store_create_ticket

logger = logging.getLogger(__name__)

# urgency 白名单
ALLOWED_URGENCY = ("low", "medium", "high", "critical")


class CreateTicket(BaseTool):
    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "create_ticket"

    @property
    def description(self) -> str:
        return (
            "创建客服工单，转人工处理。"
            "仅在用户明确要求人工/报修，出现安全风险、确认异常或自助排查无效时使用；"
            "普通 3C 参数、订单、物流、退款流程和设备故障咨询先由 Agent 处理。"
            "issue: 问题描述（必填）"
            "customer_name: 客户称呼（选填）"
            "phone: 联系电话（选填）"
            "urgency: 紧急程度，low/medium/high/critical（选填，默认 medium）"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "issue": {
                    "type": "string",
                    "description": "问题描述，简要概括用户诉求",
                },
                "customer_name": {
                    "type": "string",
                    "description": "客户称呼，如果用户没有提供传空字符串",
                },
                "phone": {
                    "type": "string",
                    "description": "联系电话，如果用户没有提供传空字符串",
                },
                "urgency": {
                    "type": "string",
                    "enum": list(ALLOWED_URGENCY),
                    "default": "medium",
                    "description": "low(一般) / medium(普通) / high(退款换货) / critical(投诉威胁)",
                },
            },
            "required": ["issue"],
        }

    async def execute(
        self,
        issue: str,
        customer_name: str = "",
        phone: str = "",
        urgency: str = "medium",
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        if tool_context is None:
            return ToolResult(
                name=self.name,
                status="error",
                error="缺少当前用户身份，无法创建工单",
            )

        if urgency not in ALLOWED_URGENCY:
            urgency = "medium"

        ticket_id = f"TK{datetime.now().strftime('%Y%m%d%H%M%S')}{random.randint(100, 999)}"
        initial_status = tool_context.ticket_queue_status or "AI待处理"
        if initial_status not in {"AI待处理", "待人工处理"}:
            initial_status = "AI待处理"

        try:
            await store_create_ticket(
                ticket_id=ticket_id,
                customer_name=customer_name,
                phone=phone,
                issue=issue,
                urgency=urgency,
                customer_user_id=tool_context.user_id,
                # 普通 Agent 工具创建的明确售后诉求进入 AI 队列；聊天入口已完成
                # 人工边界判断时，由服务端上下文直接写入人工队列，避免提交后竞态。
                status=initial_status,
            )

            return ToolResult(
                name=self.name,
                status="success",
                data={
                    "ticket_id": ticket_id,
                    "urgency": urgency,
                    "status": initial_status,
                    "message": (
                        f"工单 {ticket_id} 已创建，已转人工客服处理"
                        if initial_status == "待人工处理"
                        else f"工单 {ticket_id} 已创建，智能客服正在处理中"
                    ),
                },
            )

        except Exception:
            logger.error("工单创建失败")
            return ToolResult(name=self.name, status="error", error="工单创建失败")
