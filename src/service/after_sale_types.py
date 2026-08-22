"""退款领域的内部类型与命令契约。

本模块只保存领域契约，不访问数据库、不依赖 FastAPI，也不执行业务副作用。
HTTP/JSON 边界负责把外部字符串解析成这里的枚举和值对象。
"""

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import UUID


class ActorType(StrEnum):
    """可以调用退款领域命令的内部主体类型。"""

    CUSTOMER = "CUSTOMER"
    AGENT = "AGENT"
    FINANCE = "FINANCE"
    OPERATOR = "OPERATOR"
    ADMIN = "ADMIN"
    WORKER = "WORKER"
    PAYMENT_GATEWAY = "PAYMENT_GATEWAY"
    SYSTEM = "SYSTEM"


class Source(StrEnum):
    """调用来源；它不是权限本身，仍需和 ActorType 组合校验。"""

    API = "api"
    AGENT = "agent"
    WORKER = "worker"
    CALLBACK = "callback"
    SYSTEM = "system"


# 兼容早期草案中的名称，新的契约统一使用 Source。
CommandSource = Source


class AfterSaleStatus(StrEnum):
    SUBMITTED = "SUBMITTED"  # 客户已经提交售后申请，等待系统或客服处理。
    EVIDENCE_PENDING = "EVIDENCE_PENDING"  # 需要客户补充证据，暂时不能继续审核。
    UNDER_REVIEW = "UNDER_REVIEW"  # 申请正在审核，客服或系统正在核对事实。
    PENDING_CUSTOMER_CONFIRMATION = "PENDING_CUSTOMER_CONFIRMATION"  # 符合自动退款条件，等待客户确认。
    PENDING_FINANCE_APPROVAL = "PENDING_FINANCE_APPROVAL"  # 需要财务人工审批，尚未批准退款。
    REFUND_PROCESSING = "REFUND_PROCESSING"  # 已经批准退款，退款请求正在处理中。
    REFUNDED = "REFUNDED"  # 已确认退款成功，售后流程完成。
    REJECTED = "REJECTED"  # 财务或审核流程拒绝了本次申请。
    CANCELLED = "CANCELLED"  # 客户主动取消，或申请被允许取消。
    EXPIRED = "EXPIRED"  # 客户确认期限已过，申请不能继续自动处理。
    REFUND_EXCEPTION = "REFUND_EXCEPTION"  # 退款失败或对账异常，需要后续人工处理。


class RefundStatus(StrEnum):
    CREATED = "CREATED"  # 退款单已经创建，但还没有开始投递或处理。
    PROCESSING = "PROCESSING"  # 退款请求已经交给退款处理流程。
    SUCCEEDED = "SUCCEEDED"  # 已通过回调或对账确认资金退款成功。
    FAILED = "FAILED"  # 退款处理明确失败。
    RECONCILIATION_EXCEPTION = "RECONCILIATION_EXCEPTION"  # 外部结果无法确认，需要对账处理。


class AfterSaleCommand(StrEnum):
    SUBMIT = "submit_after_sale"
    CANCEL = "cancel_after_sale"
    REQUEST_EVIDENCE = "request_evidence"
    COMPLETE_EVIDENCE = "complete_evidence"
    CLAIM = "claim_after_sale"
    RELEASE_OR_RECOVER_CLAIM = "release_or_recover_claim"
    SUBMIT_REVIEW = "submit_review"
    ENTER_CUSTOMER_CONFIRMATION = "enter_customer_confirmation"
    CONFIRM_AUTO_REFUND = "confirm_auto_refund"
    FINANCE_APPROVE = "finance_approve"
    FINANCE_REJECT = "finance_reject"
    EXPIRE_CUSTOMER_CONFIRMATION = "expire_customer_confirmation"
    RECORD_REFUND_PROCESSING = "record_refund_processing"
    RECORD_REFUND_CALLBACK = "record_refund_callback"
    RECORD_REFUND_FAILURE = "record_refund_failure"
    RECORD_RECONCILIATION_EXCEPTION = "record_reconciliation_exception"


class Currency(StrEnum):
    CNY = "CNY"


class QualificationPath(StrEnum):
    AUTO = "AUTO"
    FINANCE = "FINANCE"


