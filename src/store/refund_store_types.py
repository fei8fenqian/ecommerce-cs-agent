"""退款 Store 使用的记录、更新和持久化 DTO。

这些类型位于领域服务和 PostgreSQL 之间，不是 HTTP 请求/响应模型。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service.after_sale_types import (
    ActorType,
    AfterSaleCommand,
    AfterSaleStatus,
    AuditAction,
    ConflictKind,
    Currency,
    DecisionReasonCode,
    DomainErrorCode,
    EvidenceRefs,
    ExternalEventId,
    ExternalRefundId,
    FailureReasonCode,
    FulfillmentStatus,
    IdempotencyKey,
    LegacyOrderId,
    MerchantRefundRequestNo,
    NonNegativeCents,
    NonNegativeInt,
    NormalizedExternalStatus,
    OutboxEventType,
    OutboxStatus,
    PaymentTransactionRef,
    PolicyVersion,
    PositiveCents,
    PositiveInt,
    QualificationPath,
    QualificationResult,
    ReasonCode,
    RefundStatus,
    ResourceType,
    SafeText,
    Source,
)


class AsyncCursor(Protocol):
    """Store 所需的最小异步游标接口。"""

    async def fetchone(self) -> Sequence[object] | None:
        """读取一行查询结果。

        Returns:
            查询结果行；没有结果时返回 None。
        """


class AsyncConnection(Protocol):
    """Store 所需的最小异步数据库连接接口。"""

    async def execute(self, query: str, params: Sequence[object] = ()) -> AsyncCursor:
        """执行参数化 SQL。

        Args:
            query: 固定的 SQL 模板。
            params: SQL 值参数，不包含动态表名或列名。

        Returns:
            可读取结果的异步游标。
        """

    async def commit(self) -> None:
        """提交当前事务。"""

    async def rollback(self) -> None:
        """回滚当前事务。"""


@dataclass(frozen=True, slots=True)
class SafeFactsSnapshot:
    """允许写入 facts_snapshot 的最小事实集合。"""

    payment_succeeded: bool
    payment_amount_cents: NonNegativeCents
    currency: Currency
    paid_at: datetime | None
    fulfillment_status: FulfillmentStatus

    def as_json(self) -> dict[str, object]:
        """转换为白名单 JSON，不包含原始订单或支付响应。"""
        return {
            "payment_succeeded": self.payment_succeeded,
            "payment_amount_cents": self.payment_amount_cents.value,
            "currency": self.currency.value,
            "paid_at": self.paid_at.isoformat() if self.paid_at else None,
            "fulfillment_status": self.fulfillment_status.value,
        }

    @classmethod
    def from_json(cls, value: object) -> "SafeFactsSnapshot | None":
        """从数据库 JSONB 读取并重新校验白名单事实。"""
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("facts snapshot must be a mapping")
        payment_amount = value.get("payment_amount_cents")
        currency = value.get("currency")
        payment_succeeded = value.get("payment_succeeded")
        fulfillment_status = value.get("fulfillment_status")
        paid_at = value.get("paid_at")
        if not isinstance(payment_amount, int) or isinstance(payment_amount, bool):
            raise ValueError("facts snapshot payment amount is invalid")
        if not isinstance(currency, str) or not isinstance(payment_succeeded, bool):
            raise ValueError("facts snapshot payment fields are invalid")
        if not isinstance(fulfillment_status, str):
            raise ValueError("facts snapshot fulfillment status is invalid")
        parsed_paid_at = datetime.fromisoformat(paid_at) if isinstance(paid_at, str) else None
        return cls(
            payment_succeeded=payment_succeeded,
            payment_amount_cents=NonNegativeCents(payment_amount),
            currency=Currency(currency),
            paid_at=parsed_paid_at,
            fulfillment_status=FulfillmentStatus(fulfillment_status),
        )


@dataclass(frozen=True, slots=True)
class AfterSaleRecord:
    id: UUID
    order_id: LegacyOrderId
    customer_user_id: int
    assigned_agent_id: int | None
    status: AfterSaleStatus
    currency: Currency
    payment_transaction_ref: PaymentTransactionRef
    payment_amount_cents: NonNegativeCents
    refund_amount_cents: NonNegativeCents
    reason_code: ReasonCode
    customer_note: SafeText | None
    evidence_refs: EvidenceRefs
    evidence_round: int
    evidence_due_at: datetime | None
    qualification_path: QualificationPath | None
    qualification_result: QualificationResult | None
    policy_version: PolicyVersion | None
    facts_snapshot: SafeFactsSnapshot | None
    version: NonNegativeInt
    submitted_at: datetime
    claimed_at: datetime | None
    claim_expires_at: datetime | None
    reviewed_by_agent_id: int | None
    reviewed_at: datetime | None
    finance_decided_by: int | None
    finance_decided_at: datetime | None
    decision_reason_code: DecisionReasonCode | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class NewAfterSaleRecord:
    id: UUID
    order_id: LegacyOrderId
    customer_user_id: int
    status: AfterSaleStatus
    currency: Currency
    payment_transaction_ref: PaymentTransactionRef
    payment_amount_cents: NonNegativeCents
    refund_amount_cents: NonNegativeCents
    reason_code: ReasonCode
    customer_note: SafeText | None
    evidence_refs: EvidenceRefs
    qualification_path: QualificationPath | None
    qualification_result: QualificationResult | None
    policy_version: PolicyVersion | None
    facts_snapshot: SafeFactsSnapshot | None
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class AfterSaleTransitionUpdate:
    target_status: AfterSaleStatus
    updated_at: datetime
    decision_reason_code: DecisionReasonCode | None = None
    policy_version: PolicyVersion | None = None
    qualification_path: QualificationPath | None = None
    qualification_result: QualificationResult | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvidenceUpdate:
    evidence_refs: EvidenceRefs
    evidence_round: int
    evidence_due_at: datetime | None
    customer_note: SafeText | None
    target_status: AfterSaleStatus
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewUpdate:
    target_status: AfterSaleStatus
    reviewed_by_agent_id: int
    reviewed_at: datetime
    decision_reason_code: DecisionReasonCode
    policy_version: PolicyVersion | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FinanceDecisionUpdate:
    target_status: AfterSaleStatus
    finance_decided_by: int
    finance_decided_at: datetime
    decision_reason_code: DecisionReasonCode
    updated_at: datetime
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RefundRecord:
    id: UUID
    after_sale_request_id: UUID
    payment_transaction_ref: PaymentTransactionRef
    merchant_refund_request_no: MerchantRefundRequestNo
    currency: Currency
    amount_cents: PositiveCents
    status: RefundStatus
    version: NonNegativeInt
    external_refund_id: ExternalRefundId | None
    last_external_event_id: ExternalEventId | None
    last_external_status: NormalizedExternalStatus | None
    failure_reason_code: FailureReasonCode | None
    retryable: bool
    created_at: datetime
    processing_at: datetime | None
    succeeded_at: datetime | None
    failed_at: datetime | None
    last_callback_at: datetime | None
    reconciliation_due_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NewRefundRecord:
    id: UUID
    after_sale_request_id: UUID
    payment_transaction_ref: PaymentTransactionRef
    merchant_refund_request_no: MerchantRefundRequestNo
    currency: Currency
    amount_cents: PositiveCents
    status: RefundStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RefundStatusUpdate:
    target_status: RefundStatus
    updated_at: datetime
    failure_reason_code: FailureReasonCode | None = None
    retryable: bool | None = None
    processing_at: datetime | None = None
    succeeded_at: datetime | None = None
    failed_at: datetime | None = None
    reconciliation_due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VerifiedCallbackUpdate:
    external_event_id: ExternalEventId
    merchant_refund_request_no: MerchantRefundRequestNo
    external_refund_id: ExternalRefundId
    external_status: NormalizedExternalStatus
    amount_cents: PositiveCents
    currency: Currency
    callback_at: datetime


@dataclass(frozen=True, slots=True)
class AuditMetadata:
    """审计 JSONB 的白名单字段。"""

    safe_reason: SafeText | None = None
    evidence_count: NonNegativeInt | None = None
    evidence_round: NonNegativeInt | None = None
    qualification_result: QualificationResult | None = None
    qualification_path: QualificationPath | None = None
    policy_version: PolicyVersion | None = None
    conflict_kind: ConflictKind | None = None
    replay_of_audit_id: UUID | None = None
    external_status: NormalizedExternalStatus | None = None
    retryable: bool | None = None
    source: Source | None = None
    conflict_version: NonNegativeInt | None = None
    first_audit_event_id: UUID | None = None
    outbox_event_id: UUID | None = None
    retry_attempt: NonNegativeInt | None = None
    provider_reason_code: ReasonCode | None = None

    def as_json(self) -> dict[str, object]:
        """只输出非空白名单字段。"""
        values: dict[str, object] = {}
        if self.safe_reason is not None:
            values["safe_reason"] = str(self.safe_reason)
        if self.evidence_count is not None:
            values["evidence_count"] = self.evidence_count.value
        if self.evidence_round is not None:
            values["evidence_round"] = self.evidence_round.value
        if self.qualification_result is not None:
            values["qualification_result"] = self.qualification_result.value
        if self.qualification_path is not None:
            values["qualification_path"] = self.qualification_path.value
        if self.policy_version is not None:
            values["policy_version"] = str(self.policy_version)
        if self.conflict_kind is not None:
            values["conflict_kind"] = self.conflict_kind.value
        if self.replay_of_audit_id is not None:
            values["replay_of_audit_id"] = str(self.replay_of_audit_id)
        if self.external_status is not None:
            values["external_status"] = str(self.external_status)
        if self.retryable is not None:
            values["retryable"] = self.retryable
        if self.source is not None:
            values["source"] = self.source.value
        if self.conflict_version is not None:
            values["conflict_version"] = self.conflict_version.value
        if self.first_audit_event_id is not None:
            values["first_audit_event_id"] = str(self.first_audit_event_id)
        if self.outbox_event_id is not None:
            values["outbox_event_id"] = str(self.outbox_event_id)
        if self.retry_attempt is not None:
            values["retry_attempt"] = self.retry_attempt.value
        if self.provider_reason_code is not None:
            values["provider_reason_code"] = str(self.provider_reason_code)
        return values


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    audit_event_id: UUID
    request_hash: str
    result_resource_type: ResourceType
    result_resource_id: UUID
    result_version: NonNegativeInt


@dataclass(frozen=True, slots=True)
class NewAuditEvent:
    id: UUID
    resource_type: ResourceType
    resource_id: UUID
    owner_user_id: int | None
    actor_type: ActorType
    actor_user_id: int | None
    action: AuditAction
    from_status: AfterSaleStatus | RefundStatus | None
    to_status: AfterSaleStatus | RefundStatus | None
    expected_version: NonNegativeInt | None
    new_version: NonNegativeInt | None
    reason_code: ReasonCode | DecisionReasonCode | FailureReasonCode | None
    policy_version: PolicyVersion | None
    request_id: str | None
    trace_id: str | None
    span_id: str | None
    command_name: AfterSaleCommand | None
    idempotency_key: IdempotencyKey | None
    request_hash: str | None
    result_resource_type: ResourceType | None
    result_resource_id: UUID | None
    result_version: NonNegativeInt | None
    external_event_id: ExternalEventId | None
    merchant_refund_request_no: MerchantRefundRequestNo | None
    external_refund_id: ExternalRefundId | None
    metadata: AuditMetadata


@dataclass(frozen=True, slots=True)
class AuditRecord:
    id: UUID
    resource_type: ResourceType
    resource_id: UUID
    request_hash: str | None
    result_resource_type: ResourceType | None
    result_resource_id: UUID | None
    result_version: NonNegativeInt | None


@dataclass(frozen=True, slots=True)
class RefundOutboxPayload:
    outbox_event_id: UUID
    refund_id: UUID
    after_sale_request_id: UUID
    merchant_refund_request_no: MerchantRefundRequestNo
    payment_transaction_ref: PaymentTransactionRef
    amount_cents: PositiveCents
    currency: Currency
    attempt: PositiveInt
    created_at: datetime

    def as_json(self) -> dict[str, object]:
        """生成禁止包含 PII、签名和原始 callback 的白名单 payload。"""
        return {
            "outbox_event_id": str(self.outbox_event_id),
            "refund_id": str(self.refund_id),
            "after_sale_request_id": str(self.after_sale_request_id),
            "merchant_refund_request_no": str(self.merchant_refund_request_no),
            "payment_transaction_ref": str(self.payment_transaction_ref),
            "amount_cents": self.amount_cents.value,
            "currency": self.currency.value,
            "attempt": self.attempt.value,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_json(cls, value: object) -> "RefundOutboxPayload":
        """从数据库 JSONB 读取并重新构造白名单 payload。"""
        if not isinstance(value, Mapping):
            raise ValueError("outbox payload must be a mapping")
        return cls(
            outbox_event_id=UUID(str(value["outbox_event_id"])),
            refund_id=UUID(str(value["refund_id"])),
            after_sale_request_id=UUID(str(value["after_sale_request_id"])),
            merchant_refund_request_no=MerchantRefundRequestNo(str(value["merchant_refund_request_no"])),
            payment_transaction_ref=PaymentTransactionRef(str(value["payment_transaction_ref"])),
            amount_cents=PositiveCents(value["amount_cents"]),
            currency=Currency(str(value["currency"])),
            attempt=PositiveInt(value["attempt"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class NewRefundOutboxEvent:
    id: UUID
    refund_id: UUID
    event_type: OutboxEventType
    idempotency_key: IdempotencyKey
    payload: RefundOutboxPayload

    def __post_init__(self) -> None:
        """确保事件外层 ID 与白名单 payload 中的 ID 一致。"""
        if self.id != self.payload.outbox_event_id:
            raise ValueError("outbox event id must match payload outbox_event_id")
        if self.refund_id != self.payload.refund_id:
            raise ValueError("outbox refund id must match payload refund_id")


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    id: UUID
    refund_id: UUID
    event_type: OutboxEventType
    idempotency_key: IdempotencyKey
    status: OutboxStatus
    attempt_count: NonNegativeInt
    payload: RefundOutboxPayload
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxDeliveryUpdate:
    status: OutboxStatus
    attempt_count: NonNegativeInt
    updated_at: datetime
    last_error_code: DomainErrorCode | None = None
    processed_at: datetime | None = None
    dead_lettered_at: datetime | None = None
