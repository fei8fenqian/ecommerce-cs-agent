import logging
import random
from datetime import datetime
from typing import Any

from agent.tools_registry import BaseTool, ToolResult
from core.db_pool import get_connection, put_connection

logger = logging.getLogger(__name__)

# urgency 白名单
ALLOWED_URGENCY = ("low", "medium", "high", "critical")


class CreateTicket(BaseTool):
    @property
    def name(self) -> str:
        return "create_ticket"

    @property
    def description(self) -> str:
        return (
            "创建客服工单，转人工处理。"
            "用户投诉、要求退款赔偿、情绪激动、明确要求转人工时使用。"
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

    def execute(
        self,
        issue: str,
        customer_name: str = "",
        phone: str = "",
        urgency: str = "medium",
    ) -> ToolResult:
        if urgency not in ALLOWED_URGENCY:
            urgency = "medium"

        ticket_id = f"TK{datetime.now().strftime('%Y%m%d%H%M%S')}{random.randint(100, 999)}"

        try:
            conn = get_connection()
            cur = conn.cursor()

            # 幂等建表
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id SERIAL PRIMARY KEY,
                    ticket_id VARCHAR(20) UNIQUE NOT NULL,
                    customer_name VARCHAR(50) DEFAULT '',
                    phone VARCHAR(20) DEFAULT '',
                    issue TEXT NOT NULL,
                    urgency VARCHAR(10) DEFAULT 'medium',
                    status VARCHAR(10) DEFAULT '待处理',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute(
                "INSERT INTO tickets (ticket_id, customer_name, phone, issue, urgency) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ticket_id, customer_name, phone, issue, urgency),
            )
            conn.commit()

            logger.info("工单创建: %s urgency=%s", ticket_id, urgency)

            return ToolResult(
                name=self.name,
                status="success",
                data={
                    "ticket_id": ticket_id,
                    "urgency": urgency,
                    "status": "待处理",
                    "message": f"工单 {ticket_id} 已创建，人工客服将尽快跟进",
                },
            )

        except Exception as e:
            logger.error("工单创建失败: %s", str(e))
            return ToolResult(name=self.name, status="error", error=f"工单创建失败: {str(e)}")

        finally:
            if "cur" in locals():
                cur.close()
            if "conn" in locals():
                put_connection(conn)