class QualificationResult(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    INDETERMINATE = "INDETERMINATE"


class OwnershipStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    UNMATCHED = "UNMATCHED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class PaymentChannel(StrEnum):
    ALIPAY = "ALIPAY"
    WECHAT = "WECHAT"
    OTHER = "OTHER"


class FulfillmentStatus(StrEnum):
    UNSHIPPED = "UNSHIPPED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ClaimReleaseMode(StrEnum):
    VOLUNTARY_RELEASE = "VOLUNTARY_RELEASE"
    TIMEOUT_RECOVERY = "TIMEOUT_RECOVERY"
    SUPERVISOR_RECOVERY = "SUPERVISOR_RECOVERY"


class ReviewOutcome(StrEnum):
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    RECOMMEND_CUSTOMER_CONFIRMATION = "RECOMMEND_CUSTOMER_CONFIRMATION"
    RECOMMEND_FINANCE_REVIEW = "RECOMMEND_FINANCE_REVIEW"
    RECOMMEND_REJECT = "RECOMMEND_REJECT"


class ResourceType(StrEnum):
    AFTER_SALE_REQUEST = "AFTER_SALE_REQUEST"
    REFUND = "REFUND"
    OUTBOX_EVENT = "OUTBOX_EVENT"
    COMMAND = "COMMAND"


class ConflictKind(StrEnum):
    """审计中允许记录的冲突类别。"""

    VERSION = "VERSION"
    IDEMPOTENCY = "IDEMPOTENCY"
    UNIQUE_CONSTRAINT = "UNIQUE_CONSTRAINT"


class DomainErrorCode(StrEnum):
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_REPLAY = "IDEMPOTENCY_REPLAY"
    INVALID_STATE = "INVALID_STATE"
    RESOURCE_NOT_AVAILABLE = "RESOURCE_NOT_AVAILABLE"
    FORBIDDEN = "FORBIDDEN"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    DUPLICATE_ACTIVE_REQUEST = "DUPLICATE_ACTIVE_REQUEST"
    ORDER_NOT_ELIGIBLE = "ORDER_NOT_ELIGIBLE"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    EVIDENCE_LIMIT_EXCEEDED = "EVIDENCE_LIMIT_EXCEEDED"
    CLAIM_NOT_AVAILABLE = "CLAIM_NOT_AVAILABLE"
    CLAIM_EXPIRED = "CLAIM_EXPIRED"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    RESPONSIBILITY_CONFLICT = "RESPONSIBILITY_CONFLICT"
    INVALID_AMOUNT = "INVALID_AMOUNT"
    INVALID_CURRENCY = "INVALID_CURRENCY"
    EXTERNAL_EVENT_INVALID = "EXTERNAL_EVENT_INVALID"


class _ValidatedValue:
    """受控字符串值对象的公共校验。

    订单号、幂等键、原因码等都不能直接使用任意 str，因此统一经过这里校验。
    """

    __slots__ = ("value",)
    max_length: ClassVar[int] = 128
    pattern: ClassVar[re.Pattern[str] | None] = None

    value: str

    def __setattr__(self, name: str, value: object) -> None:
        """只允许对象初始化时写入属性，之后禁止扩展或修改。"""
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, value: str):
        """创建受控字符串值对象。

        Args:
            value: 要保存的字符串值。

        Raises:
            ValueError: 值为空、超长、包含控制字符或不符合格式时抛出。
        """
        if not isinstance(value, str) or not value or len(value) > self.max_length:
            raise ValueError(f"{type(self).__name__} must be non-empty and within length limit")
        if any(char in "\r\n\t" for char in value):
            raise ValueError(f"{type(self).__name__} must not contain control whitespace")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{type(self).__name__} must not contain control characters")
        if self.pattern is not None and self.pattern.fullmatch(value) is None:
            raise ValueError(f"invalid {type(self).__name__} format")
        object.__setattr__(self, "value", value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.value!r})"

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.value == other.value  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self), self.value))


class _CodeValue(_ValidatedValue):
    __slots__ = ()
    max_length = 64
    pattern = re.compile(r"[A-Za-z0-9_.-]+")


class AuditAction(_CodeValue):
    """审计动作标识，禁止使用任意包含空白或控制字符的文本。"""

    __slots__ = ()


class IdempotencyKey(_ValidatedValue):
    __slots__ = ()
    max_length = 256


