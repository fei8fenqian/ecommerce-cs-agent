"""after_sale_requests 的参数化 PostgreSQL Repository。"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID

from psycopg.types.json import Jsonb

from service.after_sale_types import (
    AfterSaleStatus,
    Currency,
    DecisionReasonCode,
    EvidenceMediaType,
    EvidenceRef,
    EvidenceRefs,
    LegacyOrderId,
    NonNegativeCents,
    NonNegativeInt,
    ObjectRef,
    PaymentTransactionRef,
    PolicyVersion,
    QualificationPath,
    QualificationResult,
    ReasonCode,
    SafeText,
)
from store.refund_store_types import (
    AfterSaleRecord,
    AfterSaleTransitionUpdate,
    AsyncConnection,
    EvidenceUpdate,
    FinanceDecisionUpdate,
    NewAfterSaleRecord,
    ReviewUpdate,
    SafeFactsSnapshot,
)

_ACTIVE_STATUSES = tuple(
    status.value
    for status in (
        AfterSaleStatus.SUBMITTED,
        AfterSaleStatus.EVIDENCE_PENDING,
        AfterSaleStatus.UNDER_REVIEW,
        AfterSaleStatus.PENDING_CUSTOMER_CONFIRMATION,
        AfterSaleStatus.PENDING_FINANCE_APPROVAL,
        AfterSaleStatus.REFUND_PROCESSING,
    )
)

_RECORD_COLUMNS = """
    id, order_id, customer_user_id, assigned_agent_id, status, currency,
    payment_transaction_ref, payment_amount_cents, refund_amount_cents,
    reason_code, customer_note, evidence_refs, evidence_round, evidence_due_at,
    qualification_path, qualification_result, policy_version, facts_snapshot,
    version, submitted_at, claimed_at, claim_expires_at, reviewed_by_agent_id,
    reviewed_at, finance_decided_by, finance_decided_at, decision_reason_code,
    created_at, updated_at, closed_at
