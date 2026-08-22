"""S3-02 Step 1：不连接数据库的纯状态机和内部契约测试。"""

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from service.after_sale_state_machine import (
    AFTER_SALE_TRANSITIONS,
    REFUND_TRANSITIONS,
    require_actor,
    require_after_sale_command,
    require_after_sale_transition,
    require_command_meta,
    require_refund_transition,
)
from service.after_sale_types import (
    Actor,
    ActorNotAllowedError,
    ActorType,
    AfterSaleCommand,
    AfterSaleStatus,
    CommandMeta,
    CommandResult,
    DecisionReasonCode,
    DomainErrorCode,
    EligibilityDecision,
    FinanceApproveCommand,
    IdempotencyKey,
    InvalidStateError,
    NonNegativeInt,
    PolicyVersion,
    QualificationPath,
    QualificationResult,
    RefundStatus,
    RequestContext,
    ResourceType,
    SafeText,
    ServiceName,
    Source,
    UtcClock,
    validate_actor_source,
)


def _human_meta(*, actor_type: ActorType = ActorType.CUSTOMER, source: Source = Source.API) -> CommandMeta:
    return CommandMeta(
        actor=Actor(actor_type, actor_user_id=101),
        request=RequestContext("req-1", "trace-1", "span-1", source),
        idempotency_key=IdempotencyKey("idem-1"),
        expected_version=NonNegativeInt(0),
    )


def _machine_meta(*, actor_type: ActorType = ActorType.WORKER, source: Source = Source.WORKER) -> CommandMeta:
    return CommandMeta(
        actor=Actor(actor_type, service_name=ServiceName("refund-worker")),
        request=RequestContext("req-1", "trace-1", "span-1", source),
        idempotency_key=IdempotencyKey("idem-1"),
        expected_version=NonNegativeInt(0),
    )


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in AFTER_SALE_TRANSITIONS.items() for target in targets],
)
def test_every_declared_after_sale_transition_is_allowed(current, target):
    require_after_sale_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in AfterSaleStatus
        for target in AfterSaleStatus
        if target not in AFTER_SALE_TRANSITIONS[current]
    ],
)
def test_undeclared_after_sale_transition_is_rejected(current, target):
    with pytest.raises(InvalidStateError):
        require_after_sale_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in REFUND_TRANSITIONS.items() for target in targets],
)
def test_every_declared_refund_transition_is_allowed(current, target):
    require_refund_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in RefundStatus
        for target in RefundStatus
        if target not in REFUND_TRANSITIONS[current]
    ],
)
def test_undeclared_refund_transition_is_rejected(current, target):
    with pytest.raises(InvalidStateError):
        require_refund_transition(current, target)


def test_state_machine_rejects_raw_strings_inside_domain_layer():
    with pytest.raises(TypeError):
        require_after_sale_transition("SUBMITTED", AfterSaleStatus.UNDER_REVIEW)


@pytest.mark.parametrize(
    ("command", "actor"),
    [
        (AfterSaleCommand.SUBMIT, ActorType.CUSTOMER),
        (AfterSaleCommand.CANCEL, ActorType.CUSTOMER),
        (AfterSaleCommand.REQUEST_EVIDENCE, ActorType.AGENT),
        (AfterSaleCommand.COMPLETE_EVIDENCE, ActorType.CUSTOMER),
        (AfterSaleCommand.FINANCE_APPROVE, ActorType.FINANCE),
        (AfterSaleCommand.EXPIRE_CUSTOMER_CONFIRMATION, ActorType.WORKER),
        (AfterSaleCommand.RECORD_REFUND_CALLBACK, ActorType.PAYMENT_GATEWAY),
    ],
)
def test_command_actor_allowlist(command, actor):
    require_actor(command, actor)


@pytest.mark.parametrize(
    ("command", "actor"),
    [
        (AfterSaleCommand.CANCEL, ActorType.AGENT),
        (AfterSaleCommand.FINANCE_APPROVE, ActorType.CUSTOMER),
        (AfterSaleCommand.RECORD_REFUND_CALLBACK, ActorType.CUSTOMER),
        (AfterSaleCommand.SUBMIT_REVIEW, ActorType.FINANCE),
    ],
)
def test_command_actor_allowlist_rejects_wrong_role(command, actor):
    with pytest.raises(ActorNotAllowedError):
        require_actor(command, actor)


def test_command_guard_checks_actor_and_state():
    require_after_sale_command(
        AfterSaleCommand.FINANCE_APPROVE,
        ActorType.FINANCE,
        AfterSaleStatus.PENDING_FINANCE_APPROVAL,
        AfterSaleStatus.REFUND_PROCESSING,
    )

    with pytest.raises(InvalidStateError):
        require_after_sale_command(
            AfterSaleCommand.FINANCE_APPROVE,
            ActorType.FINANCE,
            AfterSaleStatus.UNDER_REVIEW,
            AfterSaleStatus.REFUND_PROCESSING,
        )


