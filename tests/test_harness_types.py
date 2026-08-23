"""V7-01B Harness 纯领域类型与固定 Profile 的单元测试。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from harness.profiles import AgentProfileRegistry
from harness.types import (
    AgentProfile,
    AgentRun,
    AgentTask,
    ApprovalDecision,
    ApprovalId,
    ApprovalPolicy,
    ExecutionVersions,
    HarnessRole,
    IdempotencyKey,
    KnowledgeScope,
    NonNegativeInt,
    OpaqueReference,
    OrganizationId,
    PositiveInt,
    ProfileHash,
    ProfileId,
    ProfileMismatchError,
    ProfileVersion,
    RunApprovalRequest,
    RunBudget,
    RunId,
    RunNumber,
    RunState,
    SafeText,
    StepId,
    SupportKnowledgeAssistInput,
    TaskConstraints,
    TaskId,
    TaskKind,
    ToolId,
    Version,
)

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


def _profile() -> AgentProfile:
    return AgentProfile.create(
        profile_id=ProfileId("support-knowledge-readonly"),
        version=ProfileVersion(1),
        task_kinds=frozenset({TaskKind.SUPPORT_KNOWLEDGE_ASSIST}),
        allowed_tools=frozenset(
            {
                ToolId.KNOWLEDGE_SEARCH_V1,
                ToolId.POLICY_EXCERPT_V1,
                ToolId.TICKET_AUTHORIZED_SUMMARY_V1,
            }
        ),
    )


def _task() -> AgentTask:
    return AgentTask(
        task_id=TaskId.new(),
        organization_id=OrganizationId(uuid4()),
        requester_subject_id=101,
        requester_role=HarnessRole.AGENT,
        requester_authz_version=OpaqueReference("authz-v1"),
        profile=_profile(),
        task_kind=TaskKind.SUPPORT_KNOWLEDGE_ASSIST,
        input=SupportKnowledgeAssistInput(
            question_summary=SafeText("如何处理公开的售后政策咨询"),
            knowledge_scope=KnowledgeScope(frozenset({OpaqueReference("knowledge:public-policy-v1")})),
        ),
        scope_hash=ProfileHash.from_material("scope-v1"),
        constraints=TaskConstraints(
            max_duration_seconds=PositiveInt(300),
            max_model_calls=PositiveInt(6),
            max_tool_calls=NonNegativeInt(12),
            read_only=True,
            allows_run_approval=True,
            expected_artifact_kinds=frozenset({"REPLY_DRAFT"}),
        ),
        idempotency_key=IdempotencyKey("task-idempotency-1"),
        input_hash=ProfileHash.from_material("input-v1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        retention_expires_at=NOW + timedelta(days=30),
    )


def _run(task: AgentTask) -> AgentRun:
    return AgentRun(
        run_id=RunId.new(),
        organization_id=task.organization_id,
        task_id=task.task_id,
        run_no=RunNumber(1),
        state=RunState.RECEIVED,
        version=Version(0),
        current_step_no=NonNegativeInt(0),
        checkpoint_ref=None,
        lease=None,
        authz_checked_at=None,
        budget=RunBudget(
            deadline_at=NOW + timedelta(minutes=5),
            max_model_calls=PositiveInt(6),
            max_tool_calls=NonNegativeInt(12),
        ),
        execution_versions=ExecutionVersions(
            model_version=OpaqueReference("model:deepseek-chat-v1"),
            prompt_version=OpaqueReference("prompt:support-v1"),
            profile_id=task.profile.profile_id,
            profile_version=task.profile.version,
            profile_hash=task.profile.profile_hash,
            toolset_version=OpaqueReference("toolset:v7-01"),
            knowledge_version=OpaqueReference("knowledge:public-policy-v1"),
        ),
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=task.retention_expires_at,
        retention_hold=task.retention_hold,
    )


def _approval(decision: ApprovalDecision, decided_at: datetime | None, decider: int | None) -> RunApprovalRequest:
    task = _task()
    run = _run(task)
    return RunApprovalRequest(
        approval_id=ApprovalId.new(),
        organization_id=task.organization_id,
        run_id=run.run_id,
        step_id=StepId.new(),
        step_version=Version(0),
        decision=decision,
        decision_version=Version(0),
        requested_scope_ref=OpaqueReference("scope:task-only"),
        approver_policy=ApprovalPolicy(frozenset({HarnessRole.AGENT, HarnessRole.ADMIN})),
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        decided_at=decided_at,
        decided_by_subject_id=decider,
        decision_reason_code=None,
        created_at=NOW,
        updated_at=NOW,
        retention_expires_at=task.retention_expires_at,
        retention_hold=False,
    )


def test_value_objects_are_validated_and_immutable() -> None:
    key = IdempotencyKey("task-idempotency-1")
    assert str(key) == "task-idempotency-1"
    with pytest.raises(AttributeError):
        key.value = "changed"
    with pytest.raises(AttributeError):
        key.extra = "forbidden"
    with pytest.raises(ValueError):
        ProfileHash("not-a-sha256-hash")


def test_profile_hash_is_deterministic_and_profile_is_frozen() -> None:
    first = _profile()
    second = _profile()
    assert first.profile_hash == second.profile_hash
    with pytest.raises(FrozenInstanceError):
        first.read_only = False


def test_registry_only_exposes_the_fixed_server_profile() -> None:
    registry = AgentProfileRegistry()
    profile = registry.profile_for_task(TaskKind.SUPPORT_KNOWLEDGE_ASSIST)
    assert profile.profile_id == ProfileId("support-knowledge-readonly")
    assert profile.version == ProfileVersion(1)
    assert profile.read_only is True
    assert profile.allowed_tools == frozenset(
        {
            ToolId.KNOWLEDGE_SEARCH_V1,
            ToolId.POLICY_EXCERPT_V1,
            ToolId.TICKET_AUTHORIZED_SUMMARY_V1,
        }
    )

    assert (
        registry.verify_persisted(
            profile.profile_id,
            profile.version,
            profile.profile_hash,
            TaskKind.SUPPORT_KNOWLEDGE_ASSIST,
        )
        == profile
    )
    with pytest.raises(ProfileMismatchError):
        registry.verify_persisted(
            profile.profile_id,
            profile.version,
            ProfileHash.from_material("tampered"),
            TaskKind.SUPPORT_KNOWLEDGE_ASSIST,
        )


def test_task_is_typed_immutable_and_read_only() -> None:
    task = _task()
    assert task.constraints.read_only is True
    assert task.profile.profile_hash.value
    with pytest.raises(FrozenInstanceError):
        task.retention_hold = True
    with pytest.raises(ValueError):
        AgentTask(
            task_id=TaskId.new(),
            organization_id=OrganizationId(uuid4()),
            requester_subject_id=101,
            requester_role=HarnessRole.ADMIN,
            requester_authz_version=OpaqueReference("authz-v1"),
            profile=task.profile,
            task_kind=TaskKind.SUPPORT_KNOWLEDGE_ASSIST,
            input=task.input,
            scope_hash=task.scope_hash,
            constraints=task.constraints,
            idempotency_key=task.idempotency_key,
            input_hash=task.input_hash,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            retention_expires_at=NOW + timedelta(days=30),
        )


def test_run_budget_and_run_require_valid_time_and_budget() -> None:
    task = _task()
    run = _run(task)
    assert run.budget.max_tool_calls.value == 12
    with pytest.raises(ValueError):
        RunBudget(
            deadline_at=NOW,
            max_model_calls=PositiveInt(1),
            max_tool_calls=NonNegativeInt(0),
            model_calls_used=NonNegativeInt(2),
        )


@pytest.mark.parametrize(
    ("decision", "decided_at", "decider"),
    [
        (ApprovalDecision.PENDING, None, None),
        (ApprovalDecision.APPROVED, NOW, 201),
        (ApprovalDecision.REJECTED, NOW, 201),
        (ApprovalDecision.REVOKED, NOW, None),
        (ApprovalDecision.EXPIRED, NOW, None),
    ],
)
def test_approval_decision_fields_follow_the_database_check(
    decision: ApprovalDecision,
    decided_at: datetime | None,
    decider: int | None,
) -> None:
    approval = _approval(decision, decided_at, decider)
    assert approval.decision is decision


@pytest.mark.parametrize(
    ("decision", "decided_at", "decider"),
    [
        (ApprovalDecision.PENDING, NOW, 201),
        (ApprovalDecision.APPROVED, NOW, None),
        (ApprovalDecision.REJECTED, None, 201),
        (ApprovalDecision.EXPIRED, NOW, 201),
        (ApprovalDecision.REVOKED, None, None),
    ],
)
def test_approval_rejects_inconsistent_decision_fields(
    decision: ApprovalDecision,
    decided_at: datetime | None,
    decider: int | None,
) -> None:
    with pytest.raises(ValueError, match="approval decision fields"):
        _approval(decision, decided_at, decider)