class LegacyOrderId(_ValidatedValue):
    __slots__ = ()
    max_length = 20


class SafeText(_ValidatedValue):
    __slots__ = ()
    max_length = 4000


class PolicyVersion(_ValidatedValue):
    __slots__ = ()
    max_length = 64


class ServiceName(_CodeValue):
    __slots__ = ()
    max_length = 64


class ReasonCode(_CodeValue):
    __slots__ = ()
    pass


class EvidenceReasonCode(_CodeValue):
    __slots__ = ()
    pass


class DecisionReasonCode(_CodeValue):
    __slots__ = ()
    pass


class FailureReasonCode(_CodeValue):
    __slots__ = ()
    pass


class ObjectRef(_ValidatedValue):
    __slots__ = ()
    max_length = 512


class PaymentTransactionRef(_ValidatedValue):
    __slots__ = ()
    max_length = 128


class ExternalEventId(_ValidatedValue):
    __slots__ = ()
    max_length = 128


class MerchantRefundRequestNo(_ValidatedValue):
    __slots__ = ()
    max_length = 128


class ExternalRefundId(_ValidatedValue):
    __slots__ = ()
    max_length = 128


class NormalizedExternalStatus(_CodeValue):
    """已经由 callback 适配器规范化的外部状态。"""

    __slots__ = ()
    max_length = 64


class OutboxEventType(StrEnum):
    REFUND_REQUEST_AUTHORIZED = "REFUND_REQUEST_AUTHORIZED"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"


class _Cents:
    """金额或非负整数的基础值对象，避免把负数或 bool 当成版本/金额。"""

    __slots__ = ("value",)
    value: int
    minimum: ClassVar[int] = 0

    def __setattr__(self, name: str, value: object) -> None:
        """只允许对象初始化时写入属性，之后禁止扩展或修改。"""
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, value: int):
        """创建受控整数值对象。

        Args:
            value: 要保存的整数值。

        Raises:
            ValueError: 值不是整数、是 bool 或小于允许的最小值时抛出。
        """
        if not isinstance(value, int) or isinstance(value, bool) or value < self.minimum:
            raise ValueError(f"{type(self).__name__} is invalid")
        object.__setattr__(self, "value", value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.value})"

    def __eq__(self, other: object) -> bool:
        return type(self) is type(other) and self.value == other.value  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self), self.value))


class NonNegativeCents(_Cents):
    __slots__ = ()
    minimum = 0


class PositiveCents(_Cents):
    __slots__ = ()
    minimum = 1


class NonNegativeInt(_Cents):
    __slots__ = ()
    minimum = 0


class PositiveInt(_Cents):
    """必须大于零的受控整数，例如 Outbox 投递尝试次数。"""

    __slots__ = ()
    minimum = 1


class EvidenceMediaType(StrEnum):
    JPEG = "image/jpeg"
    PNG = "image/png"
    WEBP = "image/webp"
    PDF = "application/pdf"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    object_ref: ObjectRef
    media_type: EvidenceMediaType
    size_bytes: int

    def __post_init__(self) -> None:
        if self.size_bytes <= 0 or self.size_bytes > 10 * 1024 * 1024:
            raise ValueError("evidence file size must be between 1 byte and 10 MiB")


EvidenceRefs = tuple[EvidenceRef, ...]


