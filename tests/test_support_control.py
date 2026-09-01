"""轻量客服 Control Plane 与事实归一化测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.engines.loop import AgentLoop
from agent.llm.llm_client import LLMResponse, TokenUsage, ToolCall
from agent.support_control import (
    build_execution_plan,
    completion_satisfied,
    confirmation_required,
    extract_decision_context,
    extract_decision_facts,
    partial_completion_satisfied,
    readiness_satisfied,
    resolve_workflow,
    validate_execution_plan,
)
from agent.tools.check_refund_eligibility import CheckRefundEligibility
from agent.tools.query_refund_status import QueryRefundStatus
from agent.tools_registry import BaseTool, ToolContext, ToolRegistry, ToolResult
from service.checkout_refund_service import customer_visible_refund_status
from store.checkout_store import CustomerCheckoutOrder


def test_false_eligibility_is_known_and_can_finish_with_explanation():
    requests = [
        {
            "domain": "after_sales",
            "operation": "after_sales_transition",
            "risk": "customer_confirmation",
        }
    ]
    facts = {
        "service_order_identified": "AS-1",
        "service_order_status": "UNDER_REVIEW",
        "exchange_eligibility": False,
    }

    assert readiness_satisfied(requests, facts) is True
    assert completion_satisfied(requests, facts) is True


def test_financial_refund_operations_do_not_create_agent_confirmation_boundary():
    assert confirmation_required([{"domain": "refund", "operation": "request", "risk": "read_only"}]) is False
    assert confirmation_required([{"domain": "refund", "operation": "cancel", "required_tools": []}]) is False


@pytest.mark.parametrize(
    ("domain", "operation"),
    [
        ("delivery", "track_order"),
        ("after_sales", "check_after_sales"),
        ("after_sales", "after_sales_transition"),
        ("after_sales", "return_logistics"),
        ("inventory", "check_stock"),
        ("refund", "status"),
        ("refund", "expected_arrival"),
        ("refund", "processing_time"),
        ("refund", "anomaly"),
        ("refund", "amount"),
        ("refund", "destination"),
        ("refund", "eligibility"),
        ("refund", "request"),
        ("refund", "procedure"),
        ("refund", "cancel"),
        ("return", "refund_dependency"),
        ("price_protection", "refund_status"),
    ],
)
def test_control_plane_routes_have_registered_workflows(domain, operation):
    assert resolve_workflow({"domain": domain, "operation": operation}) is not None


def test_plan_exposes_capability_coverage_without_using_similar_tools():
    requests = [{"domain": "delivery", "operation": "track_order"}]

    plan, required_facts, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert plan[0]["tool"] == "track_order"
    assert plan[0]["expected_results"] == ["order_identified", "order_status", "shipping_status"]
    assert plan[1]["tool"] == "query_expected_ship_time"
    assert plan[1]["available"] is False
    assert "expected_ship_time" in required_facts
    assert validation["valid"] is True
    assert validation["coverage_complete"] is False
    assert validation["unavailable_capabilities"] == ["query_expected_ship_time"]


def test_missing_workflow_is_structurally_valid_but_never_coverage_complete():
    requests = [{"domain": "refund", "operation": "unimplemented_operation"}]

    plan, required_facts, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert plan == []
    assert required_facts == []
    assert validation["valid"] is True
    assert validation["coverage_complete"] is False
    assert validation["unsupported_workflows"] == ["refund.unimplemented_operation"]
    assert completion_satisfied(requests, {}) is False
    assert readiness_satisfied(requests, {}) is False


def test_refund_destination_declares_real_capability_gap_instead_of_silent_empty_plan():
    requests = [{"domain": "refund", "operation": "destination"}]

    plan, required_facts, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert [step["tool"] for step in plan] == [
        "track_order",
        "query_refund_status",
        "query_refund_destination",
    ]
    assert required_facts == ["order_identified", "refund_status", "refund_destination"]
    assert validation["valid"] is True
    assert validation["coverage_complete"] is False
    assert validation["unsupported_workflows"] == []
    assert validation["unavailable_capabilities"] == ["query_refund_destination"]


def test_multi_goal_request_cannot_complete_when_any_goal_lacks_its_facts():
    requests = [
        {"domain": "refund", "operation": "status"},
        {"domain": "refund", "operation": "destination"},
    ]
    facts = {"order_identified": True, "refund_status": "PROCESSING"}

    assert completion_satisfied(requests, facts) is False
    assert readiness_satisfied(requests, facts) is False


def test_refund_expected_arrival_does_not_complete_from_processing_status_alone():
    requests = [{"domain": "refund", "operation": "expected_arrival"}]
    facts = {"order_identified": True, "refund_status": "PROCESSING"}

    plan, _, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert completion_satisfied(requests, facts) is False
    assert validation["coverage_complete"] is False
    assert validation["unavailable_capabilities"] == ["query_refund_expected_arrival"]
    assert partial_completion_satisfied(requests, facts) is True


def test_refund_request_does_not_degrade_when_the_safe_self_service_entry_is_missing():
    facts = {
        "order_identified": True,
        "refund_status": "NOT_FOUND",
        "refund_eligibility": True,
    }

    assert partial_completion_satisfied([{"domain": "refund", "operation": "request"}], facts) is False


def test_refund_eligibility_false_is_a_known_policy_result():
    requests = [{"domain": "refund", "operation": "eligibility"}]
    facts = {"order_identified": True, "refund_eligibility": False}

    assert completion_satisfied(requests, facts) is True
    assert readiness_satisfied(requests, facts) is True


def test_pending_payment_order_resolves_refund_request_as_cancel_handoff_not_refund():
    facts = {"order_identified": True, "order_status": "PENDING_PAYMENT"}

    assert completion_satisfied([{"domain": "refund", "operation": "eligibility"}], facts) is True
    assert completion_satisfied([{"domain": "refund", "operation": "request"}], facts) is True
    assert confirmation_required([{"domain": "order", "operation": "cancel"}]) is False


def test_payment_and_pending_order_cancel_workflows_are_registered():
    payment = [{"domain": "payment", "operation": "check_payment_status"}]
    cancel = [{"domain": "order", "operation": "cancel"}]

    assert completion_satisfied(payment, {"order_identified": True, "payment_status": "PAYMENT_STATUS_UNAVAILABLE"})
    assert completion_satisfied(cancel, {"order_identified": True, "order_status": "PENDING_PAYMENT"})


def test_price_protection_status_is_read_only():
    requests = [{"domain": "price_protection", "operation": "refund_status"}]
    facts = {"order_identified": True, "price_protection_status": "PROCESSING"}
    workflow = resolve_workflow(requests[0])

    assert workflow is not None
    assert workflow.write_confirmation_required is False
    assert confirmation_required(requests) is False
    assert completion_satisfied(requests, facts) is True
    assert readiness_satisfied(requests, facts) is True


def test_refund_request_requires_self_service_entry_not_agent_confirmation():
    requests = [{"domain": "refund", "operation": "request"}]
    facts = {
        "order_identified": True,
        "refund_status": "NOT_FOUND",
        "refund_eligibility": True,
    }

    assert confirmation_required(requests) is False
    assert readiness_satisfied(requests, facts) is False
    assert completion_satisfied(requests, facts) is False

    facts["refund_entry"] = "?page=orders&refund_order=SO-1"
    assert completion_satisfied(requests, facts) is True


def test_refund_request_plan_uses_available_read_eligibility_capability():
    requests = [{"domain": "refund", "operation": "request"}]
    plan, _, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert "check_refund_eligibility" in [step["tool"] for step in plan]
    assert "generate_refund_entry" in [step["tool"] for step in plan]
    assert validation["coverage_complete"] is True


def test_refund_procedure_is_deterministic_informational_workflow():
    requests = [{"domain": "refund", "operation": "procedure"}]

    plan, required_facts, completion_facts = build_execution_plan(requests)

    assert plan == []
    assert required_facts == []
    assert completion_facts == []
    assert validate_execution_plan(requests, plan)["coverage_complete"] is True
    assert completion_satisfied(requests, {}) is True


def test_refund_cancel_exposes_unavailable_capability_without_claiming_success():
    requests = [{"domain": "refund", "operation": "cancel"}]

    plan, _, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert validation["valid"] is True
    assert validation["coverage_complete"] is False
    assert validation["unavailable_capabilities"] == ["check_refund_cancel_eligibility"]
    assert confirmation_required(requests) is False


def test_clarification_request_is_not_treated_as_completed_conversation_noop():
    requests = [{"domain": "refund", "operation": "clarify"}]
    plan, _, _ = build_execution_plan(requests)
    validation = validate_execution_plan(requests, plan)

    assert plan == []
    assert validation["coverage_complete"] is True
    assert validation["clarification_requests"] == ["refund.clarify"]
    assert completion_satisfied(requests, {}) is False
    assert readiness_satisfied(requests, {}) is False


def test_normalizer_preserves_negative_and_normalizes_core_read_results():
    assert extract_decision_facts(
        "check_stock",
        {"results": [{"availability": "暂时缺货"}]},
    ) == {"stock_status": "OUT_OF_STOCK", "stock_match_count": 1}
    assert extract_decision_facts(
        "query_refund_status",
        {
            "refunds": [{"refund_id": "RF-1", "status": "SUCCEEDED", "amount_cents": 100}],
            "selection_required": False,
        },
    ) == {"refund_status": "COMPLETED", "refund_amount": 100}
    assert extract_decision_facts("query_refund_status", {"refund_lookup": "no_record"}) == {
        "refund_status": "NOT_FOUND"
    }
    assert extract_decision_facts(
        "check_refund_eligibility",
        {"order_id": "SO-1", "refund_eligibility": False},
    ) == {"refund_eligibility": False}


def test_decision_context_rejects_tool_result_for_different_bound_order():
    context = extract_decision_context(
        "query_refund_status",
        {
            "refund_lookup": "found",
            "selection_required": False,
            "refunds": [
                {
                    "refund_id": "RF-2",
                    "order_id": "SO-2",
                    "status": "PROCESSING",
                    "amount_cents": 100,
                }
            ],
        },
        requested_order_id="SO-1",
    )

    assert context is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PENDING_CONFIRMATION", "PENDING_CONFIRMATION"),
        ("PENDING_FINANCE_APPROVAL", "PENDING_MERCHANT_REVIEW"),
        ("PROCESSING", "PROCESSING"),
        ("SUCCEEDED", "COMPLETED"),
        ("REJECTED", "FAILED"),
    ],
)
def test_refund_status_workflow_completes_without_eta(status, expected):
    requests = [{"domain": "refund", "operation": "refund_status"}]
    facts = {"order_identified": True, "refund_status": expected}

    assert customer_visible_refund_status(status) == expected
    assert completion_satisfied(requests, facts) is True
    plan, required_facts, _ = build_execution_plan(requests)
    assert [step["tool"] for step in plan] == ["track_order", "query_refund_status"]
    assert required_facts == ["order_identified", "refund_status"]
    assert "query_refund_expected_arrival" not in [step["tool"] for step in plan]


class _TrackOrderTool(BaseTool):
    @property
    def name(self) -> str:
        return "track_order"

    @property
    def description(self) -> str:
        return "测试查单"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        return ToolResult(
            name=self.name,
            status="success",
            data={
                "order_id": "SO-1",
                "status": "PAID",
                "delivery_state": "NOT_SHIPPED",
            },
        )


@pytest.mark.asyncio
async def test_agent_loop_normalizes_tool_results_after_pre_read_stage():
    class _LLM:
        model = "test"

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=[ToolCall(id="c1", name="track_order", arguments={})],
                    usage=TokenUsage(),
                )
            return LLMResponse(content="已查到", usage=TokenUsage())

    registry = ToolRegistry()
    registry.register(_TrackOrderTool())
    result = await AgentLoop(_LLM(), registry).run("查订单")

    assert result.decision_facts == {
        "order_identified": True,
        "order_status": "PAID",
        "shipping_status": "NOT_SHIPPED",
    }


@pytest.mark.asyncio
async def test_query_refund_status_reads_only_owned_checkout_refunds():
    order = CustomerCheckoutOrder(
        order_no="SO-1",
        status="REFUND_PROCESSING",
        total_amount_cents=1000,
        product_name="测试商品",
        quantity=1,
        payment_status="SUCCEEDED",
        fulfillment_status="NOT_APPLICABLE",
        tracking_company=None,
        tracking_number=None,
        created_at="2026-08-28T00:00:00+00:00",
        refund_id="RF-1",
        refund_status="PROCESSING",
        refund_amount_cents=300,
    )
    with patch(
        "agent.tools.query_refund_status.list_customer_checkout_orders",
        new=AsyncMock(return_value=[order]),
    ) as list_orders:
        result = await QueryRefundStatus().execute(
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.is_success is True
    assert result.data["refunds"][0]["status"] == "PROCESSING"
    assert result.data["refunds"][0]["amount_cents"] == 300
    list_orders.assert_awaited_once_with(7, limit=30)


@pytest.mark.asyncio
async def test_query_refund_status_hides_internal_finance_state():
    order = CustomerCheckoutOrder(
        order_no="SO-2",
        status="REFUND_PENDING",
        total_amount_cents=1000,
        product_name="测试商品",
        quantity=1,
        payment_status="SUCCEEDED",
        fulfillment_status="NOT_APPLICABLE",
        tracking_company=None,
        tracking_number=None,
        created_at="2026-08-28T00:00:00+00:00",
        refund_id="RF-2",
        refund_status="PENDING_FINANCE_APPROVAL",
    )
    with patch(
        "agent.tools.query_refund_status.list_customer_checkout_orders",
        new=AsyncMock(return_value=[order]),
    ):
        result = await QueryRefundStatus().execute(tool_context=ToolContext(user_id=7, role="customer"))

    assert result.is_success is True
    assert result.data["refunds"][0]["status"] == "PENDING_MERCHANT_REVIEW"


@pytest.mark.asyncio
async def test_query_refund_status_requires_selection_for_multiple_refunds():
    def make_order(order_no: str) -> CustomerCheckoutOrder:
        return CustomerCheckoutOrder(
            order_no=order_no,
            status="REFUND_PROCESSING",
            total_amount_cents=1000,
            product_name="测试商品",
            quantity=1,
            payment_status="SUCCEEDED",
            fulfillment_status="NOT_APPLICABLE",
            tracking_company=None,
            tracking_number=None,
            created_at="2026-08-28T00:00:00+00:00",
            refund_id=f"RF-{order_no}",
            refund_status="PROCESSING",
        )

    with patch(
        "agent.tools.query_refund_status.list_customer_checkout_orders",
        new=AsyncMock(return_value=[make_order("SO-3"), make_order("SO-4")]),
    ):
        result = await QueryRefundStatus().execute(tool_context=ToolContext(user_id=7, role="customer"))

    assert result.is_success is True
    assert result.data["selection_required"] is True
    assert result.data["refund_lookup"] == "found"
    assert extract_decision_facts("query_refund_status", result.data) == {}


@pytest.mark.asyncio
async def test_query_refund_status_does_not_leak_other_customer_order():
    order = CustomerCheckoutOrder(
        order_no="SO-5",
        status="REFUND_PROCESSING",
        total_amount_cents=1000,
        product_name="测试商品",
        quantity=1,
        payment_status="SUCCEEDED",
        fulfillment_status="NOT_APPLICABLE",
        tracking_company=None,
        tracking_number=None,
        created_at="2026-08-28T00:00:00+00:00",
        refund_id="RF-5",
        refund_status="PROCESSING",
    )
    with patch(
        "agent.tools.query_refund_status.list_customer_checkout_orders",
        new=AsyncMock(return_value=[order]),
    ) as list_orders:
        result = await QueryRefundStatus().execute(
            order_id="SO-OTHER",
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.is_success is True
    assert result.data["refunds"] == []
    assert result.data["refund_lookup"] == "no_record"
    list_orders.assert_awaited_once_with(7, limit=30)


@pytest.mark.asyncio
async def test_query_refund_status_store_error_is_not_a_successful_no_record():
    with patch(
        "agent.tools.query_refund_status.list_customer_checkout_orders",
        new=AsyncMock(side_effect=RuntimeError("db unavailable")),
    ):
        result = await QueryRefundStatus().execute(tool_context=ToolContext(user_id=7, role="customer"))

    assert result.status == "error"
    assert result.data == {}


@pytest.mark.asyncio
async def test_check_refund_eligibility_is_owned_read_only_fact():
    with patch(
        "agent.tools.check_refund_eligibility.get_customer_refund_eligibility",
        new=AsyncMock(return_value=False),
    ) as eligibility:
        result = await CheckRefundEligibility().execute(
            order_id="SO-1",
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.is_success is True
    assert result.data == {"order_id": "SO-1", "refund_eligibility": False}
    eligibility.assert_awaited_once_with(customer_user_id=7, order_no="SO-1")
