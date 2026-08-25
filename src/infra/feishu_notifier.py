"""飞书值班通知适配器。

飞书只负责通知和协作，不是工单事实来源。这里不提供任意群聊、通讯录、文档或
多维表格操作；通知内容由固定字段组装，避免模型直接获得外部写权限。
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from config import settings
from log_config import redact_text

_ORDER_REFERENCE_PATTERN = re.compile(r"\b(?:ORD|SO|ORDER)[-_A-Z0-9]{3,}\b", re.IGNORECASE)


def _redact_issue_summary(value: str) -> str:
    """在通用文本脱敏后移除常见订单引用，避免摘要泄露业务标识。"""
    return _ORDER_REFERENCE_PATTERN.sub("[REDACTED]", redact_text(value))


class FeishuDeliveryError(RuntimeError):
    """飞书通知没有被可靠接收。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DutyNotifier(Protocol):
    """人工升级通知的最小受控接口。"""

    async def send_escalation(
        self,
        *,
        ticket_id: str,
        urgency: str,
        reason_code: str,
        issue_summary: str,
        created_at: str,
    ) -> None:
        """发送一条不含敏感信息的人工升级通知。"""
        ...


def build_escalation_payload(
    *,
    ticket_id: str,
    urgency: str,
    reason_code: str,
    issue_summary: str,
    created_at: str,
    workbench_url: str,
) -> dict[str, Any]:
    """组装固定字段的飞书交互卡片。

    Args:
        ticket_id: 本系统工单编号。
        urgency: 工单紧急度。
        reason_code: 数据库白名单中的升级原因码。
        issue_summary: 工单摘要；函数会再次做文本脱敏和长度限制。
        created_at: 工单创建时间文本。
        workbench_url: 客服工作台深链。

    Returns:
        可直接提交给飞书群机器人 Webhook 的 JSON。不会包含客户姓名、手机号、
        邮箱、订单号、完整对话或模型异常。
    """
    safe_summary = _redact_issue_summary(issue_summary).strip().replace("\n", " ")[:240]
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "客服工单需要跟进"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**工单**：{ticket_id}\n"
                            f"**紧急度**：{urgency}\n"
                            f"**升级原因**：{reason_code}\n"
                            f"**问题摘要**：{safe_summary or '暂无摘要'}\n"
                            f"**创建时间**：{created_at}"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "打开客服工作台"},
                            "type": "primary",
                            "url": workbench_url,
                        }
                    ],
                },
            ],
        },
    }


@dataclass
class FakeDutyNotifier:
    """开发和测试用通知器，不访问网络。"""

    sent: list[dict[str, Any]] = field(default_factory=list)

    async def send_escalation(
        self,
        *,
        ticket_id: str,
        urgency: str,
        reason_code: str,
        issue_summary: str,
        created_at: str,
    ) -> None:
        """保存一份固定字段通知，供本地演示和断言使用。"""
        self.sent.append(
            build_escalation_payload(
                ticket_id=ticket_id,
                urgency=urgency,
                reason_code=reason_code,
                issue_summary=issue_summary,
                created_at=created_at,
                workbench_url=settings.feishu_workbench_url,
            )
        )


@dataclass
class FeishuWebhookNotifier:
    """通过固定群机器人 Webhook 投递值班卡片。"""

    webhook_url: str
    timeout_seconds: float = 5.0
    workbench_url: str = "http://localhost:5173/after-sales"
    transport: httpx.AsyncBaseTransport | None = field(default=None, repr=False)

    async def send_escalation(
        self,
        *,
        ticket_id: str,
        urgency: str,
        reason_code: str,
        issue_summary: str,
        created_at: str,
    ) -> None:
        """发送固定卡片并校验飞书成功响应。

        Raises:
            FeishuDeliveryError: 网络失败、非 2xx 响应或飞书业务码非零。
        """
        payload = build_escalation_payload(
            ticket_id=ticket_id,
            urgency=urgency,
            reason_code=reason_code,
            issue_summary=issue_summary,
            created_at=created_at,
            workbench_url=self.workbench_url,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(self.webhook_url, json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise FeishuDeliveryError("HTTP_ERROR") from exc

        if not isinstance(body, dict) or body.get("code", 0) not in (0, None):
            raise FeishuDeliveryError("REMOTE_REJECTED")


def build_duty_notifier() -> DutyNotifier:
    """根据配置创建通知器；未配置飞书时返回本地 FakeNotifier。"""
    if not settings.feishu_duty_webhook_url:
        return FakeDutyNotifier()
    return FeishuWebhookNotifier(
        webhook_url=settings.feishu_duty_webhook_url,
        timeout_seconds=settings.feishu_webhook_timeout_seconds,
        workbench_url=settings.feishu_workbench_url,
    )
