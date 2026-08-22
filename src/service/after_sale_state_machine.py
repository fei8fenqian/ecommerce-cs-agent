"""退款领域状态机。

状态机是纯函数：只验证状态和主体，不访问数据库，也不执行副作用。
"""

from service.after_sale_types import (
    COMMAND_ACTORS,
    ActorNotAllowedError,
    ActorType,
    AfterSaleCommand,
    AfterSaleStatus,
    CommandMeta,
    InvalidStateError,
    RefundStatus,
    Source,
)

# key 是当前状态，value 是允许到达的下一状态集合。
# frozenset 表示这张规则表运行时不可修改。
AFTER_SALE_TRANSITIONS: dict[AfterSaleStatus, frozenset[AfterSaleStatus]] = {
    AfterSaleStatus.SUBMITTED: frozenset(
        {
            AfterSaleStatus.EVIDENCE_PENDING,
            AfterSaleStatus.UNDER_REVIEW,
            AfterSaleStatus.CANCELLED,
        }
    ),
    AfterSaleStatus.EVIDENCE_PENDING: frozenset(
        {
            AfterSaleStatus.EVIDENCE_PENDING,
            AfterSaleStatus.UNDER_REVIEW,
            AfterSaleStatus.CANCELLED,
        }
    ),
    AfterSaleStatus.UNDER_REVIEW: frozenset(
        {
            AfterSaleStatus.PENDING_CUSTOMER_CONFIRMATION,
            AfterSaleStatus.PENDING_FINANCE_APPROVAL,
            AfterSaleStatus.CANCELLED,
        }
    ),
    AfterSaleStatus.PENDING_CUSTOMER_CONFIRMATION: frozenset(
        {
            AfterSaleStatus.REFUND_PROCESSING,
            AfterSaleStatus.CANCELLED,
            AfterSaleStatus.EXPIRED,
        }
    ),
    AfterSaleStatus.PENDING_FINANCE_APPROVAL: frozenset(
        {
            AfterSaleStatus.REFUND_PROCESSING,
            AfterSaleStatus.REJECTED,
        }
    ),
    AfterSaleStatus.REFUND_PROCESSING: frozenset({AfterSaleStatus.REFUNDED, AfterSaleStatus.REFUND_EXCEPTION}),
    AfterSaleStatus.REFUNDED: frozenset(),
    AfterSaleStatus.REJECTED: frozenset(),
    AfterSaleStatus.CANCELLED: frozenset(),
    AfterSaleStatus.EXPIRED: frozenset(),
    AfterSaleStatus.REFUND_EXCEPTION: frozenset(),
}


# 退款单是另一套状态机，不能把退款单状态和售后申请状态混在一起。
REFUND_TRANSITIONS: dict[RefundStatus, frozenset[RefundStatus]] = {
    RefundStatus.CREATED: frozenset({RefundStatus.PROCESSING}),
    RefundStatus.PROCESSING: frozenset(
        {
            RefundStatus.SUCCEEDED,
            RefundStatus.FAILED,
            RefundStatus.RECONCILIATION_EXCEPTION,
        }
    ),
    RefundStatus.SUCCEEDED: frozenset(),
    RefundStatus.FAILED: frozenset(),
    RefundStatus.RECONCILIATION_EXCEPTION: frozenset(),
}


def require_after_sale_transition(
    current: AfterSaleStatus,
    target: AfterSaleStatus,
) -> None:
    """要求售后申请存在一条冻结的状态边。

    这里故意不接受 str；数据库/API 的字符串必须先在边界层解析成枚举。

    Args:
        current: 当前售后申请状态。
        target: 要变更到的目标状态。

    Returns:
        None。校验成功只表示这条状态边被状态机允许。

    Raises:
        TypeError: current 或 target 不是 AfterSaleStatus。
        InvalidStateError: 当前状态不允许变更到目标状态。
    """
    if not isinstance(current, AfterSaleStatus) or not isinstance(target, AfterSaleStatus):
        raise TypeError("after-sale transition requires AfterSaleStatus values")
    if target not in AFTER_SALE_TRANSITIONS[current]:
        raise InvalidStateError(f"invalid after-sale transition: {current.value} -> {target.value}")


def require_refund_transition(
    current: RefundStatus,
    target: RefundStatus,
) -> None:
    """要求退款单存在一条冻结的状态边。

    Args:
        current: 当前退款单状态。
        target: 要变更到的目标状态。

    Returns:
        None。校验成功只表示这条状态边被状态机允许。

    Raises:
        TypeError: current 或 target 不是 RefundStatus。
        InvalidStateError: 当前状态不允许变更到目标状态。
    """
    if not isinstance(current, RefundStatus) or not isinstance(target, RefundStatus):
        raise TypeError("refund transition requires RefundStatus values")
    if target not in REFUND_TRANSITIONS[current]:
        raise InvalidStateError(f"invalid refund transition: {current.value} -> {target.value}")


def require_actor(command: AfterSaleCommand, actor_type: ActorType, source: Source | None = None) -> None:
    """要求主体有权执行命令；不涉及资源归属和数据库状态。

    Args:
        command: 要执行的领域命令。
        actor_type: 发起命令的主体类型。
        source: 可选的调用来源；用于额外校验来源和命令的组合。

    Returns:
        None。校验成功表示角色和来源满足基础契约。

    Raises:
        ActorNotAllowedError: 主体或来源不能执行该命令。
    """
    allowed = COMMAND_ACTORS[command]
    finance_commands = {
        AfterSaleCommand.FINANCE_APPROVE,
        AfterSaleCommand.FINANCE_REJECT,
    }
    if actor_type not in allowed or (source is Source.AGENT and command in finance_commands):
        raise ActorNotAllowedError(command, actor_type)


def require_command_meta(command: AfterSaleCommand, meta: CommandMeta) -> None:
    """校验命令元数据中的主体、来源和命令角色边界。

    Args:
        command: 要执行的领域命令。
        meta: 命令携带的主体、请求上下文、幂等键和版本号。

    Returns:
        None。校验成功表示 meta 可以进入后续业务流程。

    Raises:
        ActorNotAllowedError: 主体或来源不能执行该命令。
    """
    require_actor(command, meta.actor.actor_type, meta.request.source)


def require_after_sale_command(
    command: AfterSaleCommand,
    actor_type: ActorType,
    current: AfterSaleStatus,
    target: AfterSaleStatus,
) -> None:
    """同时校验命令主体和状态边。

    这是领域层的基础守卫，不代表完整应用服务授权；资源归属、版本和事实校验仍由
    Application Service 在事务中完成。

    Args:
        command: 要执行的领域命令。
        actor_type: 发起命令的主体类型。
        current: 当前售后申请状态。
        target: 命令希望进入的目标状态。

    Returns:
        None。角色和状态边都通过时返回。

    Raises:
        ActorNotAllowedError: 主体不能执行该命令。
        TypeError: 状态参数不是 AfterSaleStatus。
        InvalidStateError: 状态边不被允许。
    """
    require_actor(command, actor_type)
    require_after_sale_transition(current, target)


__all__ = [
    "AFTER_SALE_TRANSITIONS",
    "REFUND_TRANSITIONS",
    "require_after_sale_command",
    "require_after_sale_transition",
    "require_actor",
    "require_command_meta",
    "require_refund_transition",
]
