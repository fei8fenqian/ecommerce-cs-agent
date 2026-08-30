"""客服规划第一阶段使用的最小事实本体与能力注册表。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FactDefinition:
    fact_id: str
    source: str
    producer: str
    depends_on: tuple[str, ...] = ()


_SYSTEM = "system_observable"
_USER = "user_only"

_DEFINITIONS = (
    FactDefinition("order_identified", _SYSTEM, "identify_order"),
    FactDefinition("service_order_identified", _SYSTEM, "identify_service_order", ("order_identified",)),
    FactDefinition("order_status", _SYSTEM, "query_order_status", ("order_identified",)),
    FactDefinition("shipping_status", _SYSTEM, "query_shipping_status", ("order_identified",)),
    FactDefinition("expected_ship_time", _SYSTEM, "query_expected_ship_time", ("order_identified",)),
    FactDefinition("delivery_eta", _SYSTEM, "query_delivery_eta", ("order_identified",)),
    FactDefinition("delivery_status", _SYSTEM, "query_delivery_status", ("order_identified",)),
    FactDefinition("pickup_status", _SYSTEM, "query_pickup_status", ("service_order_identified",)),
    FactDefinition("refund_status", _SYSTEM, "query_refund_status", ("order_identified",)),
    FactDefinition("after_sales_eligibility", _SYSTEM, "query_after_sales_eligibility", ("order_identified",)),
    FactDefinition("exchange_eligibility", _SYSTEM, "query_exchange_eligibility", ("order_identified",)),
    FactDefinition("service_order_status", _SYSTEM, "query_service_order_status", ("service_order_identified",)),
    FactDefinition("return_logistics_status", _SYSTEM, "query_return_logistics", ("service_order_identified",)),
    FactDefinition(
        "price_protection_eligibility", _SYSTEM, "query_price_protection_eligibility", ("order_identified",)
    ),
    FactDefinition(
        "price_protection_application_status", _SYSTEM, "query_price_protection_status", ("order_identified",)
    ),
    FactDefinition("membership_status", _SYSTEM, "query_membership_status"),
    FactDefinition("auto_renew_status", _SYSTEM, "query_auto_renew_status", ("membership_status",)),
    FactDefinition("repair_status", _SYSTEM, "query_repair_status", ("service_order_identified",)),
    FactDefinition("fulfillment_status", _SYSTEM, "query_fulfillment_status", ("order_identified",)),
    FactDefinition("product_model", _SYSTEM, "query_order"),
    FactDefinition("product_identified", _SYSTEM, "identify_product"),
    FactDefinition("user_problem_description", _USER, "ask_customer"),
    FactDefinition("exchange_reason", _USER, "ask_customer"),
    FactDefinition("destination_region", _USER, "ask_customer"),
    FactDefinition("phone_model", _USER, "ask_customer"),
)

FACTS = {definition.fact_id: definition for definition in _DEFINITIONS}


def ontology_for_prompt() -> list[dict[str, object]]:
    return [
        {
            "fact": item.fact_id,
            "source": item.source,
            "producer": item.producer,
            "depends_on": list(item.depends_on),
        }
        for item in _DEFINITIONS
    ]


def build_plan_for_facts(facts: list[str]) -> tuple[list[dict[str, object]], list[str]]:
    """由所选决策事实确定性推导 capability 顺序及当前 next action。"""

    required: list[str] = []

    def include(fact_id: str) -> None:
        definition = FACTS.get(fact_id)
        if not definition or fact_id in required:
            return
        for dependency in definition.depends_on:
            include(dependency)
        required.append(fact_id)

    for fact in facts:
        include(fact)

    steps: list[dict[str, object]] = []
    capabilities: list[str] = []
    for index, fact_id in enumerate(required, start=1):
        definition = FACTS[fact_id]
        capability = definition.producer
        if capability in capabilities:
            continue
        capabilities.append(capability)
        steps.append(
            {
                "step_id": str(index),
                "path": "happy_path",
                "precondition": list(definition.depends_on),
                "capability": capability,
                "expected_result": fact_id,
                "success_branch": "continue",
                "failure_branch": "evaluate_fallback",
            }
        )
    return steps, capabilities


def validate_plan(
    facts: list[str], steps: list[dict[str, object]], next_capability: str, case_status: str
) -> list[str]:
    """检查本体覆盖、前置依赖及 next_action / 状态的一致性。"""

    violations: list[str] = []
    expected_steps, _ = build_plan_for_facts(facts)
    expected_capabilities = [str(step["capability"]) for step in expected_steps]
    observed_capabilities = [str(step.get("capability") or "") for step in steps]
    if expected_capabilities != observed_capabilities:
        violations.append("capability_chain_does_not_cover_required_facts")
    if next_capability != (expected_capabilities[0] if expected_capabilities else ""):
        violations.append("next_action_is_not_the_first_dependency_ready_capability")
    expected_status = "AWAITING_CUSTOMER" if next_capability == "ask_customer" else "READY_TO_EXECUTE"
    if case_status != expected_status:
        violations.append("case_status_is_inconsistent_with_next_action")
    return violations