def test_actor_context_and_command_meta_are_separate():
    meta = _human_meta()
    assert meta.actor.actor_user_id == 101
    assert meta.request.request_id == "req-1"
    assert meta.request.source is Source.API
    assert meta.idempotency_key.value == "idem-1"

    with pytest.raises(ValueError):
        Actor(ActorType.CUSTOMER)
    with pytest.raises(ValueError):
        Actor(ActorType.WORKER)
    with pytest.raises(ValueError):
        Actor(ActorType.WORKER, actor_user_id=999, service_name=ServiceName("refund-worker"))
    with pytest.raises(ValueError):
        Actor(ActorType.CUSTOMER, actor_user_id=101, service_name=ServiceName("api"))


@pytest.mark.parametrize(
    ("actor_type", "source"),
    [
        (ActorType.PAYMENT_GATEWAY, Source.API),
        (ActorType.SYSTEM, Source.API),
        (ActorType.WORKER, Source.CALLBACK),
    ],
)
def test_invalid_actor_source_combinations_are_rejected(actor_type, source):
    with pytest.raises(ValueError):
        validate_actor_source(actor_type, source)


def test_agent_source_cannot_execute_finance_command():
    meta = _human_meta(actor_type=ActorType.FINANCE, source=Source.AGENT)
    with pytest.raises(ActorNotAllowedError):
        require_command_meta(AfterSaleCommand.FINANCE_APPROVE, meta)


def test_callback_source_must_use_callback_context():
    with pytest.raises(ValueError):
        CommandMeta(
            actor=Actor(ActorType.PAYMENT_GATEWAY, service_name=ServiceName("payment-adapter")),
            request=RequestContext("req-1", None, None, Source.CALLBACK),
            idempotency_key=IdempotencyKey("idem-1"),
            expected_version=NonNegativeInt(0),
        )


def test_command_meta_rejects_negative_version():
    with pytest.raises(ValueError):
        CommandMeta(
            actor=Actor(ActorType.CUSTOMER, actor_user_id=101),
            request=RequestContext("req-1", None, None, Source.API),
            idempotency_key=IdempotencyKey("idem-1"),
            expected_version=NonNegativeInt(-1),
        )


def test_command_dto_is_typed_and_immutable():
    command = FinanceApproveCommand(
        meta=_human_meta(actor_type=ActorType.FINANCE),
        after_sale_request_id=uuid4(),
        decision_reason_code=DecisionReasonCode("MANUAL_APPROVAL"),
        decision_note=SafeText("approved after review"),
    )
    assert command.meta.actor.actor_type is ActorType.FINANCE
    with pytest.raises(FrozenInstanceError):
        command.decision_note = None
    key = IdempotencyKey("immutable")
    with pytest.raises(AttributeError):
        key.value = "changed"
    with pytest.raises(AttributeError):
        key.extra = "not allowed"


def test_command_result_contains_version_and_replay_marker():
    result = CommandResult(
        resource_type=ResourceType.AFTER_SALE_REQUEST,
        resource_id=uuid4(),
        status=AfterSaleStatus.CANCELLED,
        version=NonNegativeInt(2),
        idempotent_replay=True,
    )
    assert result.idempotent_replay is True
    assert result.version.value == 2

    with pytest.raises(ValueError):
        CommandResult(
            resource_type=ResourceType.AFTER_SALE_REQUEST,
            resource_id=uuid4(),
            status=RefundStatus.PROCESSING,
            version=NonNegativeInt(1),
            idempotent_replay=False,
        )


def test_domain_error_has_controlled_code_and_safe_fields():
    error = InvalidStateError("state transition is not allowed")
    assert error.code is DomainErrorCode.INVALID_STATE
    assert error.resource_id is None
    assert error.retryable is False


def test_eligibility_decision_requires_path_for_determinate_result():
    result = EligibilityDecision(
        QualificationResult.INDETERMINATE,
        None,
        PolicyVersion("policy-v1"),
        DecisionReasonCode("FACTS_UNAVAILABLE"),
    )
    assert result.path is None

    with pytest.raises(ValueError):
        EligibilityDecision(
            QualificationResult.INDETERMINATE,
            QualificationPath.FINANCE,
            PolicyVersion("policy-v1"),
            DecisionReasonCode("FACTS_UNAVAILABLE"),
        )
    with pytest.raises(ValueError):
        EligibilityDecision(
            QualificationResult.ELIGIBLE,
            QualificationPath.FINANCE,
            PolicyVersion("policy-v1"),
            DecisionReasonCode("ELIGIBLE"),
        )
    with pytest.raises(ValueError):
        EligibilityDecision(
            QualificationResult.INELIGIBLE,
            QualificationPath.AUTO,
            PolicyVersion("policy-v1"),
            DecisionReasonCode("MANUAL_REVIEW"),
        )


def test_clock_returns_timezone_aware_utc_time():
    now = UtcClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0
