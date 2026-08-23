"""V7 Harness 的受控领域类型。

本模块不连接 FastAPI、PostgreSQL、Redis、LLM 或现有 ToolRegistry。HTTP/存储边界
必须先把外部数据转换为这里的值对象、枚举和不可变 DTO，领域层不接受裸字典。
"""

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import ClassVar
from uuid import UUID, uuid4


class HarnessRole(StrEnum):
    """Harness 内部可识别的人类角色。"""

    AGENT = "AGENT"
    ADMIN = "ADMIN"


class TaskKind(StrEnum):
    """V7-01 已启用的受控任务类别。"""

    SUPPORT_KNOWLEDGE_ASSIST = "SUPPORT_KNOWLEDGE_ASSIST"


class RunState(StrEnum):
    """持久化 AgentRun 的状态。"""

    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    SUCCEEDED = "SUCCEEDED"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class StepKind(StrEnum):
    """Run 内受控步骤类别。"""

    PLAN = "PLAN"
    TOOL_CALL = "TOOL_CALL"
    WORKFLOW_CALL = "WORKFLOW_CALL"
    RUN_APPROVAL = "RUN_APPROVAL"
    ARTIFACT = "ARTIFACT"
    FINALIZE = "FINALIZE"


class StepState(StrEnum):
    """持久化 RunStep 的状态。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CANCELLED = "CANCELLED"


class ApprovalDecision(StrEnum):
    """任务级审批决定；不表达任何业务资金或订单命令。"""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ToolId(StrEnum):
    """V7-01C 唯一允许进入 Harness 的 L0 façade 标识。"""

    KNOWLEDGE_SEARCH_V1 = "knowledge.search.v1"
    POLICY_EXCERPT_V1 = "policy.excerpt.v1"
    TICKET_AUTHORIZED_SUMMARY_V1 = "ticket.authorized_summary.v1"


class HarnessErrorCode(StrEnum):
    """Harness 可以向上层返回的受控错误代码。"""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    PROFILE_NOT_AVAILABLE = "PROFILE_NOT_AVAILABLE"
    PROFILE_MISMATCH = "PROFILE_MISMATCH"
    NOT_AUTHORIZED = "NOT_AUTHORIZED"
    RESOURCE_NOT_AVAILABLE = "RESOURCE_NOT_AVAILABLE"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    LEASE_LOST = "LEASE_LOST"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    APPROVAL_NOT_AVAILABLE = "APPROVAL_NOT_AVAILABLE"


class HarnessDomainError(Exception):
    """Harness 领域错误的公共基类。

    Args:
        code: 受控错误代码，不能包含供应商或用户原始异常。
        message: 面向内部调用方的安全摘要。
    """

    def __init__(self, code: HarnessErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidTransitionError(HarnessDomainError):
    """状态机拒绝当前状态到目标状态的转换。"""

    def __init__(self, message: str) -> None:
        super().__init__(HarnessErrorCode.INVALID_TRANSITION, message)


class ProfileNotAvailableError(HarnessDomainError):
    """服务端 Registry 中不存在或未发布请求的 Profile。"""

    def __init__(self, message: str) -> None:
        super().__init__(HarnessErrorCode.PROFILE_NOT_AVAILABLE, message)


class ProfileMismatchError(HarnessDomainError):
    """持久化 Profile ID、版本或 hash 无法通过 Registry 复核。"""

    def __init__(self, message: str) -> None:
        super().__init__(HarnessErrorCode.PROFILE_MISMATCH, message)


class _ValidatedString:
    """受控字符串值对象的公共校验实现。"""

    __slots__ = ("value",)
    max_length: ClassVar[int] = 128
    pattern: ClassVar[re.Pattern[str] | None] = None

    value: str

    def __init__(self, value: str) -> None:
        """校验并创建不可变字符串值对象。

        Args:
            value: 来自已验证内部边界的文本值。

        Raises:
            ValueError: 值为空、过长、包含控制字符或不符合子类格式时抛出。
        """
        if not isinstance(value, str) or not value or len(value) > self.max_length:
            raise ValueError(f"{type(self).__name__} must be non-empty and within its length limit")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{type(self).__name__} must not contain control characters")
        if self.pattern is not None and self.pattern.fullmatch(value) is None:
            raise ValueError(f"invalid {type(self).__name__} format")
        object.__setattr__(self, "value", value)

    def __setattr__(self, name: str, value: object) -> None:
        """禁止修改或动态扩展值对象。"""
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)

    def __str__(self) -> str:
        """返回数据库或日志边界使用的基础字符串。"""
        return self.value

    def __repr__(self) -> str:
        """返回调试用且不包含隐式类型转换的表示。"""
        return f"{type(self).__name__}({self.value!r})"

    def __eq__(self, other: object) -> bool:
        """只允许同类值对象按值相等。"""
        return type(self) is type(other) and self.value == other.value  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        """使受控值对象可用于不可变集合。"""
        return hash((type(self), self.value))


class _Identifier(_ValidatedString):
    """仅允许稳定、非空白的内部标识符。"""

    __slots__ = ()
    pattern = re.compile(r"[a-z][a-z0-9_.-]*")


class ProfileId(_Identifier):
    """不可变 AgentProfile 标识。"""

    __slots__ = ()


class ProfileHash(_ValidatedString):
    """小写 SHA-256 十六进制 hash。"""

    __slots__ = ()
    max_length = 64
    pattern = re.compile(r"[0-9a-f]{64}")

    @classmethod
    def from_material(cls, material: str) -> "ProfileHash":
        """从确定性 Profile 规范化文本计算 hash。

        Args:
            material: 已稳定排序、无密钥的 Profile 表示。

        Returns:
            对应的 SHA-256 ProfileHash。
        """
        return cls(sha256(material.encode("utf-8")).hexdigest())


class IdempotencyKey(_ValidatedString):
    """客户端重放保护键；服务层仍要绑定 actor、scope 与输入 hash。"""

    __slots__ = ()
    max_length = 256


class StepIdempotencyKey(_ValidatedString):
    """稳定的 Step 副作用去重键。"""

    __slots__ = ()
    max_length = 256


class SafeText(_ValidatedString):
    """限长、无控制字符的脱敏摘要文本。"""

    __slots__ = ()
    max_length = 2000


class OpaqueReference(_ValidatedString):
    """不包含业务正文的服务端资源或结果引用。"""

    __slots__ = ()
    max_length = 256


class WorkerId(_Identifier):
    """持有 Run lease 的受控 worker 标识。"""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class OrganizationId:
    """服务端派生的单组织 UUID。"""

    value: UUID

    def __post_init__(self) -> None:
        """拒绝字符串或客户端伪造的非 UUID 输入。

        Raises:
            TypeError: value 不是 UUID 时抛出。
        """
        if not isinstance(self.value, UUID):
            raise TypeError("OrganizationId.value must be UUID")


@dataclass(frozen=True, slots=True)
class TaskId:
    """Harness Task 的不可枚举 UUID。"""

    value: UUID

    @classmethod
    def new(cls) -> "TaskId":
        """生成新的 Task ID。

        Returns:
            新的随机 UUID 值对象。
        """
        return cls(uuid4())


@dataclass(frozen=True, slots=True)
class RunId:
    """Harness Run 的不可枚举 UUID。"""

    value: UUID

    @classmethod
    def new(cls) -> "RunId":
        """生成新的 Run ID。

        Returns:
            新的随机 UUID 值对象。
        """
        return cls(uuid4())


@dataclass(frozen=True, slots=True)
class StepId:
    """Harness Step 的不可枚举 UUID。"""

    value: UUID

    @classmethod
    def new(cls) -> "StepId":
        """生成新的 Step ID。

        Returns:
            新的随机 UUID 值对象。
        """
        return cls(uuid4())


@dataclass(frozen=True, slots=True)
class ApprovalId:
    """任务级审批的不可枚举 UUID。"""

    value: UUID

    @classmethod
    def new(cls) -> "ApprovalId":
        """生成新的 Approval ID。

        Returns:
            新的随机 UUID 值对象。
        """
        return cls(uuid4())


@dataclass(frozen=True, slots=True)
class ProfileVersion:
    """正整数 Profile 版本号。"""

    value: int

    def __post_init__(self) -> None:
        """校验版本从 1 开始。

        Raises:
            ValueError: value 小于 1 时抛出。
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ValueError("ProfileVersion.value must be a positive integer")


