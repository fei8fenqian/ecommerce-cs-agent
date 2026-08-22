"""audit_events 的追加式 PostgreSQL Repository。"""

from typing import Protocol, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from service.after_sale_types import (
    AfterSaleCommand,
    ExternalEventId,
    ExternalRefundId,
    IdempotencyKey,
    MerchantRefundRequestNo,
    NonNegativeInt,
    ResourceType,
)
from store.refund_store_types import AsyncConnection, AuditRecord, IdempotencyRecord, NewAuditEvent


class AuditRepository(Protocol):
    async def find_command_idempotency(
        self,
        actor_user_id: int,
        command_name: AfterSaleCommand,
        idempotency_key: IdempotencyKey,
    ) -> IdempotencyRecord | None:
        """按人类命令的幂等作用域读取首次结果。"""

    async def find_callback_deduplication(
        self,
        external_event_id: ExternalEventId,
        merchant_refund_request_no: MerchantRefundRequestNo,
        external_refund_id: ExternalRefundId,
    ) -> AuditRecord | None:
        """按 callback 三元组读取已处理审计事件。"""

    async def append(self, event: NewAuditEvent) -> AuditRecord:
        """追加一条白名单审计事件。"""


class PsycopgAuditRepository:
    """绑定到一个事务连接的追加式审计 Repository。"""

    def __init__(self, connection: AsyncConnection):
        self._connection = connection

    async def find_command_idempotency(
        self,
        actor_user_id: int,
        command_name: AfterSaleCommand,
        idempotency_key: IdempotencyKey,
    ) -> IdempotencyRecord | None:
        """按人类命令幂等作用域读取首次结果。

        Args:
            actor_user_id: 发起命令的人类用户 ID。
            command_name: 受控命令名称。
            idempotency_key: 客户端提供的幂等键。

        Returns:
            首次命令的结果定位；没有记录时返回 None。
        """
        cursor = await self._connection.execute(
            """SELECT id, request_hash, result_resource_type,
                      result_resource_id, result_version
               FROM public.audit_events
               WHERE actor_user_id = %s
                 AND command_name = %s
                 AND idempotency_key = %s
               ORDER BY created_at ASC
               LIMIT 1""",
            (actor_user_id, command_name.value, str(idempotency_key)),
        )
        row = await cursor.fetchone()
        if row is None or row[1] is None or row[2] is None or row[3] is None or row[4] is None:
            return None
        return IdempotencyRecord(
            audit_event_id=cast(UUID, row[0]),
            request_hash=str(row[1]),
            result_resource_type=ResourceType(str(row[2])),
            result_resource_id=cast(UUID, row[3]),
            result_version=NonNegativeInt(cast(int, row[4])),
        )

    async def find_callback_deduplication(
        self,
        external_event_id: ExternalEventId,
        merchant_refund_request_no: MerchantRefundRequestNo,
        external_refund_id: ExternalRefundId,
    ) -> AuditRecord | None:
        """按 callback 三元组读取已处理事件。

        Args:
            external_event_id: 已规范化的外部事件 ID。
            merchant_refund_request_no: 商户退款请求号。
            external_refund_id: 外部退款单 ID。

        Returns:
            已处理的审计记录；没有重复记录时返回 None。
        """
        cursor = await self._connection.execute(
            """SELECT id, resource_type, resource_id, request_hash,
                      result_resource_type, result_resource_id, result_version
               FROM public.audit_events
               WHERE external_event_id = %s
                 AND merchant_refund_request_no = %s
                 AND external_refund_id = %s
               LIMIT 1""",
            (
                str(external_event_id),
                str(merchant_refund_request_no),
                str(external_refund_id),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return AuditRecord(
            id=cast(UUID, row[0]),
            resource_type=ResourceType(str(row[1])),
            resource_id=cast(UUID, row[2]),
            request_hash=str(row[3]) if row[3] is not None else None,
            result_resource_type=ResourceType(str(row[4])) if row[4] else None,
            result_resource_id=cast(UUID, row[5]),
            result_version=NonNegativeInt(cast(int, row[6])) if row[6] is not None else None,
        )

    async def append(self, event: NewAuditEvent) -> AuditRecord:
        """追加审计事件，不提供更新或删除入口。

        Args:
            event: 已通过白名单校验的审计事件 DTO。

        Returns:
            插入后的审计记录定位。

        Raises:
            RuntimeError: 数据库没有返回插入记录时抛出。
        """
        cursor = await self._connection.execute(
            """INSERT INTO public.audit_events (
                    id, resource_type, resource_id, owner_user_id,
                    actor_type, actor_user_id, action, from_status, to_status,
                    expected_version, new_version, reason_code, policy_version,
                    request_id, trace_id, span_id, command_name, idempotency_key,
                    request_hash, result_resource_type, result_resource_id,
                    result_version, external_event_id, merchant_refund_request_no,
                    external_refund_id, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id, resource_type, resource_id, request_hash,
                          result_resource_type, result_resource_id, result_version""",
            (
                event.id,
                event.resource_type.value,
                event.resource_id,
                event.owner_user_id,
                event.actor_type.value,
                event.actor_user_id,
                str(event.action),
                event.from_status.value if event.from_status else None,
                event.to_status.value if event.to_status else None,
                event.expected_version.value if event.expected_version else None,
                event.new_version.value if event.new_version else None,
                str(event.reason_code) if event.reason_code else None,
                str(event.policy_version) if event.policy_version else None,
                event.request_id,
                event.trace_id,
                event.span_id,
                event.command_name.value if event.command_name else None,
                str(event.idempotency_key) if event.idempotency_key else None,
                event.request_hash,
                event.result_resource_type.value if event.result_resource_type else None,
                event.result_resource_id,
                event.result_version.value if event.result_version else None,
                str(event.external_event_id) if event.external_event_id else None,
                str(event.merchant_refund_request_no) if event.merchant_refund_request_no else None,
                str(event.external_refund_id) if event.external_refund_id else None,
                Jsonb(event.metadata.as_json()),
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("audit insert returned no row")
        return AuditRecord(
            id=cast(UUID, row[0]),
            resource_type=ResourceType(str(row[1])),
            resource_id=cast(UUID, row[2]),
            request_hash=str(row[3]) if row[3] is not None else None,
            result_resource_type=ResourceType(str(row[4])) if row[4] else None,
            result_resource_id=cast(UUID | None, row[5]),
            result_version=NonNegativeInt(cast(int, row[6])) if row[6] is not None else None,
        )