class DomainError(Exception):
    """领域异常，不携带 HTTP 状态码或供应商原始异常。

    API 层以后再把 code 映射成 HTTP 响应；领域层只表达业务失败原因。
    """

    def __init__(
        self,
        code: DomainErrorCode,
        safe_message: str,
        resource_type: ResourceType | None = None,
        resource_id: UUID | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.retryable = retryable


class InvalidStateError(DomainError):
    """状态值非法，或状态机不允许该转换。"""

    def __init__(self, message: str):
        super().__init__(DomainErrorCode.INVALID_STATE, message)


class ActorNotAllowedError(DomainError):
    """当前主体不能执行指定领域命令。"""

    def __init__(self, command: AfterSaleCommand, actor_type: ActorType):
        self.command = command
        self.actor_type = actor_type
        super().__init__(
            DomainErrorCode.FORBIDDEN,
            f"actor {actor_type.value} is not allowed to execute {command.value}",
        )


@dataclass(frozen=True, slots=True)
class Actor:
    """受控调用主体，不是客户端可以直接提交的请求体。

    人类主体靠 actor_user_id 识别，例如客户、客服、财务；
    机器主体靠 service_name 识别，例如 worker、支付适配器、系统任务。
    两种身份不能混用。
    """

    actor_type: ActorType  # 主体角色，例如 CUSTOMER 或 FINANCE。
    actor_user_id: int | None = None  # 人类账号 ID；机器主体必须为空。
    service_name: ServiceName | None = None  # 机器服务名；人类主体必须为空。

    def __post_init__(self) -> None:
        machine_actor = self.actor_type in {
            ActorType.WORKER,
            ActorType.PAYMENT_GATEWAY,
            ActorType.SYSTEM,
        }
        if machine_actor:
            # worker/system/payment gateway 不是普通用户，不能冒充某个人。
            if self.actor_user_id is not None:
                raise ValueError("machine actor must not have actor_user_id")
            if self.service_name is None:
                raise ValueError("machine actor must have a controlled service_name")
        else:
            # 客户、客服、财务等人类角色必须绑定真实 users.id。
            if self.actor_user_id is None or self.actor_user_id <= 0:
                raise ValueError("human actor must have a positive actor_user_id")
            if self.service_name is not None:
                raise ValueError("human actor must not have service_name")


@dataclass(frozen=True, slots=True)
class RequestContext:
    """一次调用的链路信息，不承担权限判断。"""

    request_id: str  # 当前请求的关联 ID。
    trace_id: str | None  # 分布式链路 ID，可以为空。
    span_id: str | None  # 当前调用片段 ID，可以为空。
    source: Source  # API、Agent、Worker、Callback 或 System。

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id must not be empty")


_HUMAN_ACTORS = frozenset(
    {
        ActorType.CUSTOMER,
        ActorType.AGENT,
        ActorType.FINANCE,
        ActorType.OPERATOR,
        ActorType.ADMIN,
    }
)


def validate_actor_source(actor_type: ActorType, source: Source) -> None:
    """拒绝明显不可能的主体/来源组合。"""
    if source is Source.API and actor_type in {ActorType.SYSTEM, ActorType.PAYMENT_GATEWAY}:
        raise ValueError(f"{actor_type.value} cannot use API source")
    if source is Source.CALLBACK and actor_type is ActorType.WORKER:
        raise ValueError("WORKER cannot use CALLBACK source")
    if source in {Source.WORKER, Source.SYSTEM} and actor_type in _HUMAN_ACTORS:
        raise ValueError(f"{actor_type.value} cannot use {source.value} source")


@dataclass(frozen=True, slots=True)
class CommandMeta:
    """所有人类/内部命令共用的元数据。"""

    actor: Actor  # 谁执行命令。
    request: RequestContext  # 从哪里来、属于哪条请求链路。
    idempotency_key: IdempotencyKey  # 重复提交时识别同一命令。
    expected_version: NonNegativeInt  # 乐观锁版本，防止覆盖别人的更新。

    def __post_init__(self) -> None:
        if self.request.source is Source.CALLBACK:
            raise ValueError("callback must use CallbackContext instead of CommandMeta")
        validate_actor_source(self.actor.actor_type, self.request.source)


@dataclass(frozen=True, slots=True)
class CallbackContext:
    actor: Actor
    request: RequestContext
    signature_verified: bool
    merchant_id_verified: bool

    def __post_init__(self) -> None:
        if self.actor.actor_type not in {ActorType.PAYMENT_GATEWAY, ActorType.SYSTEM}:
            raise ValueError("callback actor must be PAYMENT_GATEWAY or SYSTEM")
        if self.request.source is not Source.CALLBACK:
            raise ValueError("callback context must use CALLBACK source")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """状态变更命令的统一成功结果。"""

    resource_type: ResourceType  # 返回的是售后申请、退款单还是其他资源。
    resource_id: UUID  # 内部 UUID，不使用可猜测的外部编号。
    status: AfterSaleStatus | RefundStatus  # 变更后的状态。
    version: NonNegativeInt  # 提交后的版本号。
    idempotent_replay: bool  # 是否是重复命令重放。
    outbox_event_id: UUID | None = None  # 如果产生退款投递任务，则返回其 ID。

    def __post_init__(self) -> None:
        if self.resource_type is ResourceType.AFTER_SALE_REQUEST and not isinstance(self.status, AfterSaleStatus):
            raise ValueError("after-sale result must use AfterSaleStatus")
        if self.resource_type is ResourceType.REFUND and not isinstance(self.status, RefundStatus):
            raise ValueError("refund result must use RefundStatus")


@dataclass(frozen=True, slots=True)
class PaymentFact:
    payment_channel: PaymentChannel
    payment_transaction_ref: PaymentTransactionRef
    payment_succeeded: bool
    amount_cents: NonNegativeCents
    currency: Currency
    paid_at: datetime | None


@dataclass(frozen=True, slots=True)
class FulfillmentFact:
    fulfillment_status: FulfillmentStatus
    shipped_at: datetime | None
    delivered_at: datetime | None


@dataclass(frozen=True, slots=True)
class OwnershipFact:
    ownership_status: OwnershipStatus
    customer_user_id: int | None


@dataclass(frozen=True, slots=True)
class OrderEligibilityFacts:
    """资格判断所需的只读事实快照，不是资格结论。"""

    ownership: OwnershipFact
    payment: PaymentFact
    fulfillment: FulfillmentFact


class OrderEligibilityFactProvider(Protocol):
    async def get_facts(self, *, order_id: LegacyOrderId, customer_user_id: int) -> OrderEligibilityFacts:
        """只读获取已验证事实，不读取或修改资格结果。

        Args:
            order_id: 要检查的旧订单编号。
            customer_user_id: 当前已认证客户的用户 ID。

        Returns:
            包含归属、支付和履约事实的快照。

        Raises:
            Exception: 具体实现应将外部依赖失败转换为受控领域异常。
        """


@dataclass(frozen=True, slots=True)
class AfterSaleEligibilityInput:
    order_id: LegacyOrderId
    customer_user_id: int
    reason_code: ReasonCode


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """资格提供者的受控结论。

    事实快照由 Provider 提供，具体政策由后续阶段实现；这里不调用 LLM。
    """

    result: QualificationResult  # ELIGIBLE、INELIGIBLE 或 INDETERMINATE。
    path: QualificationPath | None  # AUTO、FINANCE；不确定时必须为空。
    policy_version: PolicyVersion  # 产生这个结论的政策版本。
    reason_code: DecisionReasonCode  # 受控原因码，不是自由文本。

    def __post_init__(self) -> None:
        if self.result is QualificationResult.INDETERMINATE and self.path is not None:
            raise ValueError("INDETERMINATE result must not select a qualification path")
        if self.result is QualificationResult.ELIGIBLE and self.path is not QualificationPath.AUTO:
            raise ValueError("ELIGIBLE result must select AUTO path")
        if self.result is QualificationResult.INELIGIBLE and self.path is not QualificationPath.FINANCE:
            raise ValueError("INELIGIBLE result must select FINANCE path")


class EligibilityProvider(Protocol):
    async def evaluate(
        self,
        *,
        request: AfterSaleEligibilityInput,
        facts: OrderEligibilityFacts,
        now: datetime,
    ) -> EligibilityDecision:
        """根据已验证事实和注入时间返回资格结果；不实现具体政策。

        Args:
            request: 已验证的售后申请上下文。
            facts: 只读事实提供者返回的事实快照。
            now: 由 Clock 提供的当前 UTC 时间。

        Returns:
            受控的资格结论和资格路径。

        Raises:
            Exception: 具体实现应将依赖失败转换为受控领域异常。
        """


@dataclass(frozen=True, slots=True)
class SubmitAfterSaleCommand:
    """客户提交售后申请时使用的不可变命令对象。"""

    meta: CommandMeta  # 身份、请求链路、幂等键和版本号。
    order_id: LegacyOrderId  # 只作为订单引用，不代表已确认归属。
    reason_code: ReasonCode  # 客户选择的受控售后原因。
    customer_note: SafeText | None  # 可选安全文本，不是资格依据。
    evidence_refs: EvidenceRefs  # 受控证据引用，不包含文件原文。

    def __post_init__(self) -> None:
        if len(self.evidence_refs) > 5:
            raise ValueError("at most five evidence references are allowed")


@dataclass(frozen=True, slots=True)
class CancelAfterSaleCommand:
    """客户取消售后申请的命令。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    reason_code: DecisionReasonCode


@dataclass(frozen=True, slots=True)
class ClaimAfterSaleCommand:
    """客服认领售后申请的命令。"""

    meta: CommandMeta
    after_sale_request_id: UUID


@dataclass(frozen=True, slots=True)
class ReleaseOrRecoverClaimCommand:
    """客服主动释放或系统回收认领的命令。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    reason_code: DecisionReasonCode
    mode: ClaimReleaseMode


@dataclass(frozen=True, slots=True)
class SubmitEvidenceCommand:
    """客户补交证据的命令。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    evidence_refs: EvidenceRefs
    customer_note: SafeText | None

    def __post_init__(self) -> None:
        if len(self.evidence_refs) == 0 or len(self.evidence_refs) > 5:
            raise ValueError("each evidence submission must contain one to five references")


@dataclass(frozen=True, slots=True)
class SubmitReviewCommand:
    """客服提交审核意见的命令；意见不是退款批准。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    review_outcome: ReviewOutcome
    reason_code: DecisionReasonCode
    review_note: SafeText | None
    policy_version: PolicyVersion | None


@dataclass(frozen=True, slots=True)
class FinanceApproveCommand:
    """财务批准退款的命令；金额不能由命令传入。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    decision_reason_code: DecisionReasonCode
    decision_note: SafeText | None


@dataclass(frozen=True, slots=True)
class FinanceRejectCommand:
    """财务驳回售后申请的命令。"""

    meta: CommandMeta
    after_sale_request_id: UUID
    decision_reason_code: DecisionReasonCode
    decision_note: SafeText | None


COMMAND_ACTORS: dict[AfterSaleCommand, frozenset[ActorType]] = {
    AfterSaleCommand.SUBMIT: frozenset({ActorType.CUSTOMER}),
    AfterSaleCommand.CANCEL: frozenset({ActorType.CUSTOMER}),
    AfterSaleCommand.REQUEST_EVIDENCE: frozenset({ActorType.AGENT, ActorType.SYSTEM}),
    AfterSaleCommand.COMPLETE_EVIDENCE: frozenset({ActorType.CUSTOMER}),
    AfterSaleCommand.CLAIM: frozenset({ActorType.AGENT}),
    AfterSaleCommand.RELEASE_OR_RECOVER_CLAIM: frozenset({ActorType.AGENT, ActorType.SYSTEM}),
    AfterSaleCommand.SUBMIT_REVIEW: frozenset({ActorType.AGENT}),
    AfterSaleCommand.ENTER_CUSTOMER_CONFIRMATION: frozenset({ActorType.AGENT, ActorType.SYSTEM}),
    AfterSaleCommand.CONFIRM_AUTO_REFUND: frozenset({ActorType.CUSTOMER}),
    AfterSaleCommand.FINANCE_APPROVE: frozenset({ActorType.FINANCE}),
    AfterSaleCommand.FINANCE_REJECT: frozenset({ActorType.FINANCE}),
    AfterSaleCommand.EXPIRE_CUSTOMER_CONFIRMATION: frozenset({ActorType.SYSTEM, ActorType.WORKER}),
    AfterSaleCommand.RECORD_REFUND_PROCESSING: frozenset({ActorType.SYSTEM, ActorType.WORKER}),
    AfterSaleCommand.RECORD_REFUND_CALLBACK: frozenset({ActorType.PAYMENT_GATEWAY, ActorType.WORKER}),
    AfterSaleCommand.RECORD_REFUND_FAILURE: frozenset({ActorType.PAYMENT_GATEWAY, ActorType.WORKER, ActorType.SYSTEM}),
    AfterSaleCommand.RECORD_RECONCILIATION_EXCEPTION: frozenset(
        {ActorType.PAYMENT_GATEWAY, ActorType.WORKER, ActorType.SYSTEM}
    ),
}


@dataclass(frozen=True, slots=True)
class UtcClock:
    """默认 UTC 时钟；测试可注入固定时钟替代它。"""

    def now(self) -> datetime:
        """获取当前 UTC 时间。

        Returns:
            带 UTC 时区信息的 datetime。
        """
        return datetime.now(timezone.utc)


class Clock(Protocol):
    def now(self) -> datetime:
        """返回带时区的 UTC 时间。

        Returns:
            当前 UTC 时间。
        """