@dataclass(frozen=True, slots=True)
class Version:
    """非负的乐观锁或 Step 版本号。"""

    value: int

    def __post_init__(self) -> None:
        """校验版本不能为负数。

        Raises:
            ValueError: value 不是非负整数时抛出。
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("Version.value must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RunNumber:
    """同一 Task 内从 1 开始的 Run 序号。"""

    value: int

    def __post_init__(self) -> None:
        """校验 Run 序号至少为 1。

        Raises:
            ValueError: value 小于 1 时抛出。
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ValueError("RunNumber.value must be a positive integer")


@dataclass(frozen=True, slots=True)
class NonNegativeInt:
    """非负整数预算、序号或计数。"""

    value: int

    def __post_init__(self) -> None:
        """校验值为非负整数。

        Raises:
            ValueError: value 不符合非负整数约束时抛出。
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
            raise ValueError("NonNegativeInt.value must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class PositiveInt:
    """正整数时间或调用预算。"""

    value: int

    def __post_init__(self) -> None:
        """校验值为正整数。

        Raises:
            ValueError: value 不符合正整数约束时抛出。
        """
        if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 1:
            raise ValueError("PositiveInt.value must be a positive integer")


def _require_aware_utc(value: datetime, field_name: str) -> None:
    """拒绝无时区时间，避免 Run/lease 在本地时区下被错误恢复。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """服务端已经解析的受控知识 source 范围。"""

    source_refs: frozenset[OpaqueReference]

    def __post_init__(self) -> None:
        """确保任务至少绑定一个已批准知识来源。

        Raises:
            ValueError: source_refs 为空时抛出。
        """
        if not self.source_refs:
            raise ValueError("KnowledgeScope.source_refs must not be empty")


