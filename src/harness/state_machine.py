"""V7 Harness Run、Step 与任务级审批的纯状态机。"""

from collections.abc import Mapping

from harness.types import (
    ApprovalDecision,
    InvalidTransitionError,
    RunState,
    StepState,
)

RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.RECEIVED: frozenset({RunState.PLANNING, RunState.CANCELLED, RunState.EXPIRED}),
    RunState.PLANNING: frozenset(
        {
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
            RunState.FAILED_RETRYABLE,
            RunState.FAILED_FINAL,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING_APPROVAL,
            RunState.WAITING_DEPENDENCY,
            RunState.FAILED_RETRYABLE,
            RunState.FAILED_FINAL,
            RunState.SUCCEEDED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.EXPIRED, RunState.FAILED_FINAL}
    ),
    RunState.WAITING_DEPENDENCY: frozenset(
        {
            RunState.RUNNING,
            RunState.FAILED_RETRYABLE,
            RunState.FAILED_FINAL,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.FAILED_RETRYABLE: frozenset(
        {RunState.RUNNING, RunState.FAILED_FINAL, RunState.CANCELLED, RunState.EXPIRED}
    ),
    RunState.SUCCEEDED: frozenset(),
    RunState.FAILED_FINAL: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
}

STEP_TRANSITIONS: Mapping[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.RUNNING, StepState.CANCELLED}),
    StepState.RUNNING: frozenset(
        {
            StepState.SUCCEEDED,
            StepState.FAILED_RETRYABLE,
            StepState.FAILED_FINAL,
            StepState.WAITING_APPROVAL,
            StepState.CANCELLED,
        }
    ),
    StepState.WAITING_APPROVAL: frozenset({StepState.RUNNING, StepState.FAILED_FINAL, StepState.CANCELLED}),
    StepState.FAILED_RETRYABLE: frozenset({StepState.RUNNING, StepState.FAILED_FINAL, StepState.CANCELLED}),
    StepState.SUCCEEDED: frozenset(),
    StepState.FAILED_FINAL: frozenset(),
    StepState.CANCELLED: frozenset(),
}

APPROVAL_TRANSITIONS: Mapping[ApprovalDecision, frozenset[ApprovalDecision]] = {
    ApprovalDecision.PENDING: frozenset(
        {
            ApprovalDecision.APPROVED,
            ApprovalDecision.REJECTED,
            ApprovalDecision.EXPIRED,
            ApprovalDecision.REVOKED,
        }
    ),
    ApprovalDecision.APPROVED: frozenset({ApprovalDecision.REVOKED}),
    ApprovalDecision.REJECTED: frozenset(),
    ApprovalDecision.EXPIRED: frozenset(),
    ApprovalDecision.REVOKED: frozenset(),
}


def require_run_transition(current: RunState, target: RunState) -> None:
    """验证 AgentRun 状态转换符合已冻结的 V7-00 合约。

    Args:
        current: 当前 RunState 枚举。
        target: 要写入的目标 RunState 枚举。

    Returns:
        None。没有异常表示转换允许。

    Raises:
        TypeError: 任一参数不是 RunState 时抛出。
        InvalidTransitionError: 当前状态不允许转换到目标状态时抛出。
    """
    _require_transition("Run", RUN_TRANSITIONS, current, target, RunState)


def require_step_transition(current: StepState, target: StepState) -> None:
    """验证 RunStep 状态转换符合 Harness 失败和审批收敛规则。

    Args:
        current: 当前 StepState 枚举。
        target: 要写入的目标 StepState 枚举。

    Returns:
        None。没有异常表示转换允许。

    Raises:
        TypeError: 任一参数不是 StepState 时抛出。
        InvalidTransitionError: 当前状态不允许转换到目标状态时抛出。
    """
    _require_transition("Step", STEP_TRANSITIONS, current, target, StepState)


def require_approval_transition(current: ApprovalDecision, target: ApprovalDecision) -> None:
    """验证任务级审批决定不会从终态重新变为有效。

    Args:
        current: 当前 ApprovalDecision 枚举。
        target: 要写入的目标 ApprovalDecision 枚举。

    Returns:
        None。没有异常表示转换允许。

    Raises:
        TypeError: 任一参数不是 ApprovalDecision 时抛出。
        InvalidTransitionError: 当前决定不允许转换到目标决定时抛出。
    """
    _require_transition("Approval", APPROVAL_TRANSITIONS, current, target, ApprovalDecision)


def _require_transition[T](
    subject: str,
    transitions: Mapping[T, frozenset[T]],
    current: T,
    target: T,
    expected_type: type[T],
) -> None:
    """校验一个受控枚举状态机的边。

    Args:
        subject: 用于安全错误摘要的状态机名称。
        transitions: 由当前状态到允许目标状态的不可变映射。
        current: 当前枚举状态。
        target: 目标枚举状态。
        expected_type: 此状态机唯一接受的枚举类型。

    Returns:
        None。没有异常表示允许该边。

    Raises:
        TypeError: current 或 target 不是 expected_type 时抛出。
        InvalidTransitionError: 当前状态没有到目标状态的声明边时抛出。
    """
    if not isinstance(current, expected_type) or not isinstance(target, expected_type):
        raise TypeError(f"{subject} transitions require {expected_type.__name__} values")
    if target not in transitions[current]:
        raise InvalidTransitionError(f"{subject} transition is not allowed")
