"""V7-01C 的纯内存 Harness 安全契约骨架。

这里的 InMemoryHarness 只是冻结 lease、fencing、scope、审批和留存规则的测试替身，
不代表 PostgreSQL Repository 或真实 worker 已完成。后续持久化/Runner 实现必须复用
这些场景验证真实并发与恢复行为。
"""

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from harness.profiles import AgentProfileRegistry
from harness.types import ApprovalDecision, HarnessDomainError, OrganizationId, RunState, TaskKind

LEASE_SECONDS = 90
RENEW_INTERVAL_SECONDS = 30
SAFETY_MARGIN_SECONDS = 15
RUN_RETENTION_DAYS = 30


class FakeClock:
    """可由测试显式推进的 UTC 时钟。"""

    def __init__(self, now: datetime) -> None:
        """创建固定起点的测试时钟。

        Args:
            now: 必须带 UTC 时区的当前时间。

        Raises:
            ValueError: now 没有时区时抛出。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("FakeClock requires a timezone-aware time")
        self._now = now

    def now(self) -> datetime:
        """返回当前假时间。

        Returns:
            当前 UTC 时间，不读取系统时钟。
        """
        return self._now

    def advance(self, seconds: int) -> None:
        """将假时间向前推进指定秒数。

        Args:
            seconds: 非负推进秒数。

        Raises:
            ValueError: seconds 为负数时抛出。
        """
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._now += timedelta(seconds=seconds)


class ContractRejectedError(HarnessDomainError):
    """内存契约替身拒绝一次不安全操作。"""

    def __init__(self, message: str) -> None:
        """创建不包含敏感数据的拒绝错误。

        Args:
            message: 安全的契约违反摘要。
        """
        from harness.types import HarnessErrorCode

        super().__init__(HarnessErrorCode.VERSION_CONFLICT, message)


@dataclass(frozen=True, slots=True)
class _Lease:
    """内存 Run 的当前租约快照。"""

    owner: str
    expires_at: datetime
    fencing_token: int


@dataclass(frozen=True, slots=True)
class _Approval:
    """内存任务级审批快照。"""

    decision: ApprovalDecision
    expires_at: datetime


class InMemoryHarness:
    """仅用于冻结 V7-01C 安全语义的最小内存替身。"""

    def __init__(self, organization_id: OrganizationId, clock: FakeClock) -> None:
        """创建一个属于单一组织的可控 Run。

        Args:
            organization_id: 由服务端派生的唯一组织范围。
            clock: 所有 lease 与审批判断共享的假时钟。
        """
        self.organization_id = organization_id
        self.clock = clock
        self.run_id: UUID = uuid4()
        self.version = 0
        self.state = RunState.RECEIVED
        self.lease: _Lease | None = None
        self.cancelled = False
        self.permission_available = True
        self.completed_step_keys: set[str] = set()
        self.approval: _Approval | None = None
        self.retention_expires_at = clock.now() + timedelta(days=RUN_RETENTION_DAYS)
        self.retention_hold = False

    def acquire_lease(self, organization_id: OrganizationId, worker_id: str) -> _Lease:
        """原子语义地为一个 worker 获取 90 秒 lease。

        Args:
            organization_id: 调用方服务端 scope。
            worker_id: 内部 worker 标识。

        Returns:
            新 lease 快照，fencing token 单调递增。

        Raises:
            ContractRejectedError: scope 不符、Run 已取消或未过期 lease 已存在时抛出。
        """
        self._require_scope(organization_id)
        if self.cancelled:
            raise ContractRejectedError("cancelled Run cannot acquire a lease")
        if self.lease is not None and self.lease.expires_at > self.clock.now():
            raise ContractRejectedError("an active lease already exists")
        next_token = 1 if self.lease is None else self.lease.fencing_token + 1
        self.lease = _Lease(worker_id, self.clock.now() + timedelta(seconds=LEASE_SECONDS), next_token)
        self.version += 1
        return self.lease

    def renew_lease(self, organization_id: OrganizationId, worker_id: str, fencing_token: int) -> _Lease:
        """按当前 token 续租 90 秒。

        Args:
            organization_id: 调用方服务端 scope。
            worker_id: 请求续租的 worker。
            fencing_token: worker 当前持有 token。

        Returns:
            续租后的 lease；token 不变。

        Raises:
            ContractRejectedError: scope、worker、token 或 lease 有效期不匹配时抛出。
        """
        self._require_current_lease(organization_id, worker_id, fencing_token)
        assert self.lease is not None
        self.lease = _Lease(worker_id, self.clock.now() + timedelta(seconds=LEASE_SECONDS), fencing_token)
        self.version += 1
        return self.lease

    def can_dispatch(self, organization_id: OrganizationId, tool_timeout_seconds: int) -> bool:
        """判断当前 lease 是否足以覆盖工具 timeout 加 15 秒安全余量。

        Args:
            organization_id: 调用方服务端 scope。
            tool_timeout_seconds: ToolSpec 的固定 timeout，模型不可指定。

        Returns:
            剩余 lease 至少等于 timeout 加安全余量时返回 true。

        Raises:
            ContractRejectedError: scope 不符、无有效 lease、权限收缩或 Run 已取消时抛出。
        """
        self._require_scope(organization_id)
        if self.cancelled or not self.permission_available:
            raise ContractRejectedError("Run is no longer permitted to dispatch")
        if self.lease is None or self.lease.expires_at <= self.clock.now():
            raise ContractRejectedError("an active lease is required before dispatch")
        remaining = (self.lease.expires_at - self.clock.now()).total_seconds()
        return remaining >= tool_timeout_seconds + SAFETY_MARGIN_SECONDS

    def write_step_result(
        self,
        organization_id: OrganizationId,
        worker_id: str,
        fencing_token: int,
        step_idempotency_key: str,
    ) -> bool:
        """用当前 lease 写入一次 Step 结果，并拒绝重复或迟到写入。

        Args:
            organization_id: 调用方服务端 scope。
            worker_id: 回写结果的 worker。
            fencing_token: 发起调用时持有的 token。
            step_idempotency_key: 同一 Step 的稳定去重键。

        Returns:
            首次安全写入返回 true；重复 Step 返回 false。

        Raises:
            ContractRejectedError: scope、lease、fencing、取消或权限条件不满足时抛出。
        """
        self._require_current_lease(organization_id, worker_id, fencing_token)
        if self.cancelled or not self.permission_available:
            raise ContractRejectedError("Run can no longer accept a Step result")
        if step_idempotency_key in self.completed_step_keys:
            return False
        self.completed_step_keys.add(step_idempotency_key)
        self.version += 1
        return True

    def cancel(self, organization_id: OrganizationId) -> None:
        """取消 Run，使其不能继续派发或写入结果。

        Args:
            organization_id: 调用方服务端 scope。
        """
        self._require_scope(organization_id)
        self.cancelled = True
        self.state = RunState.CANCELLED
        self.version += 1

    def set_permission_available(self, available: bool) -> None:
        """模拟恢复时重新计算的权限交集结果。

        Args:
            available: 当前 actor/profile/scope/Task 交集是否仍存在。
        """
        self.permission_available = available

    def request_approval(self, expires_in_seconds: int) -> None:
        """创建仅用于任务继续的 PENDING 审批。

        Args:
            expires_in_seconds: 服务端固定审批有效期。

        Raises:
            ValueError: 有效期不是正数时抛出。
        """
        if expires_in_seconds < 1:
            raise ValueError("approval expiry must be positive")
        self.approval = _Approval(
            ApprovalDecision.PENDING,
            self.clock.now() + timedelta(seconds=expires_in_seconds),
        )
        self.state = RunState.WAITING_APPROVAL

    def approve_and_resume(self, organization_id: OrganizationId) -> None:
        """只在未过期、权限仍有效的审批下恢复 Run。

        Args:
            organization_id: 调用方服务端 scope。

        Raises:
            ContractRejectedError: 审批不存在、已过期或权限已收缩时抛出。
        """
        self._require_scope(organization_id)
        if self.approval is None or self.approval.decision is not ApprovalDecision.PENDING:
            raise ContractRejectedError("no pending approval can resume this Run")
        if self.approval.expires_at <= self.clock.now():
            self.approval = _Approval(ApprovalDecision.EXPIRED, self.approval.expires_at)
            raise ContractRejectedError("expired approval cannot resume this Run")
        if not self.permission_available:
            raise ContractRejectedError("permission shrink prevents Run recovery")
        self.approval = _Approval(ApprovalDecision.APPROVED, self.approval.expires_at)
        self.state = RunState.RUNNING

    def _require_scope(self, organization_id: OrganizationId) -> None:
        """拒绝不同 organization_id 的读取或写入。"""
        if organization_id != self.organization_id:
            raise ContractRejectedError("resource is not available in this organization")

    def _require_current_lease(
        self,
        organization_id: OrganizationId,
        worker_id: str,
        fencing_token: int,
    ) -> None:
        """确保 worker 仍持有未过期且 token 匹配的 lease。"""
        self._require_scope(organization_id)
        if self.lease is None:
            raise ContractRejectedError("Run has no lease")
        if self.lease.owner != worker_id or self.lease.fencing_token != fencing_token:
            raise ContractRejectedError("stale fencing token cannot write the Run")
        if self.lease.expires_at <= self.clock.now():
            raise ContractRejectedError("expired lease cannot write the Run")


def _harness() -> tuple[InMemoryHarness, OrganizationId, FakeClock]:
    organization_id = OrganizationId(uuid4())
    clock = FakeClock(datetime(2026, 8, 24, 9, 0, tzinfo=UTC))
    return InMemoryHarness(organization_id, clock), organization_id, clock


def test_single_organization_scope_rejects_another_organization() -> None:
    harness, organization_id, _ = _harness()

    harness.acquire_lease(organization_id, "worker-a")
    with pytest.raises(ContractRejectedError, match="resource is not available"):
        harness.acquire_lease(OrganizationId(uuid4()), "worker-b")


def test_profile_is_server_selected_and_cannot_be_overridden_by_client_input() -> None:
    registry = AgentProfileRegistry()

    profile = registry.profile_for_task(TaskKind.SUPPORT_KNOWLEDGE_ASSIST)
    assert profile.profile_id.value == "support-knowledge-readonly"
    assert profile.version.value == 1
    assert "client_profile_id" not in AgentProfileRegistry.profile_for_task.__annotations__


def test_lease_defaults_renewal_and_safety_margin() -> None:
    harness, organization_id, clock = _harness()
    lease = harness.acquire_lease(organization_id, "worker-a")
    assert (lease.expires_at - clock.now()).total_seconds() == LEASE_SECONDS
    assert harness.can_dispatch(organization_id, tool_timeout_seconds=75) is True
    assert harness.can_dispatch(organization_id, tool_timeout_seconds=76) is False

    clock.advance(RENEW_INTERVAL_SECONDS)
    renewed = harness.renew_lease(organization_id, "worker-a", lease.fencing_token)
    assert renewed.fencing_token == lease.fencing_token
    assert (renewed.expires_at - clock.now()).total_seconds() == LEASE_SECONDS


def test_old_fencing_token_cannot_write_after_worker_takeover() -> None:
    harness, organization_id, clock = _harness()
    old_lease = harness.acquire_lease(organization_id, "worker-a")
    clock.advance(LEASE_SECONDS)
    new_lease = harness.acquire_lease(organization_id, "worker-b")

    with pytest.raises(ContractRejectedError, match="stale fencing token"):
        harness.write_step_result(organization_id, "worker-a", old_lease.fencing_token, "step-1")
    assert harness.write_step_result(organization_id, "worker-b", new_lease.fencing_token, "step-1") is True


def test_duplicate_step_and_cancel_cannot_create_more_effects() -> None:
    harness, organization_id, _ = _harness()
    lease = harness.acquire_lease(organization_id, "worker-a")
    assert harness.write_step_result(organization_id, "worker-a", lease.fencing_token, "step-1") is True
    assert harness.write_step_result(organization_id, "worker-a", lease.fencing_token, "step-1") is False

    harness.cancel(organization_id)
    with pytest.raises(ContractRejectedError, match="cancelled Run"):
        harness.acquire_lease(organization_id, "worker-b")


def test_expired_approval_cannot_resume_run() -> None:
    harness, organization_id, clock = _harness()
    harness.request_approval(expires_in_seconds=30)
    clock.advance(30)

    with pytest.raises(ContractRejectedError, match="expired approval"):
        harness.approve_and_resume(organization_id)
    assert harness.approval is not None
    assert harness.approval.decision is ApprovalDecision.EXPIRED


def test_permission_shrink_blocks_recovery_and_step_write() -> None:
    harness, organization_id, _ = _harness()
    lease = harness.acquire_lease(organization_id, "worker-a")
    harness.request_approval(expires_in_seconds=30)
    harness.set_permission_available(False)

    with pytest.raises(ContractRejectedError, match="permission shrink"):
        harness.approve_and_resume(organization_id)
    with pytest.raises(ContractRejectedError, match="no longer accept"):
        harness.write_step_result(organization_id, "worker-a", lease.fencing_token, "step-1")


def test_retention_is_30_days_and_agent_cannot_mutate_hold() -> None:
    harness, _, clock = _harness()
    assert harness.retention_expires_at == clock.now() + timedelta(days=RUN_RETENTION_DAYS)

    @dataclass(frozen=True, slots=True)
    class AgentVisibleRetention:
        retention_expires_at: datetime
        retention_hold: bool

    visible = AgentVisibleRetention(harness.retention_expires_at, harness.retention_hold)
    with pytest.raises(FrozenInstanceError):
        setattr(visible, "retention_hold", True)