@dataclass(frozen=True, slots=True)
class SupportKnowledgeAssistInput:
    """SUPPORT_KNOWLEDGE_ASSIST 的脱敏、版本化输入 DTO。"""

    question_summary: SafeText
    knowledge_scope: KnowledgeScope
    ticket_reference: OpaqueReference | None = None


@dataclass(frozen=True, slots=True)
class TaskConstraints:
    """服务端固定的 Run 时间、调用与只读限制。"""

    max_duration_seconds: PositiveInt
    max_model_calls: PositiveInt
    max_tool_calls: NonNegativeInt
    read_only: bool
    allows_run_approval: bool
    expected_artifact_kinds: frozenset[str]

    def __post_init__(self) -> None:
        """禁止 V7-01 Task 放开业务写能力。

        Raises:
            ValueError: read_only 为 false 时抛出。
        """
        if not self.read_only:
            raise ValueError("V7-01 TaskConstraints must remain read-only")


@dataclass(frozen=True, slots=True)
class AgentProfile:
    """服务端发布的不可变 AgentProfile。"""

    profile_id: ProfileId
    version: ProfileVersion
    profile_hash: ProfileHash
    task_kinds: frozenset[TaskKind]
    allowed_tools: frozenset[ToolId]
    read_only: bool

    @classmethod
    def create(
        cls,
        profile_id: ProfileId,
        version: ProfileVersion,
        task_kinds: frozenset[TaskKind],
        allowed_tools: frozenset[ToolId],
    ) -> "AgentProfile":
        """创建并计算不可变 Profile 的内容 hash。

        Args:
            profile_id: 服务端定义的稳定 Profile 标识。
            version: 不可变版本号。
            task_kinds: 此 Profile 明确允许的任务类别。
            allowed_tools: 此 Profile 可收缩到的 ToolSpec 标识集合。

        Returns:
            含确定性 profile_hash 的只读 Profile。

        Raises:
            ValueError: task_kinds 或 allowed_tools 为空时抛出。
        """
        if not task_kinds or not allowed_tools:
            raise ValueError("AgentProfile requires at least one task kind and one tool")
        material = "|".join(
            (
                str(profile_id),
                str(version.value),
                ",".join(sorted(kind.value for kind in task_kinds)),
                ",".join(sorted(tool.value for tool in allowed_tools)),
                "read_only=true",
            )
        )
        return cls(
            profile_id=profile_id,
            version=version,
            profile_hash=ProfileHash.from_material(material),
            task_kinds=task_kinds,
            allowed_tools=allowed_tools,
            read_only=True,
        )


