"""人工升级通知 worker。

该 worker 只负责把应用内的升级记录投递到通知适配器，不改变工单业务状态。
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from infra.feishu_notifier import DutyNotifier, FeishuDeliveryError
from store.ticket_store import (
    claim_human_escalation,
    get_human_escalation_ticket,
    list_due_human_escalation_ids,
    mark_human_escalation_delivered,
    mark_human_escalation_retry,
)

logger = logging.getLogger(__name__)


class TicketEscalationNotificationWorker:
    """领取升级记录并可靠投递值班通知。"""

    def __init__(
        self,
        notifier: DutyNotifier,
        *,
        interval_seconds: float,
        max_attempts: int,
        claim_timeout_seconds: int = 60,
    ) -> None:
        self._notifier = notifier
        self._interval_seconds = interval_seconds
        self._max_attempts = max_attempts
        self._claim_timeout_seconds = claim_timeout_seconds

    async def process_once(self) -> int:
        """处理一轮到期通知。

        Returns:
            本轮成功领取并处理的升级记录数量。
        """
        processed = 0
        for escalation_id in await list_due_human_escalation_ids(claim_timeout_seconds=self._claim_timeout_seconds):
            record = await claim_human_escalation(
                escalation_id,
                claim_timeout_seconds=self._claim_timeout_seconds,
            )
            if record is None:
                continue
            processed += 1
            await self._deliver(record)
        return processed

    async def _deliver(self, record: dict[str, Any]) -> None:
        """投递一条已领取记录，并将结果写回投递状态。"""
        escalation_id = int(record["id"])
        attempts = int(record["attempts"])
        ticket = await get_human_escalation_ticket(escalation_id)
        if ticket is None:
            await mark_human_escalation_retry(
                escalation_id,
                "TICKET_NOT_FOUND",
                None,
                dead_lettered=True,
            )
            return

        try:
            await self._notifier.send_escalation(
                ticket_id=str(ticket["ticket_id"]),
                urgency=str(ticket["urgency"]),
                reason_code=str(record["reason_code"]),
                issue_summary=str(ticket["issue"]),
                created_at=str(ticket["created_at"]),
            )
        except FeishuDeliveryError as exc:
            await self._mark_failure(escalation_id, attempts, exc.code)
            return
        except Exception:
            logger.warning("人工升级通知投递失败", extra={"reason": "unknown_delivery_error"})
            await self._mark_failure(escalation_id, attempts, "UNKNOWN")
            return

        await mark_human_escalation_delivered(escalation_id)

    async def _mark_failure(self, escalation_id: int, attempts: int, error_code: str) -> None:
        """根据已消耗次数安排安全重试或死信。"""
        dead_lettered = attempts >= self._max_attempts
        next_attempt_at = None
        if not dead_lettered:
            delay_seconds = min(300, 2 ** max(0, attempts - 1))
            next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        await mark_human_escalation_retry(
            escalation_id,
            error_code,
            next_attempt_at,
            dead_lettered=dead_lettered,
        )

    async def run(self) -> None:
        """持续投递升级记录；取消时立即退出。"""
        while True:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("人工升级通知 worker 本轮执行失败")
            await asyncio.sleep(self._interval_seconds)
