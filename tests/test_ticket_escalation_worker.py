"""人工升级通知 worker 的无数据库测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from infra.feishu_notifier import FeishuDeliveryError
from service.ticket_escalation_worker import TicketEscalationNotificationWorker


def _record(attempts: int = 1) -> dict[str, object]:
    return {
        "id": 7,
        "ticket_id": "TK-001",
        "escalation_generation": 1,
        "reason_code": "KNOWLEDGE_UNAVAILABLE",
        "attempts": attempts,
        "created_at": "2026-08-26T10:00:00+08:00",
    }


@pytest.mark.asyncio
async def test_notification_worker_delivers_and_marks_success() -> None:
    """通知成功后只确认投递，不修改工单状态。"""
    notifier = AsyncMock()
    worker = TicketEscalationNotificationWorker(notifier, interval_seconds=1, max_attempts=3)

    with (
        patch(
            "service.ticket_escalation_worker.list_due_human_escalation_ids",
            new=AsyncMock(return_value=[7]),
        ),
        patch(
            "service.ticket_escalation_worker.claim_human_escalation",
            new=AsyncMock(return_value=_record()),
        ),
        patch(
            "service.ticket_escalation_worker.get_human_escalation_ticket",
            new=AsyncMock(
                return_value={
                    "ticket_id": "TK-001",
                    "urgency": "high",
                    "issue": "订单无法支付",
                    "created_at": "2026-08-26T10:00:00+08:00",
                }
            ),
        ),
        patch(
            "service.ticket_escalation_worker.mark_human_escalation_delivered",
            new=AsyncMock(return_value=True),
        ) as mark_delivered,
    ):
        assert await worker.process_once() == 1

    notifier.send_escalation.assert_awaited_once()
    mark_delivered.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_notification_worker_retries_without_exposing_provider_error() -> None:
    """飞书失败只写安全错误码，未达到上限时进入退避重试。"""
    notifier = AsyncMock()
    notifier.send_escalation.side_effect = FeishuDeliveryError("HTTP_ERROR")
    worker = TicketEscalationNotificationWorker(notifier, interval_seconds=1, max_attempts=3)

    with (
        patch(
            "service.ticket_escalation_worker.list_due_human_escalation_ids",
            new=AsyncMock(return_value=[7]),
        ),
        patch(
            "service.ticket_escalation_worker.claim_human_escalation",
            new=AsyncMock(return_value=_record(attempts=1)),
        ),
        patch(
            "service.ticket_escalation_worker.get_human_escalation_ticket",
            new=AsyncMock(
                return_value={
                    "ticket_id": "TK-001",
                    "urgency": "high",
                    "issue": "订单无法支付",
                    "created_at": "2026-08-26T10:00:00+08:00",
                }
            ),
        ),
        patch(
            "service.ticket_escalation_worker.mark_human_escalation_retry",
            new=AsyncMock(return_value=True),
        ) as mark_retry,
    ):
        assert await worker.process_once() == 1

    mark_retry.assert_awaited_once()
    retry_call = mark_retry.await_args
    assert retry_call is not None
    assert retry_call.args[1] == "HTTP_ERROR"
    assert retry_call.kwargs["dead_lettered"] is False


@pytest.mark.asyncio
async def test_notification_worker_moves_exhausted_delivery_to_dlq() -> None:
    """达到最大尝试次数后不再无限重试。"""
    notifier = AsyncMock()
    notifier.send_escalation.side_effect = FeishuDeliveryError("REMOTE_REJECTED")
    worker = TicketEscalationNotificationWorker(notifier, interval_seconds=1, max_attempts=3)

    with (
        patch(
            "service.ticket_escalation_worker.list_due_human_escalation_ids",
            new=AsyncMock(return_value=[7]),
        ),
        patch(
            "service.ticket_escalation_worker.claim_human_escalation",
            new=AsyncMock(return_value=_record(attempts=3)),
        ),
        patch(
            "service.ticket_escalation_worker.get_human_escalation_ticket",
            new=AsyncMock(
                return_value={
                    "ticket_id": "TK-001",
                    "urgency": "high",
                    "issue": "订单无法支付",
                    "created_at": "2026-08-26T10:00:00+08:00",
                }
            ),
        ),
        patch(
            "service.ticket_escalation_worker.mark_human_escalation_retry",
            new=AsyncMock(return_value=True),
        ) as mark_retry,
    ):
        await worker.process_once()

    retry_call = mark_retry.await_args
    assert retry_call is not None
    assert retry_call.kwargs["dead_lettered"] is True
    assert retry_call.args[2] is None