"""


class AfterSaleRepository(Protocol):
    async def get_for_update(self, after_sale_request_id: UUID) -> AfterSaleRecord | None:
        """锁定并读取售后申请。

        Args:
            after_sale_request_id: 售后申请内部 UUID。

        Returns:
            记录或 None。
        """

    async def find_active_by_order(self, order_id: LegacyOrderId) -> AfterSaleRecord | None:
        """读取订单当前有效申请。"""

    async def insert_submitted(self, record: NewAfterSaleRecord) -> AfterSaleRecord:
        """插入已校验的售后申请。"""

    async def update_status(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        transition: AfterSaleTransitionUpdate,
    ) -> AfterSaleRecord | None:
        """按版本更新售后状态。"""

    async def claim_if_available(
        self,
        after_sale_request_id: UUID,
        agent_user_id: int,
        expected_version: NonNegativeInt,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> AfterSaleRecord | None:
        """原子认领未认领的售后申请。"""

    async def release_claim(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        released_by_user_id: int | None,
        now: datetime,
    ) -> AfterSaleRecord | None:
        """释放客服认领或执行系统回收。"""

    async def update_evidence(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: EvidenceUpdate,
    ) -> AfterSaleRecord | None:
        """保存补交证据和客户备注。"""

    async def save_review(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: ReviewUpdate,
    ) -> AfterSaleRecord | None:
        """保存客服审核结果对应的状态字段。"""

    async def save_finance_decision(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: FinanceDecisionUpdate,
    ) -> AfterSaleRecord | None:
        """保存财务决策字段。"""


def _evidence_to_json(refs: EvidenceRefs) -> list[dict[str, object]]:
    return [
        {
            "object_ref": str(ref.object_ref),
            "media_type": ref.media_type.value,
            "size_bytes": ref.size_bytes,
        }
        for ref in refs
    ]


def _evidence_from_json(value: object) -> EvidenceRefs:
    if not isinstance(value, list):
        return tuple()
    return tuple(
        EvidenceRef(
            object_ref=ObjectRef(str(item["object_ref"])),
            media_type=EvidenceMediaType(str(item["media_type"])),
            size_bytes=int(item["size_bytes"]),
        )
        for item in value
        if isinstance(item, Mapping)
    )


def _record_from_row(row: Sequence[object]) -> AfterSaleRecord:
    return AfterSaleRecord(
        id=cast(UUID, row[0]),
        order_id=LegacyOrderId(str(row[1])),
        customer_user_id=cast(int, row[2]),
        assigned_agent_id=cast(int | None, row[3]),
        status=AfterSaleStatus(str(row[4])),
        currency=Currency(str(row[5])),
        payment_transaction_ref=PaymentTransactionRef(str(row[6])),
        payment_amount_cents=NonNegativeCents(cast(int, row[7])),
        refund_amount_cents=NonNegativeCents(cast(int, row[8])),
        reason_code=ReasonCode(str(row[9])),
        customer_note=SafeText(str(row[10])) if row[10] else None,
        evidence_refs=_evidence_from_json(row[11]),
        evidence_round=cast(int, row[12]),
        evidence_due_at=cast(datetime | None, row[13]),
        qualification_path=QualificationPath(str(row[14])) if row[14] else None,
        qualification_result=QualificationResult(str(row[15])) if row[15] else None,
        policy_version=PolicyVersion(str(row[16])) if row[16] else None,
        facts_snapshot=SafeFactsSnapshot.from_json(row[17]),
        version=NonNegativeInt(cast(int, row[18])),
        submitted_at=cast(datetime, row[19]),
        claimed_at=cast(datetime | None, row[20]),
        claim_expires_at=cast(datetime | None, row[21]),
        reviewed_by_agent_id=cast(int | None, row[22]),
        reviewed_at=cast(datetime | None, row[23]),
        finance_decided_by=cast(int | None, row[24]),
        finance_decided_at=cast(datetime | None, row[25]),
        decision_reason_code=DecisionReasonCode(str(row[26])) if row[26] else None,
        created_at=cast(datetime, row[27]),
        updated_at=cast(datetime, row[28]),
        closed_at=cast(datetime | None, row[29]),
    )


class PsycopgAfterSaleRepository:
    """绑定到一个事务连接的售后申请 Repository。"""

    def __init__(self, connection: AsyncConnection):
        self._connection = connection

    async def _fetch_record(self, query: str, params: tuple[object, ...]) -> AfterSaleRecord | None:
        cursor = await self._connection.execute(query, params)
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def get_for_update(self, after_sale_request_id: UUID) -> AfterSaleRecord | None:
        """锁定并读取一条售后申请。

        Args:
            after_sale_request_id: 售后申请内部 UUID。

        Returns:
            锁定后的记录；不存在时返回 None。
        """
        return await self._fetch_record(
            f"SELECT {_RECORD_COLUMNS} FROM public.after_sale_requests WHERE id = %s FOR UPDATE",
            (after_sale_request_id,),
        )

    async def find_active_by_order(self, order_id: LegacyOrderId) -> AfterSaleRecord | None:
        """读取同一订单当前有效的售后申请，不加锁。

        Args:
            order_id: 旧订单编号。

        Returns:
            当前有效申请；没有时返回 None。
        """
        placeholders = ", ".join(["%s"] * len(_ACTIVE_STATUSES))
        return await self._fetch_record(
            f"""SELECT {_RECORD_COLUMNS}
                FROM public.after_sale_requests
                WHERE order_id = %s AND status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT 1""",
            (order_id, *_ACTIVE_STATUSES),
        )

    async def insert_submitted(self, record: NewAfterSaleRecord) -> AfterSaleRecord:
        """插入售后申请并返回数据库生成的完整记录。

        Args:
            record: 已通过服务层事实和权限校验的新申请记录。

        Returns:
            插入后的完整售后申请记录。

        Raises:
            RuntimeError: 数据库没有返回插入记录时抛出。
        """
        cursor = await self._connection.execute(
            f"""INSERT INTO public.after_sale_requests (
                    id, order_id, customer_user_id, status, currency,
                    payment_transaction_ref, payment_amount_cents, refund_amount_cents,
                    reason_code, customer_note, evidence_refs, qualification_path,
                    qualification_result, policy_version, facts_snapshot, submitted_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_RECORD_COLUMNS}""",
            (
                record.id,
                str(record.order_id),
                record.customer_user_id,
                record.status.value,
                record.currency.value,
                str(record.payment_transaction_ref),
                record.payment_amount_cents.value,
                record.refund_amount_cents.value,
                str(record.reason_code),
                str(record.customer_note) if record.customer_note else None,
                Jsonb(_evidence_to_json(record.evidence_refs)),
                record.qualification_path.value if record.qualification_path else None,
                record.qualification_result.value if record.qualification_result else None,
                str(record.policy_version) if record.policy_version else None,
                Jsonb(record.facts_snapshot.as_json()) if record.facts_snapshot else None,
                record.submitted_at,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("after-sale insert returned no row")
        return _record_from_row(row)

    async def update_status(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        transition: AfterSaleTransitionUpdate,
    ) -> AfterSaleRecord | None:
        """按 ID 和版本更新售后状态。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            expected_version: 调用方读取到的旧版本。
            transition: 受控状态更新对象。

        Returns:
            更新后的记录；版本不匹配或资源不存在时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET status = %s,
                   decision_reason_code = COALESCE(%s, decision_reason_code),
                   policy_version = COALESCE(%s, policy_version),
                   qualification_path = COALESCE(%s, qualification_path),
                   qualification_result = COALESCE(%s, qualification_result),
                   version = version + 1,
                   updated_at = %s,
                   closed_at = COALESCE(%s, closed_at)
               WHERE id = %s AND version = %s
               RETURNING """,
            (
                transition.target_status.value,
                transition.decision_reason_code.value if transition.decision_reason_code else None,
                transition.policy_version.value if transition.policy_version else None,
                transition.qualification_path.value if transition.qualification_path else None,
                transition.qualification_result.value if transition.qualification_result else None,
                transition.updated_at,
                transition.closed_at,
                after_sale_request_id,
                expected_version.value,
            ),
        )

    async def _update_and_fetch(self, query_prefix: str, params: tuple[object, ...]) -> AfterSaleRecord | None:
        cursor = await self._connection.execute(f"{query_prefix} {_RECORD_COLUMNS}", params)
        row = await cursor.fetchone()
        return _record_from_row(row) if row is not None else None

    async def claim_if_available(
        self,
        after_sale_request_id: UUID,
        agent_user_id: int,
        expected_version: NonNegativeInt,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> AfterSaleRecord | None:
        """原子认领未认领申请；竞争失败返回 None。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            agent_user_id: 当前客服用户 ID。
            expected_version: 调用方读取到的旧版本。
            claimed_at: 认领发生时间。
            claim_expires_at: 认领过期时间。

        Returns:
            认领后的记录；已经被认领或版本冲突时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET assigned_agent_id = %s,
                   claimed_at = %s,
                   claim_expires_at = %s,
                   version = version + 1,
                   updated_at = %s
               WHERE id = %s
                 AND assigned_agent_id IS NULL
                 AND version = %s
               RETURNING""",
            (
                agent_user_id,
                claimed_at,
                claim_expires_at,
                claimed_at,
                after_sale_request_id,
                expected_version.value,
            ),
        )

    async def release_claim(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        released_by_user_id: int | None,
        now: datetime,
    ) -> AfterSaleRecord | None:
        """释放当前客服认领或执行受控回收。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            expected_version: 调用方读取到的旧版本。
            released_by_user_id: 当前客服 ID；系统回收时为 None。
            now: 释放或回收发生时间。

        Returns:
            释放后的记录；条件不匹配时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET assigned_agent_id = NULL,
                   claimed_at = NULL,
                   claim_expires_at = NULL,
                   version = version + 1,
                   updated_at = %s
               WHERE id = %s
                 AND version = %s
                 AND (%s IS NULL OR assigned_agent_id = %s)
               RETURNING""",
            (now, after_sale_request_id, expected_version.value, released_by_user_id, released_by_user_id),
        )

    async def update_evidence(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: EvidenceUpdate,
    ) -> AfterSaleRecord | None:
        """保存受控证据引用并递增版本。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            expected_version: 调用方读取到的旧版本。
            update: 证据引用和目标状态更新。

        Returns:
            更新后的记录；资源不存在或版本冲突时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET evidence_refs = %s,
                   evidence_round = %s,
                   evidence_due_at = %s,
                   customer_note = %s,
                   status = %s,
                   version = version + 1,
                   updated_at = %s
               WHERE id = %s AND version = %s
               RETURNING""",
            (
                Jsonb(_evidence_to_json(update.evidence_refs)),
                update.evidence_round,
                update.evidence_due_at,
                str(update.customer_note) if update.customer_note else None,
                update.target_status.value,
                update.updated_at,
                after_sale_request_id,
                expected_version.value,
            ),
        )

    async def save_review(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: ReviewUpdate,
    ) -> AfterSaleRecord | None:
        """保存客服审核字段；ReviewOutcome 由服务层写入审计 action。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            expected_version: 调用方读取到的旧版本。
            update: 审核人、原因、策略版本和目标状态。

        Returns:
            更新后的记录；资源不存在或版本冲突时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET status = %s,
                   reviewed_by_agent_id = %s,
                   reviewed_at = %s,
                   decision_reason_code = %s,
                   policy_version = COALESCE(%s, policy_version),
                   version = version + 1,
                   updated_at = %s
               WHERE id = %s AND version = %s
               RETURNING""",
            (
                update.target_status.value,
                update.reviewed_by_agent_id,
                update.reviewed_at,
                update.decision_reason_code.value,
                update.policy_version.value if update.policy_version else None,
                update.updated_at,
                after_sale_request_id,
                expected_version.value,
            ),
        )

    async def save_finance_decision(
        self,
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: FinanceDecisionUpdate,
    ) -> AfterSaleRecord | None:
        """保存财务审批字段并递增版本。

        Args:
            after_sale_request_id: 售后申请内部 UUID。
            expected_version: 调用方读取到的旧版本。
            update: 财务决策人、原因和目标状态。

        Returns:
            更新后的记录；资源不存在或版本冲突时返回 None。
        """
        return await self._update_and_fetch(
            """UPDATE public.after_sale_requests
               SET status = %s,
                   finance_decided_by = %s,
                   finance_decided_at = %s,
                   decision_reason_code = %s,
                   version = version + 1,
                   updated_at = %s,
                   closed_at = COALESCE(%s, closed_at)
               WHERE id = %s AND version = %s
               RETURNING""",
            (
                update.target_status.value,
                update.finance_decided_by,
                update.finance_decided_at,
                update.decision_reason_code.value,
                update.updated_at,
                update.closed_at,
                after_sale_request_id,
                expected_version.value,
            ),
        )
