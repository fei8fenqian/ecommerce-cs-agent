"""V7-01B Harness Run、Step、Approval 纯状态机测试。"""

import pytest

from harness.state_machine import (
    APPROVAL_TRANSITIONS,
    RUN_TRANSITIONS,
    STEP_TRANSITIONS,
    require_approval_transition,
    require_run_transition,
    require_step_transition,
)
from harness.types import ApprovalDecision, InvalidTransitionError, RunState, StepState


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in RUN_TRANSITIONS.items() for target in targets],
)
def test_declared_run_transitions_are_allowed(current: RunState, target: RunState) -> None:
    require_run_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current in RunState for target in RunState if target not in RUN_TRANSITIONS[current]],
)
def test_undeclared_run_transitions_are_rejected(current: RunState, target: RunState) -> None:
    with pytest.raises(InvalidTransitionError):
        require_run_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in STEP_TRANSITIONS.items() for target in targets],
)
def test_declared_step_transitions_are_allowed(current: StepState, target: StepState) -> None:
    require_step_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current in StepState for target in StepState if target not in STEP_TRANSITIONS[current]],
)
def test_undeclared_step_transitions_are_rejected(current: StepState, target: StepState) -> None:
    with pytest.raises(InvalidTransitionError):
        require_step_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [(current, target) for current, targets in APPROVAL_TRANSITIONS.items() for target in targets],
)
def test_declared_approval_transitions_are_allowed(
    current: ApprovalDecision,
    target: ApprovalDecision,
) -> None:
    require_approval_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in ApprovalDecision
        for target in ApprovalDecision
        if target not in APPROVAL_TRANSITIONS[current]
    ],
)
def test_undeclared_approval_transitions_are_rejected(
    current: ApprovalDecision,
    target: ApprovalDecision,
) -> None:
    with pytest.raises(InvalidTransitionError):
        require_approval_transition(current, target)


def test_state_machines_reject_raw_strings() -> None:
    with pytest.raises(TypeError):
        require_run_transition("RECEIVED", RunState.PLANNING)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        require_step_transition("PENDING", StepState.RUNNING)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        require_approval_transition("PENDING", ApprovalDecision.APPROVED)  # type: ignore[arg-type]
