"""客服 Agent 的确定性 Control Plane。

LLM 只提供已校验的 domain/operation、实体候选和客户声明；本模块决定业务目标的
最小决策事实、产生这些事实的能力、读取顺序和完成条件。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent.goal_taxonomy import workflow_key_for_goal
from service.checkout_refund_service import customer_visible_refund_status


@dataclass(frozen=True)
class FactRequirement:
    name: str
    capability: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityDefinition:
    """一个业务能力的最小注册信息。

    ``available`` 描述当前代码/环境是否真的提供了这个能力。能力可以先被
    Workflow 声明为需要，但没有实现时，计划必须显式暴露覆盖缺口，不能让
    Planner 假装已经查过。
    """

    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    read_only: bool = True
    available: bool = True


@dataclass(frozen=True)
class WorkflowDefinition:
    key: str
    facts: tuple[FactRequirement, ...]
    completion_facts: tuple[str, ...]
    completion_criteria: Callable[[dict[str, Any]], bool]
    readiness_criteria: Callable[[dict[str, Any]], bool] | None = None
    write_confirmation_required: bool = False
    # A capability gap does not always mean that every customer-visible answer
    # is unsafe.  This predicate is deliberately narrower than full completion:
    # it permits a verified partial answer while the missing capability remains
    # visible as a limitation.  It never authorizes a write or fabricates the
    # missing transaction fact.
    partial_completion_criteria: Callable[[dict[str, Any]], bool] | None = None


def _has_status(facts: dict[str, Any], key: str) -> bool:
    """判断事实是否已知；False 是有效的否定结论，不能当成 unknown。"""
    return key in facts and facts[key] not in (None, "")


def _shipment_complete(facts: dict[str, Any]) -> bool:
    status = facts.get("shipping_status")
    return (
        _has_status(facts, "order_identified")
        and status not in (None, "")
        and (status not in {"PENDING", "NOT_SHIPPED"} or _has_status(facts, "expected_ship_time"))
    )


def _shipment_partial_complete(facts: dict[str, Any]) -> bool:
    return _has_status(facts, "order_identified") and _has_status(facts, "shipping_status")


def _after_sales_complete(facts: dict[str, Any]) -> bool:
    return _has_status(facts, "service_order_identified") and _has_status(facts, "service_order_status")


def _pickup_complete(facts: dict[str, Any]) -> bool:
    return _after_sales_complete(facts) and _has_status(facts, "pickup_status")


def _after_sales_partial_complete(facts: dict[str, Any]) -> bool:
    return _after_sales_complete(facts)


def _transition_complete(facts: dict[str, Any]) -> bool:
    if facts.get("exchange_eligibility") is False:
        # 资格已核验且不允许时，目标变成向客户解释限制/给替代方案，
        # 不应等待一个永远不会执行的写操作。
        return _after_sales_complete(facts)
    return (
        _after_sales_complete(facts)
        and _has_status(facts, "exchange_eligibility")
        and facts.get("command_result") == "SUCCESS"
    )


def _refund_complete(facts: dict[str, Any]) -> bool:
    status = facts.get("refund_status")
    return _has_status(facts, "order_identified") and status in {
        "PENDING_CONFIRMATION",
        "PENDING_MERCHANT_REVIEW",
        "PROCESSING",
        "COMPLETED",
        "FAILED",
        "NOT_FOUND",
    }


def _refund_amount_complete(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and _has_status(facts, "refund_amount")


def _refund_expected_arrival_complete(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and _has_status(facts, "expected_arrival_time")


def _refund_processing_time_complete(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and _has_status(facts, "refund_processing_sla")


def _refund_anomaly_complete(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and _has_status(facts, "refund_failure_reason")


def _refund_destination_complete(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and _has_status(facts, "refund_destination")


def _refund_eligibility_complete(facts: dict[str, Any]) -> bool:
    if _pending_payment_order(facts):
        # 尚未付款时不存在已支付款项的退款资格；这是可解释的已知业务结论，
        # 不应再进入已付款订单的退款 SOP。
        return _has_status(facts, "order_identified")
    return _has_status(facts, "order_identified") and _has_status(facts, "refund_eligibility")


def _refund_request_ready(facts: dict[str, Any]) -> bool:
    return (
        _refund_complete(facts)
        and facts.get("refund_status") == "NOT_FOUND"
        and facts.get("refund_eligibility") is True
    )


def _refund_request_complete(facts: dict[str, Any]) -> bool:
    if _pending_payment_order(facts):
        return _has_status(facts, "order_identified")
    if _refund_complete(facts) and facts.get("refund_status") != "NOT_FOUND":
        # 当前订单已经有退款记录；不能再次创建，客服应转为说明现有退款状态。
        return True
    if facts.get("refund_eligibility") is False:
        # 明确不符合资格时，客服目标是解释确定性规则，而不是虚构一笔退款。
        return _refund_eligibility_complete(facts) and _has_status(facts, "refund_status")
    # 退款由客户在官方自助页提交。入口交付完成的是客服 Goal，不是退款已创建或
    # 资金已成功退回；后续状态必须再由 query_refund_status 读取真实业务事实。
    return _refund_request_ready(facts) and _has_status(facts, "refund_entry")


def _pending_payment_order(facts: dict[str, Any]) -> bool:
    return facts.get("order_status") == "PENDING_PAYMENT" and _has_status(facts, "order_identified")


def _payment_status_complete(facts: dict[str, Any]) -> bool:
    return _has_status(facts, "order_identified") and facts.get("payment_status") in {
        "PAID",
        "PENDING",
        "NOT_PAID",
        "PAYMENT_NOT_CREATED",
        "PAYMENT_STATUS_UNAVAILABLE",
    }


def _order_cancel_complete(facts: dict[str, Any]) -> bool:
    # 聊天只提供经过核验的自助入口，不代替客户取消订单。订单状态已经查到时，
    # 无论是否仍可取消，都能给出确定性说明。
    return _has_status(facts, "order_identified") and _has_status(facts, "order_status")


def _refund_cancel_ready(facts: dict[str, Any]) -> bool:
    return _refund_complete(facts) and facts.get("refund_cancel_eligibility") is True


def _refund_cancel_complete(facts: dict[str, Any]) -> bool:
    if facts.get("refund_cancel_eligibility") is False:
        return _refund_complete(facts) and _has_status(facts, "refund_cancel_eligibility")
    # 退款取消属于资金相关状态变更，不由 Agent 代办。当前没有可信自助能力时，
    # 保持 capability gap / 人工处理，而不是等待 Agent Command Executor。
    return False


def _return_refund_dependency_complete(facts: dict[str, Any]) -> bool:
    return (
        _has_status(facts, "order_identified")
        and _has_status(facts, "return_status")
        and _has_status(facts, "return_logistics_status")
        and _has_status(facts, "warehouse_receipt_status")
        and _has_status(facts, "refund_status")
    )


def _price_protection_status_complete(facts: dict[str, Any]) -> bool:
    return _has_status(facts, "order_identified") and _has_status(facts, "price_protection_status")


def _transition_ready(facts: dict[str, Any]) -> bool:
    return _after_sales_complete(facts) and _has_status(facts, "exchange_eligibility")


def _price_protection_ready(facts: dict[str, Any]) -> bool:
    return _price_protection_status_complete(facts)


def _refund_ready(facts: dict[str, Any]) -> bool:
    return _has_status(facts, "order_identified") and _has_status(facts, "refund_status")


def _always_if_fact(fact: str) -> Callable[[dict[str, Any]], bool]:
    return lambda facts: _has_status(facts, fact)


def _always_complete(_: dict[str, Any]) -> bool:
    """Deterministic informational workflow with no business fact lookup."""
    return True


# 这是当前客服 Control Plane 的小型 Capability Registry。它描述业务能力的
# 输入/输出/前置条件，而不是把具体实现细节塞进 LLM prompt。
CAPABILITY_REGISTRY: dict[str, CapabilityDefinition] = {
    "track_order": CapabilityDefinition(
        "track_order",
        ("order_id", "phone"),
        ("order_identified", "order_status", "shipping_status"),
        ("authenticated_customer_context",),
    ),
    "check_after_sales": CapabilityDefinition(
        "check_after_sales",
        ("ticket_id",),
        ("service_order_identified", "service_order_status"),
        ("authenticated_customer_context",),
    ),
    "check_stock": CapabilityDefinition(
        "check_stock",
        ("product_name", "table"),
        ("stock_status",),
        ("authenticated_customer_context",),
    ),
    "check_payment_status": CapabilityDefinition(
        "check_payment_status",
        ("order_id",),
        ("payment_status", "order_status"),
        ("authenticated_customer_context",),
    ),
    "query_refund_status": CapabilityDefinition(
        "query_refund_status",
        ("order_id",),
        ("refund_status", "refund_amount"),
        ("authenticated_customer_context",),
    ),
    # 以下能力已经进入 Workflow 的事实模型，但当前 schema/工具还没有可靠
    # 的数据源。保留注册项是为了让评测明确区分 Tool Coverage Error，不能
    # 用已有的相似工具冒充它们。
    "query_expected_ship_time": CapabilityDefinition(
        "query_expected_ship_time",
        ("order_id",),
        ("expected_ship_time",),
        ("authenticated_customer_context", "order_identified"),
        available=False,
    ),
    "query_pickup_status": CapabilityDefinition(
        "query_pickup_status",
        ("service_order_id",),
        ("pickup_status",),
        ("authenticated_customer_context", "service_order_identified"),
        available=False,
    ),
    "check_exchange_eligibility": CapabilityDefinition(
        "check_exchange_eligibility",
        ("order_id", "service_order_id"),
        ("exchange_eligibility",),
        ("authenticated_customer_context", "service_order_identified"),
        available=False,
    ),
    "query_refund_expected_arrival": CapabilityDefinition(
        "query_refund_expected_arrival",
        ("refund_id",),
        ("expected_arrival_time",),
        ("authenticated_customer_context", "refund_status"),
        available=False,
    ),
    "query_price_protection": CapabilityDefinition(
        "query_price_protection",
        ("order_id",),
        ("price_protection_eligibility", "price_protection_status"),
        ("authenticated_customer_context", "order_identified"),
        available=False,
    ),
    "check_refund_eligibility": CapabilityDefinition(
        "check_refund_eligibility",
        ("order_id",),
        ("refund_eligibility",),
        ("authenticated_customer_context", "order_identified"),
    ),
    "generate_refund_entry": CapabilityDefinition(
        "generate_refund_entry",
        ("order_id",),
        ("refund_entry",),
        ("authenticated_customer_context", "order_identified", "refund_eligibility"),
    ),
    "query_refund_destination": CapabilityDefinition(
        "query_refund_destination",
        ("refund_id",),
        ("refund_destination",),
        ("authenticated_customer_context", "refund_status"),
        available=False,
    ),
    "query_refund_processing_sla": CapabilityDefinition(
        "query_refund_processing_sla",
        ("refund_id",),
        ("refund_processing_sla",),
        ("authenticated_customer_context", "refund_status"),
        available=False,
    ),
    "query_refund_failure_reason": CapabilityDefinition(
        "query_refund_failure_reason",
        ("refund_id",),
        ("refund_failure_reason",),
        ("authenticated_customer_context", "refund_status"),
        available=False,
    ),
    "check_refund_cancel_eligibility": CapabilityDefinition(
        "check_refund_cancel_eligibility",
        ("refund_id",),
        ("refund_cancel_eligibility",),
        ("authenticated_customer_context", "refund_status"),
        available=False,
    ),
    "query_return_status": CapabilityDefinition(
        "query_return_status",
        ("order_id",),
        ("return_status",),
        ("authenticated_customer_context", "order_identified"),
        available=False,
    ),
    "query_return_logistics": CapabilityDefinition(
        "query_return_logistics",
        ("order_id",),
        ("return_logistics_status",),
        ("authenticated_customer_context", "return_status"),
        available=False,
    ),
    "query_warehouse_receipt": CapabilityDefinition(
        "query_warehouse_receipt",
        ("order_id",),
        ("warehouse_receipt_status",),
        ("authenticated_customer_context", "return_status"),
        available=False,
    ),
}


def get_capability(name: str) -> CapabilityDefinition | None:
    return CAPABILITY_REGISTRY.get(name)


def is_capability_available(name: str) -> bool:
    capability = get_capability(name)
    return capability is not None and capability.available


_WORKFLOWS = {
    "payment.check_payment_status": WorkflowDefinition(
        "payment.check_payment_status",
        (FactRequirement("payment_status", "check_payment_status"),),
        ("payment_status",),
        _payment_status_complete,
    ),
    "order.cancel": WorkflowDefinition(
        "order.cancel",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("order_status", "track_order", ("order_identified",)),
        ),
        ("order_identified", "order_status"),
        _order_cancel_complete,
    ),
    "delivery.track_order": WorkflowDefinition(
        "delivery.track_order",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("order_status", "track_order", ("order_identified",)),
            FactRequirement("shipping_status", "track_order", ("order_identified",)),
            FactRequirement("expected_ship_time", "query_expected_ship_time", ("order_identified",)),
        ),
        ("order_identified", "order_status", "shipping_status", "expected_ship_time"),
        _shipment_complete,
        partial_completion_criteria=_shipment_partial_complete,
    ),
    "after_sales.check_after_sales": WorkflowDefinition(
        "after_sales.check_after_sales",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("service_order_identified", "check_after_sales", ("order_identified",)),
            FactRequirement("service_order_status", "check_after_sales", ("service_order_identified",)),
        ),
        ("service_order_identified", "service_order_status"),
        _after_sales_complete,
    ),
    "after_sales.after_sales_transition": WorkflowDefinition(
        "after_sales.after_sales_transition",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("service_order_identified", "check_after_sales", ("order_identified",)),
            FactRequirement("service_order_status", "check_after_sales", ("service_order_identified",)),
            FactRequirement("exchange_eligibility", "check_exchange_eligibility", ("service_order_identified",)),
        ),
        ("service_order_identified", "service_order_status", "exchange_eligibility"),
        _transition_complete,
        readiness_criteria=_transition_ready,
        write_confirmation_required=True,
    ),
    "after_sales.return_logistics": WorkflowDefinition(
        "after_sales.return_logistics",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("service_order_identified", "check_after_sales", ("order_identified",)),
            FactRequirement("service_order_status", "check_after_sales", ("service_order_identified",)),
            FactRequirement("pickup_status", "query_pickup_status", ("service_order_identified",)),
        ),
        ("service_order_identified", "service_order_status", "pickup_status"),
        _pickup_complete,
        partial_completion_criteria=_after_sales_partial_complete,
    ),
    "inventory.check_stock": WorkflowDefinition(
        "inventory.check_stock",
        (FactRequirement("stock_status", "check_stock"),),
        ("stock_status",),
        _always_if_fact("stock_status"),
    ),
    "refund.refund_status": WorkflowDefinition(
        "refund.refund_status",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
        ),
        ("order_identified", "refund_status"),
        _refund_complete,
        readiness_criteria=_refund_ready,
    ),
    "refund.procedure": WorkflowDefinition(
        "refund.procedure",
        (),
        (),
        _always_complete,
    ),
    "refund.refund_detail": WorkflowDefinition(
        "refund.refund_detail",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_amount", "query_refund_status", ("order_identified",)),
        ),
        ("order_identified", "refund_status", "refund_amount"),
        _refund_amount_complete,
        readiness_criteria=lambda facts: (
            _has_status(facts, "order_identified")
            and _has_status(facts, "refund_status")
            and _has_status(facts, "refund_amount")
        ),
    ),
    "refund.expected_arrival": WorkflowDefinition(
        "refund.expected_arrival",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("expected_arrival_time", "query_refund_expected_arrival", ("refund_status",)),
        ),
        ("order_identified", "refund_status", "expected_arrival_time"),
        _refund_expected_arrival_complete,
        partial_completion_criteria=_refund_complete,
    ),
    "refund.processing_time": WorkflowDefinition(
        "refund.processing_time",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_processing_sla", "query_refund_processing_sla", ("refund_status",)),
        ),
        ("order_identified", "refund_status", "refund_processing_sla"),
        _refund_processing_time_complete,
        partial_completion_criteria=_refund_complete,
    ),
    "refund.anomaly": WorkflowDefinition(
        "refund.anomaly",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_failure_reason", "query_refund_failure_reason", ("refund_status",)),
        ),
        ("order_identified", "refund_status", "refund_failure_reason"),
        _refund_anomaly_complete,
        partial_completion_criteria=_refund_complete,
    ),
    "refund.destination": WorkflowDefinition(
        "refund.destination",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_destination", "query_refund_destination", ("refund_status",)),
        ),
        ("order_identified", "refund_status", "refund_destination"),
        _refund_destination_complete,
        partial_completion_criteria=_refund_complete,
    ),
    "refund.eligibility": WorkflowDefinition(
        "refund.eligibility",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_eligibility", "check_refund_eligibility", ("order_identified",)),
        ),
        ("order_identified", "refund_eligibility"),
        _refund_eligibility_complete,
    ),
    "refund.request": WorkflowDefinition(
        "refund.request",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_eligibility", "check_refund_eligibility", ("order_identified",)),
            FactRequirement("refund_entry", "generate_refund_entry", ("refund_eligibility",)),
        ),
        ("order_identified", "refund_status", "refund_eligibility", "refund_entry"),
        _refund_request_complete,
    ),
    "refund.cancel": WorkflowDefinition(
        "refund.cancel",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
            FactRequirement("refund_cancel_eligibility", "check_refund_cancel_eligibility", ("refund_status",)),
        ),
        ("order_identified", "refund_status", "refund_cancel_eligibility"),
        _refund_cancel_complete,
        partial_completion_criteria=_refund_complete,
    ),
    "return.refund_dependency": WorkflowDefinition(
        "return.refund_dependency",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("return_status", "query_return_status", ("order_identified",)),
            FactRequirement("return_logistics_status", "query_return_logistics", ("return_status",)),
            FactRequirement("warehouse_receipt_status", "query_warehouse_receipt", ("return_status",)),
            FactRequirement("refund_status", "query_refund_status", ("order_identified",)),
        ),
        (
            "order_identified",
            "return_status",
            "return_logistics_status",
            "warehouse_receipt_status",
            "refund_status",
        ),
        _return_refund_dependency_complete,
    ),
    "price_protection.refund_status": WorkflowDefinition(
        "price_protection.refund_status",
        (
            FactRequirement("order_identified", "track_order"),
            FactRequirement("price_protection_status", "query_price_protection", ("order_identified",)),
        ),
        ("order_identified", "price_protection_status"),
        _price_protection_status_complete,
        readiness_criteria=_price_protection_ready,
    ),
}

# acknowledgement 是真正 no-op；clarify 则表示必须等客户补充目标。两者不能
# 共用 all([]) 的完成语义。
_CONVERSATION_NOOP_REQUESTS: set[str] = set()
_CLARIFICATION_REQUESTS = {
    "refund.clarify",
}


def _request_key(request: dict[str, Any]) -> str:
    return f"{str(request.get('domain') or '')}.{str(request.get('operation') or '')}"


def _is_conversation_noop(request: dict[str, Any]) -> bool:
    return _request_key(request) in _CONVERSATION_NOOP_REQUESTS


def clarification_required(requests: list[dict[str, Any]]) -> bool:
    """是否必须等待客户补全目标；这不是一个已完成的 no-op。"""
    return any(_request_key(request) in _CLARIFICATION_REQUESTS for request in requests)


def resolve_workflow(request: dict[str, Any]) -> WorkflowDefinition | None:
    # Goal taxonomy is the only domain/operation -> Workflow contract. A canonical Goal
    # may intentionally have no Workflow yet; callers must expose that coverage gap rather
    # than silently treating a spelling-compatible key as supported.
    workflow_key = workflow_key_for_goal(
        str(request.get("domain") or ""),
        str(request.get("operation") or ""),
    )
    return _WORKFLOWS.get(workflow_key or "")


def completion_satisfied(requests: list[dict[str, Any]], facts: dict[str, Any]) -> bool:
    if not requests:
        return False
    if clarification_required(requests):
        return False
    workflows: list[WorkflowDefinition] = []
    for request in requests:
        if _is_conversation_noop(request):
            continue
        workflow = resolve_workflow(request)
        if workflow is None:
            # 未覆盖的请求绝不能从全局完成判定中被过滤掉。
            return False
        workflows.append(workflow)
    return all(workflow.completion_criteria(facts) for workflow in workflows)


def partial_completion_satisfied(requests: list[dict[str, Any]], facts: dict[str, Any]) -> bool:
    """Return whether every request has a safe, explicitly declared partial answer.

    A partial answer is a presentation/degradation rule, never a substitute for
    the Workflow's full completion criteria.  Workflows without an explicit
    predicate remain hard-blocked when a required capability is unavailable.
    """
    if not requests or clarification_required(requests):
        return False
    predicates: list[Callable[[dict[str, Any]], bool]] = []
    for request in requests:
        if _is_conversation_noop(request):
            continue
        workflow = resolve_workflow(request)
        if workflow is None or workflow.partial_completion_criteria is None:
            return False
        predicates.append(workflow.partial_completion_criteria)
    return bool(predicates) and all(predicate(facts) for predicate in predicates)


def readiness_satisfied(requests: list[dict[str, Any]], facts: dict[str, Any]) -> bool:
    """判断是否已具备进入确认/执行边界的事实，不代表用户目标已完成。"""
    if not requests:
        return False
    if clarification_required(requests):
        return False
    workflows: list[WorkflowDefinition] = []
    for request in requests:
        if _is_conversation_noop(request):
            continue
        workflow = resolve_workflow(request)
        if workflow is None:
            return False
        workflows.append(workflow)
    return all((workflow.readiness_criteria or workflow.completion_criteria)(facts) for workflow in workflows)


def confirmation_required(requests: list[dict[str, Any]]) -> bool:
    """返回是否跨越写操作确认边界；已注册 Workflow 优先于 LLM risk 字段。"""
    write_operations = {
        "refund",
        "refund_request",
        "request",
        "cancel",
        "exchange",
        "repair",
        "invoice",
        "price_protection",
        "delivery_instruction",
        "after_sales_transition",
    }
    for request in requests:
        if _is_conversation_noop(request) or _request_key(request) in _CLARIFICATION_REQUESTS:
            continue
        workflow = resolve_workflow(request)
        if workflow is not None:
            if workflow.write_confirmation_required:
                return True
            continue
        # 未注册 Workflow 也不能因为 Router 没填 risk 就失去确认边界。
        # 这是 Control Plane 的保底判断，不采信模型自报的 required_tools/risk。
        if str(request.get("operation") or "") in write_operations:
            return True
        if str(request.get("risk") or "read_only") != "read_only":
            return True
    return False


def build_execution_plan(requests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """从 Workflow 定义构造读取计划；绝不读取 LLM required_tools。"""

    plan: list[dict[str, Any]] = []
    required_facts: list[str] = []
    completion_facts: list[str] = []
    steps_by_capability: dict[str, dict[str, Any]] = {}
    for request in requests:
        if _is_conversation_noop(request) or _request_key(request) in _CLARIFICATION_REQUESTS:
            continue
        workflow = resolve_workflow(request)
        if workflow is None:
            continue
        completion_facts.extend(fact for fact in workflow.completion_facts if fact not in completion_facts)
        for fact in workflow.facts:
            if fact.name not in required_facts:
                required_facts.append(fact.name)
            existing_step = steps_by_capability.get(fact.capability)
            if existing_step is not None:
                expected_results = existing_step["expected_results"]
                if fact.name not in expected_results:
                    expected_results.append(fact.name)
                continue
            capability = get_capability(fact.capability)
            capability_outputs = list(capability.outputs) if capability else []
            step = {
                "step": len(plan) + 1,
                "action": "read_fact",
                "tool": fact.capability,
                "precondition": list(fact.depends_on) or ["authenticated_customer_context"],
                "expected_result": fact.name,
                "expected_results": [fact.name],
                "capability_outputs": capability_outputs,
                "available": bool(capability and capability.available),
                "read_only": bool(capability is None or capability.read_only),
                "workflow": workflow.key,
            }
            plan.append(step)
            steps_by_capability[fact.capability] = step
    return plan, list(dict.fromkeys(required_facts)), completion_facts


def validate_execution_plan(requests: list[dict[str, Any]], plan: list[dict[str, Any]]) -> dict[str, Any]:
    """校验计划结构，并单独报告能力覆盖缺口。

    ``valid`` 只判断计划是否覆盖了 Workflow 声明的事实且没有把写能力混入
    只读阶段；``coverage_complete`` 还要求这些能力在当前代码中确实可用。
    二者分开，评测时才能把 Planner 错误和 Tool Coverage Error 分开统计。
    """

    _, required_facts, _ = build_execution_plan(requests)
    unsupported_workflows = list(
        dict.fromkeys(
            _request_key(request)
            for request in requests
            if not _is_conversation_noop(request)
            and _request_key(request) not in _CLARIFICATION_REQUESTS
            and resolve_workflow(request) is None
        )
    )
    conversation_noop_requests = list(
        dict.fromkeys(_request_key(request) for request in requests if _is_conversation_noop(request))
    )
    clarification_requests = list(
        dict.fromkeys(_request_key(request) for request in requests if _request_key(request) in _CLARIFICATION_REQUESTS)
    )
    planned_facts = {
        str(fact)
        for step in plan
        if isinstance(step, dict)
        for fact in step.get("expected_results", [step.get("expected_result")])
        if isinstance(fact, str)
    }
    missing_facts = [fact for fact in required_facts if fact not in planned_facts]
    output_mismatches = [
        {
            "capability": step.get("tool"),
            "facts": [
                fact
                for fact in step.get("expected_results", [step.get("expected_result")])
                if isinstance(fact, str) and fact not in step.get("capability_outputs", [])
            ],
        }
        for step in plan
        if isinstance(step, dict)
        and any(
            isinstance(fact, str) and fact not in step.get("capability_outputs", [])
            for fact in step.get("expected_results", [step.get("expected_result")])
        )
    ]
    unavailable_capabilities = list(
        dict.fromkeys(
            str(step.get("tool"))
            for step in plan
            if isinstance(step, dict) and step.get("available") is False and isinstance(step.get("tool"), str)
        )
    )
    unsafe_steps = [step.get("tool") for step in plan if isinstance(step, dict) and step.get("read_only") is False]
    return {
        "valid": not missing_facts and not unsafe_steps and not output_mismatches,
        "coverage_complete": not missing_facts and not unavailable_capabilities and not unsupported_workflows,
        "required_facts": required_facts,
        "planned_facts": sorted(planned_facts),
        "missing_facts": missing_facts,
        "unsupported_workflows": unsupported_workflows,
        "conversation_noop_requests": conversation_noop_requests,
        "clarification_requests": clarification_requests,
        "unavailable_capabilities": unavailable_capabilities,
        "unsafe_steps": unsafe_steps,
        "output_mismatches": output_mismatches,
    }


def extract_decision_facts(capability: str, data: dict[str, Any]) -> dict[str, Any]:
    """只从工具响应提取已证实的业务事实；缺字段就是未证实，不可补全。"""

    if capability == "track_order":
        order = data if isinstance(data.get("order_id"), str) else None
        if order is None and data.get("count") == 1 and isinstance(data.get("orders"), list):
            order = data["orders"][0] if data["orders"] else None
        if not isinstance(order, dict) or data.get("selection_required"):
            return {}
        order_facts: dict[str, Any] = {
            "order_identified": bool(order.get("order_id")),
            "order_status": order.get("status"),
            "shipping_status": order.get("delivery_state"),
        }
        if order.get("expected_ship_time"):
            order_facts["expected_ship_time"] = order["expected_ship_time"]
        refund = order.get("refund")
        if isinstance(refund, dict) and refund.get("status") not in (None, ""):
            order_facts["refund_status"] = _canonical_refund_status(refund["status"])
        return {key: value for key, value in order_facts.items() if value not in (None, "")}
    if capability == "check_after_sales":
        records = data.get("after_sales")
        if not isinstance(records, list) or len(records) != 1:
            return {}
        record = records[0]
        if not isinstance(record, dict) or not record.get("after_sale_id"):
            return {}
        after_sales_facts: dict[str, Any] = {
            "service_order_identified": record["after_sale_id"],
            "service_order_status": record.get("status"),
        }
        # 当前后端未承诺这些字段；如果后续工具返回明确值，归一化层接住它，
        # 但不会从 reason/status 猜测取件或资格结论。
        for fact in ("pickup_status", "exchange_eligibility", "return_eligibility"):
            if fact in record:
                after_sales_facts[fact] = record[fact]
        return {key: value for key, value in after_sales_facts.items() if value not in (None, "")}
    if capability == "query_refund_status":
        refunds = data.get("refunds")
        if data.get("refund_lookup") == "no_record":
            return {"refund_status": "NOT_FOUND"}
        if not isinstance(refunds, list) or len(refunds) != 1 or data.get("selection_required"):
            return {}
        refund = refunds[0]
        if not isinstance(refund, dict) or not refund.get("refund_id"):
            return {}
        raw_status = str(refund.get("status") or "").upper()
        # 不同存储/支付适配层的状态统一成客户可见的 Decision Fact 规范值；
        # 未知状态不猜测为成功或失败。
        status = _canonical_refund_status(raw_status)
        if not status:
            return {}
        refund_facts: dict[str, Any] = {"refund_status": status}
        if refund.get("amount_cents") is not None:
            refund_facts["refund_amount"] = refund["amount_cents"]
        return refund_facts
    if capability == "check_refund_eligibility":
        eligibility = data.get("refund_eligibility")
        if isinstance(eligibility, bool):
            return {"refund_eligibility": eligibility}
        return {}
    if capability == "generate_refund_entry":
        entry = data.get("refund_entry")
        if isinstance(entry, str) and entry.startswith("?page=orders&refund_order=SO"):
            return {"refund_entry": entry}
        return {}
    if capability == "check_payment_status":
        payment_facts: dict[str, Any] = {}
        payment_result = str(data.get("payment_result") or "")
        if payment_result in {"支付成功", "PAID"}:
            payment_facts["payment_status"] = "PAID"
        elif payment_result in {"订单尚未支付", "PENDING", "NOT_PAID"}:
            payment_facts["payment_status"] = "PENDING"
        elif payment_result in {"PAYMENT_NOT_CREATED", "PAYMENT_STATUS_UNAVAILABLE"}:
            payment_facts["payment_status"] = payment_result
        order = data.get("order")
        if isinstance(order, dict):
            if order.get("order_id") not in (None, ""):
                payment_facts["order_identified"] = True
            if order.get("status") not in (None, ""):
                payment_facts["order_status"] = order["status"]
        return payment_facts
    if capability == "check_stock":
        results = data.get("results")
        if not isinstance(results, list) or not results:
            return {}
        availability = {str(item.get("availability") or "") for item in results if isinstance(item, dict)}
        availability.discard("")
        if not availability:
            return {}
        if availability == {"有货"}:
            status = "IN_STOCK"
        elif availability == {"暂时缺货"}:
            status = "OUT_OF_STOCK"
        else:
            status = "MIXED"
        return {"stock_status": status, "stock_match_count": len(results)}
    return {}


def extract_decision_context(
    capability: str,
    data: dict[str, Any],
    *,
    requested_order_id: Any = None,
    provenance: str = "current",
) -> dict[str, Any] | None:
    """把一次受控工具结果封装成带 subject 的事实组。

    ``requested_order_id`` 只能来自服务端绑定或工具调用参数；没有唯一、可信订单
    subject 时，即使 flat fact 可以提取，也不创建 context，避免多订单事实串线。
    """
    facts = extract_decision_facts(capability, data)
    if not facts or provenance not in {"current", "historical"}:
        return None

    requested_subject_id = requested_order_id if isinstance(requested_order_id, str) else ""
    subject_id = requested_subject_id
    if not subject_id.startswith("SO"):
        subject_id = ""

    def bind_candidate(candidate: Any) -> bool:
        """Bind a returned subject, rejecting a tool response for another order."""
        nonlocal subject_id
        if not isinstance(candidate, str) or not candidate.startswith("SO"):
            return True
        if requested_subject_id.startswith("SO") and candidate != requested_subject_id:
            return False
        subject_id = candidate
        return True

    if capability == "track_order":
        if data.get("selection_required"):
            return None
        candidate = data.get("order_id") or data.get("order_no")
        if not isinstance(candidate, str) and data.get("count") == 1:
            orders = data.get("orders")
            if isinstance(orders, list) and len(orders) == 1 and isinstance(orders[0], dict):
                candidate = orders[0].get("order_id") or orders[0].get("order_no")
        if not bind_candidate(candidate):
            return None
    elif capability == "query_refund_status":
        if data.get("selection_required"):
            return None
        refunds = data.get("refunds")
        if isinstance(refunds, list) and len(refunds) == 1 and isinstance(refunds[0], dict):
            candidate = refunds[0].get("order_id") or refunds[0].get("order_no")
            if not bind_candidate(candidate):
                return None
    elif capability in {"check_refund_eligibility", "generate_refund_entry"}:
        candidate = data.get("order_id")
        if not bind_candidate(candidate):
            return None
    elif capability == "check_payment_status":
        order = data.get("order")
        candidate = (order.get("order_id") or order.get("order_no")) if isinstance(order, dict) else None
        if not bind_candidate(candidate):
            return None

    if not subject_id.startswith("SO"):
        return None
    return {
        "subject_type": "order",
        "subject_id": subject_id,
        "provenance": provenance,
        "source": capability,
        "facts": dict(facts),
    }


def _canonical_refund_status(value: Any) -> str:
    normalized = customer_visible_refund_status(value)
    if normalized == "IN_PROGRESS":
        return "PROCESSING"
    return normalized or ""
