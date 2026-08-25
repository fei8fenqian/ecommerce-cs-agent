"""飞书人工升级通知适配器测试。"""

import httpx
import pytest

from infra.feishu_notifier import (
    FakeDutyNotifier,
    FeishuDeliveryError,
    FeishuWebhookNotifier,
    build_escalation_payload,
)


def test_escalation_card_has_fixed_fields_and_redacts_customer_text() -> None:
    """通知卡片只能包含脱敏摘要和工作台入口。"""
    payload = build_escalation_payload(
        ticket_id="TK-001",
        urgency="high",
        reason_code="ORDER_OR_PAYMENT_ACTION",
        issue_summary="请联系 13800138000 或 customer@example.com，订单 ORD-SECRET-1",
        created_at="2026-08-26T10:00:00+08:00",
        workbench_url="http://localhost:5173/after-sales",
    )
    serialized = str(payload)
    assert "13800138000" not in serialized
    assert "customer@example.com" not in serialized
    assert "ORD-SECRET-1" not in serialized
    assert "TK-001" in serialized
    assert "ORDER_OR_PAYMENT_ACTION" in serialized
    assert "http://localhost:5173/after-sales" in serialized


@pytest.mark.asyncio
async def test_fake_notifier_does_not_access_network() -> None:
    """没有飞书配置时，升级通知仍可在本地记录并演示。"""
    notifier = FakeDutyNotifier()
    await notifier.send_escalation(
        ticket_id="TK-002",
        urgency="medium",
        reason_code="KNOWLEDGE_UNAVAILABLE",
        issue_summary="无法开机",
        created_at="2026-08-26T10:00:00+08:00",
    )
    assert len(notifier.sent) == 1


@pytest.mark.asyncio
async def test_webhook_notifier_accepts_success_response() -> None:
    """Webhook 适配器只在 HTTP 和飞书业务码都成功时返回。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/bot/test"
        return httpx.Response(200, json={"code": 0})

    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.cn/bot/test",
        transport=httpx.MockTransport(handler),
    )
    await notifier.send_escalation(
        ticket_id="TK-003",
        urgency="low",
        reason_code="MODEL_ESCALATION",
        issue_summary="知识不足",
        created_at="2026-08-26T10:00:00+08:00",
    )


@pytest.mark.asyncio
async def test_webhook_notifier_hides_remote_failure_details() -> None:
    """飞书响应正文不应流入业务错误或日志。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 19001, "msg": "secret webhook detail"})

    notifier = FeishuWebhookNotifier(
        webhook_url="https://open.feishu.cn/bot/test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(FeishuDeliveryError) as error:
        await notifier.send_escalation(
            ticket_id="TK-004",
            urgency="high",
            reason_code="AGENT_UNAVAILABLE",
            issue_summary="暂时不可用",
            created_at="2026-08-26T10:00:00+08:00",
        )
    assert str(error.value) == "REMOTE_REJECTED"
    assert "secret" not in str(error.value)
