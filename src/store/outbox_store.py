"""outbox_events 的参数化 PostgreSQL Repository。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from service.after_sale_types import (
    IdempotencyKey,
    NonNegativeInt,
    OutboxEventType,
    OutboxStatus,
)
from store.refund_store_types import (
    AsyncConnection,
    NewRefundOutboxEvent,
    OutboxDeliveryUpdate,
    OutboxRecord,
    RefundOutboxPayload,
)


class OutboxRepository(Protocol):
    async def find_by_idempotency_key(self, idempotency_key: IdempotencyKey) -> OutboxRecord | None:
        """按幂等键读取已有 Outbox 事件。"""

    async def find_by_refund_and_type(
        self,
        refund_id: UUID,
        event_type: OutboxEventType,
    ) -> OutboxRecord | None:
        """按退款单和事件类型读取已有事件。"""

    async def insert_refund_authorized(self, event: NewRefundOutboxEvent) -> OutboxRecord:
        """插入退款授权 Outbox 事件。"""

    async def update_delivery_state(
        self,
        event_id: UUID,
        expected_status: OutboxStatus,
        update: OutboxDeliveryUpdate,
    ) -> OutboxRecord | None:
        """按期望状态更新投递状态。"""


def _record_from_row(row: Sequence[object]) -> OutboxRecord:
    event_id = cast(UUID, row[0])
    refund_id = cast(UUID, row[1])
    payload = RefundOutboxPayload.from_json(row[6])
    if payload.outbox_event_id != event_id or payload.refund_id != refund_id:
        raise ValueError("outbox payload identifiers do not match row identifiers")
    return OutboxRecord(
        id=event_id,
        refund_id=refund_id,
        event_type=OutboxEventType(str(row[2])),
        idempotency_key=IdempotencyKey(str(row[3])),
        status=OutboxStatus(str(row[4])),
        attempt_count=NonNegativeInt(cast(int, row[5])),
        payload=payload,
        created_at=cast(datetime, row[7]),
    )


class PsycopgOutboxRepository:
    """绑定到一个事务连接的 Outbox Repository。"""

    def __init__(self, connection: AsyncConnection):
        self._connection = connection

    async def find_by_idempotency_key(self, idempotency_key: IdempotencyKey) -> OutboxRecord | None:
        """按 Outbox 幂等键读取已有事件。

        Args:
            idempotency_key: Outbox 事件幂等键。

        Returns:
            已存在的事件；没有时返回 None。
        """
        cursor = await self._connection.execute(
            """SELECT id, refund_id, event_type, idempotency_key,
                      status, attempt_count, payload, created_at
               FROM public.outbox_events
               WHERE idempotency_key = %s""",
            (str(idempotency_key),),
        )
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def find_by_refund_and_type(
        self,
        refund_id: UUID,
        event_type: OutboxEventType,
    ) -> OutboxRecord | None:
        """按退款单和事件类型读取唯一 Outbox 事件。

        Args:
            refund_id: 退款单内部 UUID。
            event_type: 受控 Outbox 事件类型。

        Returns:
            已存在的事件；没有时返回 None。
        """
        cursor = await self._connection.execute(
            """SELECT id, refund_id, event_type, idempotency_key,
                      status, attempt_count, payload, created_at
               FROM public.outbox_events
               WHERE refund_id = %s AND event_type = %s""",
            (refund_id, event_type.value),
        )
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def insert_refund_authorized(self, event: NewRefundOutboxEvent) -> OutboxRecord:
        """插入已授权退款 Outbox 事件。

        Args:
            event: 只包含退款适配器所需白名单字段的新事件。

        Returns:
            插入后的 Outbox 记录。

        Raises:
            RuntimeError: 数据库没有返回插入记录时抛出。
        """
        cursor = await self._connection.execute(
            """INSERT INTO public.outbox_events (
                    id, refund_id, event_type, idempotency_key, payload
                ) VALUES (%s, %s, %s, %s, %s)
                RETURNING id, refund_id, event_type, idempotency_key,
                          status, attempt_count, payload, created_at""",
            (
                event.id,
                event.refund_id,
                event.event_type.value,
                str(event.idempotency_key),
                Jsonb(event.payload.as_json()),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("outbox insert returned no row")
        return _record_from_row(row)

    async def update_delivery_state(
        self,
        event_id: UUID,
        expected_status: OutboxStatus,
        update: OutboxDeliveryUpdate,
    ) -> OutboxRecord | None:
        """按当前状态更新 Outbox 投递状态。

        Args:
            event_id: Outbox 事件内部 UUID。
            expected_status: 调用方读取到的旧状态。
            update: 新状态、重试次数和受控错误信息。

        Returns:
            更新后的记录；状态不匹配或资源不存在时返回 None。
        """
        cursor = await self._connection.execute(
            """UPDATE public.outbox_events
                SET status = %s,
                    attempt_count = %s,
                    last_error_code = %s,
                    processed_at = %s,
                    dead_lettered_at = %s,
                    updated_at = %s
                WHERE id = %s AND status = %s
                RETURNING id, refund_id, event_type, idempotency_key,
                          status, attempt_count, payload, created_at""",
            (
                update.status.value,
                update.attempt_count.value,
                update.last_error_code.value if update.last_error_code else None,
                update.processed_at,
                update.dead_lettered_at,
                update.updated_at,
                event_id,
                expected_status.value,
            ),
        )
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None