@dataclass(frozen=True, slots=True)
class AgentTask:
    """已由服务端认证、Profile 和 scope 校验的 Harness 任务。"""

    task_id: TaskId
    organization_id: OrganizationId
    requester_subject_id: int
    requester_role: HarnessRole
    requester_authz_version: OpaqueReference
    profile: AgentProfile
    task_kind: TaskKind
    input: SupportKnowledgeAssistInput
    scope_hash: ProfileHash
    constraints: TaskConstraints
    idempotency_key: IdempotencyKey
    input_hash: ProfileHash
    created_at: datetime
    expires_at: datetime
    retention_expires_at: datetime
    retention_hold: bool = False

    def __post_init__(self) -> None:
        """校验 Task 的时间、主体、Profile 与输入类型边界。

        Raises:
            ValueError: Task 包含未授权角色、错误输入或时间顺序时抛出。
        """
        if isinstance(self.requester_subject_id, bool) or self.requester_subject_id < 1:
            raise ValueError("requester_subject_id must be a positive integer")
        if self.requester_role is not HarnessRole.AGENT:
            raise ValueError("V7-01 SUPPORT_KNOWLEDGE_ASSIST requires an AGENT requester")
        if self.task_kind not in self.profile.task_kinds:
            raise ValueError("Task kind is not allowed by its persisted Profile")
        if not isinstance(self.input, SupportKnowledgeAssistInput):
            raise TypeError("V7-01 task input must be SupportKnowledgeAssistInput")
        for field_name, value in (
            ("created_at", self.created_at),
            ("expires_at", self.expires_at),
            ("retention_expires_at", self.retention_expires_at),
        ):
            _require_aware_utc(value, field_name)
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.retention_expires_at < self.created_at:
            raise ValueError("retention_expires_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class RunBudget:
    """一个 Run 固定的时间、模型和工具调用预算。"""

    deadline_at: datetime
    max_model_calls: PositiveInt
    max_tool_calls: NonNegativeInt
    model_calls_used: NonNegativeInt = NonNegativeInt(0)
    tool_calls_used: NonNegativeInt = NonNegativeInt(0)

    def __post_init__(self) -> None:
        """校验已使用调用次数没有超过固定预算。

        Raises:
            ValueError: deadline 无时区或已使用次数超预算时抛出。
        """
        _require_aware_utc(self.deadline_at, "deadline_at")
        if self.model_calls_used.value > self.max_model_calls.value:
            raise ValueError("model_calls_used must not exceed max_model_calls")
        if self.tool_calls_used.value > self.max_tool_calls.value:
            raise ValueError("tool_calls_used must not exceed max_tool_calls")


@dataclass(frozen=True, slots=True)
class ExecutionVersions:
    """Run 固定使用的模型、Prompt、Profile、工具集与知识版本引用。"""

    model_version: OpaqueReference
    prompt_version: OpaqueReference
    profile_id: ProfileId
    profile_version: ProfileVersion
    profile_hash: ProfileHash
    toolset_version: OpaqueReference
    knowledge_version: OpaqueReference


@dataclass(frozen=True, slots=True)
class RunLease:
    """当前 worker 的 lease 与单调 fencing token。"""

    owner: WorkerId
    expires_at: datetime
    fencing_token: NonNegativeInt

    def __post_init__(self) -> None:
        """校验 lease 过期时间具有时区。

        Raises:
            ValueError: expires_at 无时区时抛出。
        """
        _require_aware_utc(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class AgentRun:
    """Task 的一次可租约、可恢复的持久化执行。"""

    run_id: RunId
    organization_id: OrganizationId
    task_id: TaskId
    run_no: RunNumber
    state: RunState
    version: Version
    current_step_no: NonNegativeInt
    checkpoint_ref: OpaqueReference | None
    lease: RunLease | None
    authz_checked_at: datetime | None
    budget: RunBudget
    execution_versions: ExecutionVersions
    created_at: datetime
    updated_at: datetime
    retention_expires_at: datetime
    retention_hold: bool

    def __post_init__(self) -> None:
        """校验 Run 时间顺序与受控组织边界。

        Raises:
            ValueError: 时间无时区、deadline 非未来或留存不合法时抛出。
        """
        for field_name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("retention_expires_at", self.retention_expires_at),
            ("deadline_at", self.budget.deadline_at),
        ):
            _require_aware_utc(value, field_name)
        if self.authz_checked_at is not None:
            _require_aware_utc(self.authz_checked_at, "authz_checked_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.budget.deadline_at <= self.created_at:
            raise ValueError("deadline_at must be later than created_at")
        if self.retention_expires_at < self.created_at:
            raise ValueError("retention_expires_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class RunStep:
    """Run 内单个计划、工具、审批或最终化步骤。"""

    step_id: StepId
    organization_id: OrganizationId
    run_id: RunId
    step_no: NonNegativeInt
    version: Version
    kind: StepKind
    state: StepState
    idempotency_key: StepIdempotencyKey | None
    capability_id: OpaqueReference | None
    capability_version: OpaqueReference | None
    input_summary_ref: OpaqueReference | None
    result_summary_ref: OpaqueReference | None
    error_class: HarnessErrorCode | None
    attempt_no: NonNegativeInt
    created_at: datetime
    updated_at: datetime
    retention_expires_at: datetime
    retention_hold: bool

    def __post_init__(self) -> None:
        """校验 Step 摘要、时间和能力引用的组合。

        Raises:
            ValueError: 时间顺序或能力版本组合不一致时抛出。
        """
        if (self.capability_id is None) != (self.capability_version is None):
            raise ValueError("capability_id and capability_version must be provided together")
        for field_name, value in (
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("retention_expires_at", self.retention_expires_at),
        ):
            _require_aware_utc(value, field_name)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.retention_expires_at < self.created_at:
            raise ValueError("retention_expires_at must not precede created_at")


@dataclass(frozen=True, slots=True)
class ApprovalPolicy:
    """任务级审批允许的角色集合，不是业务审批策略。"""

    approver_roles: frozenset[HarnessRole]

    def __post_init__(self) -> None:
        """确保审批人角色集合不为空。

        Raises:
            ValueError: approver_roles 为空时抛出。
        """
        if not self.approver_roles:
            raise ValueError("ApprovalPolicy.approver_roles must not be empty")


@dataclass(frozen=True, slots=True)
class RunApprovalRequest:
    """绑定特定 Run Step 版本的任务级继续审批。"""

    approval_id: ApprovalId
    organization_id: OrganizationId
    run_id: RunId
    step_id: StepId
    step_version: Version
    decision: ApprovalDecision
    decision_version: Version
    requested_scope_ref: OpaqueReference
    approver_policy: ApprovalPolicy
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by_subject_id: int | None
    decision_reason_code: OpaqueReference | None
    created_at: datetime
    updated_at: datetime
    retention_expires_at: datetime
    retention_hold: bool

    def __post_init__(self) -> None:
        """执行与数据库 CHECK 相同的审批决定字段校验。

        Raises:
            ValueError: 时间无时区、审批字段或留存时间不一致时抛出。
        """
        for field_name, value in (
            ("requested_at", self.requested_at),
            ("expires_at", self.expires_at),
            ("created_at", self.created_at),
            ("updated_at", self.updated_at),
            ("retention_expires_at", self.retention_expires_at),
        ):
            _require_aware_utc(value, field_name)
        if self.decided_at is not None:
            _require_aware_utc(self.decided_at, "decided_at")
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be later than requested_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.retention_expires_at < self.created_at:
            raise ValueError("retention_expires_at must not precede created_at")
        if self.decided_by_subject_id is not None and self.decided_by_subject_id < 1:
            raise ValueError("decided_by_subject_id must be positive when present")

        if self.decision is ApprovalDecision.PENDING:
            valid = self.decided_at is None and self.decided_by_subject_id is None
        elif self.decision in {ApprovalDecision.APPROVED, ApprovalDecision.REJECTED}:
            valid = self.decided_at is not None and self.decided_by_subject_id is not None
        elif self.decision is ApprovalDecision.REVOKED:
            valid = self.decided_at is not None
        else:
            valid = self.decided_at is not None and self.decided_by_subject_id is None
        if not valid:
            raise ValueError("approval decision fields do not match the decision state")
