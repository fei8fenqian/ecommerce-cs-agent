"""refunds 的参数化 PostgreSQL Repository。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from service.after_sale_types import (
    Currency,
    ExternalEventId,
    ExternalRefundId,
    FailureReasonCode,
    MerchantRefundRequestNo,
    NonNegativeInt,
    NormalizedExternalStatus,
    PaymentTransactionRef,
    PositiveCents,
    RefundStatus,
)
from store.refund_store_types import (
    AsyncConnection,
    NewRefundRecord,
    RefundRecord,
    RefundStatusUpdate,
    VerifiedCallbackUpdate,
)

_RECORD_COLUMNS = """
    id, after_sale_request_id, payment_transaction_ref, merchant_refund_request_no,
    currency, amount_cents, status, version, external_refund_id,
    last_external_event_id, last_external_status, failure_reason_code, retryable,
    created_at, processing_at, succeeded_at, failed_at, last_callback_at,
    reconciliation_due_at, updated_at
"""


class RefundRepository(Protocol):
    async def get_for_update(self, refund_id: UUID) -> RefundRecord | None:
        """锁定并读取退款单。

        Args:
            refund_id: 退款单内部 UUID。

        Returns:
            退款记录或 None。
        """

    async def get_for_update_by_after_sale(self, after_sale_request_id: UUID) -> RefundRecord | None:
        """锁定并读取售后申请对应的唯一退款单。

        Args:
            after_sale_request_id: 售后申请内部 UUID。

        Returns:
            退款记录或 None。
        """

    async def insert_created(self, record: NewRefundRecord) -> RefundRecord:
        """插入已校验的退款单并返回记录。"""

    async def update_status(
        self,
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: RefundStatusUpdate,
    ) -> RefundRecord | None:
        """按版本更新退款状态和时间字段。"""

    async def save_verified_callback(
        self,
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: VerifiedCallbackUpdate,
    ) -> RefundRecord | None:
        """保存已经过 callback 上下文验证的外部结果。"""


def _record_from_row(row: Sequence[object]) -> RefundRecord:
    return RefundRecord(
        id=cast(UUID, row[0]),
        after_sale_request_id=cast(UUID, row[1]),
        payment_transaction_ref=PaymentTransactionRef(str(row[2])),
        merchant_refund_request_no=MerchantRefundRequestNo(str(row[3])),
        currency=Currency(str(row[4])),
        amount_cents=PositiveCents(cast(int, row[5])),
        status=RefundStatus(str(row[6])),
        version=NonNegativeInt(cast(int, row[7])),
        external_refund_id=ExternalRefundId(str(row[8])) if row[8] else None,
        last_external_event_id=ExternalEventId(str(row[9])) if row[9] else None,
        last_external_status=NormalizedExternalStatus(str(row[10])) if row[10] else None,
        failure_reason_code=FailureReasonCode(str(row[11])) if row[11] else None,
        retryable=cast(bool, row[12]),
        created_at=cast(datetime, row[13]),
        processing_at=cast(datetime | None, row[14]),
        succeeded_at=cast(datetime | None, row[15]),
        failed_at=cast(datetime | None, row[16]),
        last_callback_at=cast(datetime | None, row[17]),
        reconciliation_due_at=cast(datetime | None, row[18]),
        updated_at=cast(datetime, row[19]),
    )


class PsycopgRefundRepository:
    """绑定到一个事务连接的退款单 Repository。"""

    def __init__(self, connection: AsyncConnection):
        self._connection = connection

    async def _fetch_record(self, query: str, params: tuple[object, ...]) -> RefundRecord | None:
        cursor = await self._connection.execute(query, params)
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def get_for_update(self, refund_id: UUID) -> RefundRecord | None:
        """锁定并读取退款单。

        Args:
            refund_id: 退款单内部 UUID。

        Returns:
            锁定后的退款记录；不存在时返回 None。
        """
        return await self._fetch_record(
            f"SELECT {_RECORD_COLUMNS} FROM public.refunds WHERE id = %s FOR UPDATE",
            (refund_id,),
        )

    async def get_for_update_by_after_sale(self, after_sale_request_id: UUID) -> RefundRecord | None:
        """按售后申请锁定其唯一退款单。

        Args:
            after_sale_request_id: 售后申请内部 UUID。

        Returns:
            锁定后的退款记录；尚未创建时返回 None。
        """
        return await self._fetch_record(
            f"""SELECT {_RECORD_COLUMNS}
                FROM public.refunds
                WHERE after_sale_request_id = %s
                FOR UPDATE""",
            (after_sale_request_id,),
        )

    async def insert_created(self, record: NewRefundRecord) -> RefundRecord:
        """创建退款单并返回完整记录。

        Args:
            record: 已由应用服务校验金额、币种和关联售后申请的新退款记录。

        Returns:
            插入后的完整退款记录。

        Raises:
            RuntimeError: 数据库没有返回插入记录时抛出。
        """
        cursor = await self._connection.execute(
            f"""INSERT INTO public.refunds (
                    id, after_sale_request_id, payment_transaction_ref,
                    merchant_refund_request_no, currency, amount_cents, status, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_RECORD_COLUMNS}""",
            (
                record.id,
                record.after_sale_request_id,
                str(record.payment_transaction_ref),
                str(record.merchant_refund_request_no),
                record.currency.value,
                record.amount_cents.value,
                record.status.value,
                record.created_at,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("refund insert returned no row")
        return _record_from_row(row)

    async def update_status(
        self,
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: RefundStatusUpdate,
    ) -> RefundRecord | None:
        """按 ID 和版本更新退款状态。

        Args:
            refund_id: 退款单内部 UUID。
            expected_version: 调用方读取到的旧版本。
            update: 受控的目标状态和失败信息。

        Returns:
            更新后的记录；资源不存在或版本冲突时返回 None。
        """
        cursor = await self._connection.execute(
            f"""UPDATE public.refunds
                SET status = %s,
                    version = version + 1,
                    failure_reason_code = COALESCE(%s, failure_reason_code),
                    retryable = COALESCE(%s, retryable),
                    processing_at = %s,
                    succeeded_at = %s,
                    failed_at = %s,
                    reconciliation_due_at = %s,
                    updated_at = %s
                WHERE id = %s AND version = %s
                RETURNING {_RECORD_COLUMNS}""",
            (
                update.target_status.value,
                update.failure_reason_code.value if update.failure_reason_code else None,
                update.retryable,
                update.processing_at,
                update.succeeded_at,
                update.failed_at,
                update.reconciliation_due_at,
                update.updated_at,
                refund_id,
                expected_version.value,
            ),
        )
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def save_verified_callback(
        self,
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: VerifiedCallbackUpdate,
    ) -> RefundRecord | None:
        """保存已验证 callback 的外部标识和状态摘要。

        Args:
            refund_id: 退款单内部 UUID。
            expected_version: 调用方读取到的旧版本。
            update: 已通过 CallbackContext 校验的 callback 结果。

        Returns:
            更新后的记录；事实、版本或资源不匹配时返回 None。
        """
        cursor = await self._connection.execute(
            f"""UPDATE public.refunds
                SET external_refund_id = %s,
                    last_external_event_id = %s,
                    last_external_status = %s,
                    last_callback_at = %s,
                    updated_at = %s,
                    version = version + 1
                WHERE id = %s
                  AND version = %s
                  AND amount_cents = %s
                  AND currency = %s
                  AND merchant_refund_request_no = %s
                RETURNING {_RECORD_COLUMNS}""",
            (
                str(update.external_refund_id),
                str(update.external_event_id),
                str(update.external_status),
                update.callback_at,
                update.callback_at,
                refund_id,
                expected_version.value,
                update.amount_cents.value,
                update.currency.value,
                str(update.merchant_refund_request_no),
            ),
        )
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None
