"""SupportWorkflow 的最小编排测试，不连接真实模型或数据库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.engines.loop import LoopResult, StepResult
from agent.engines.support_workflow import SupportWorkflowAgent
from agent.tools_registry import ToolContext


class _FakeAgent:
    def __init__(self):
        self.calls: list[dict] = []

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        self.calls.append(
            {
                "query": query,
                "context": context,
                "history": history,
                "system_prompt_extra": system_prompt_extra,
                "tool_context": tool_context,
            }
        )
        return LoopResult(answer="已完成事实核验并请客户选择", total_steps=2, total_tokens=20)


class _ToolCallingAgent(_FakeAgent):
    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        return LoopResult(
            answer="库存已核验",
            steps=[
                StepResult(
                    step=1,
                    tool_calls=[SimpleNamespace(name="check_stock")],
                    observation="[check_stock 结果] stock: 有货",
                )
            ],
            total_steps=1,
        )


class _AnswerAgent(_FakeAgent):
    def __init__(self, answer: str):
        super().__init__()
        self.answer = answer

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        self.calls.append(
            {
                "query": query,
                "context": context,
                "history": history,
                "system_prompt_extra": system_prompt_extra,
                "tool_context": tool_context,
            }
        )
        return LoopResult(answer=self.answer, total_steps=1)


@pytest.mark.asyncio
async def test_support_workflow_delegates_to_existing_agent_loop():
    agent = _FakeAgent()
    workflow = SupportWorkflowAgent(agent)

    result = await workflow.run(
        "一件商品缺货，另一件有货怎么办？",
        history=[{"role": "user", "content": "之前的订单"}],
        system_prompt_extra="先核验事实，再询问选择",
    )

    assert result.answer == "已完成事实核验并请客户选择"
    assert result.total_steps == 2
    assert len(agent.calls) == 1
    assert agent.calls[0]["query"] == "一件商品缺货，另一件有货怎么办？"
    assert agent.calls[0]["context"] == ""
    assert agent.calls[0]["history"] == [{"role": "user", "content": "之前的订单"}]
    assert "先核验事实，再询问选择" in agent.calls[0]["system_prompt_extra"]
    assert agent.calls[0]["tool_context"] is None


class _FactRegistry:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append(name)
        return ToolResult(name=name, status="success", data={"count": 1, "orders": [{"order_id": "SO1"}]})


class _RefundFactRegistry:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append(name)
        if name == "track_order":
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "status": "PAID", "delivery_state": "NOT_SHIPPED"},
            )
        if name == "query_refund_status":
            return ToolResult(
                name=name,
                status="success",
                data={
                    "refund_lookup": "found",
                    "selection_required": False,
                    "refunds": [{"refund_id": "RF1", "status": "PROCESSING", "amount_cents": 100}],
                },
            )
        raise AssertionError(f"unexpected tool {name}")


class _RecordingRefundFactRegistry(_RefundFactRegistry):
    def __init__(self):
        super().__init__()
        self.arguments: list[tuple[str, dict]] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        self.arguments.append((name, kwargs.copy()))
        return await super().execute(name, tool_context=tool_context, **kwargs)


class _RefundRequestRegistry(_RefundFactRegistry):
    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        if name == "query_refund_status":
            self.calls.append(name)
            return ToolResult(
                name=name,
                status="success",
                data={"refund_lookup": "no_record", "refunds": [], "selection_required": False},
            )
        if name == "check_refund_eligibility":
            self.calls.append(name)
            assert kwargs == {"order_id": "SO1"}
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "refund_eligibility": True},
            )
        return await super().execute(name, tool_context=tool_context, **kwargs)


class _RefundIneligibleRegistry(_RefundRequestRegistry):
    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        if name == "check_refund_eligibility":
            self.calls.append(name)
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "refund_eligibility": False},
            )
        return await super().execute(name, tool_context=tool_context, **kwargs)


class _RefundEligibilityErrorRegistry(_RefundRequestRegistry):
    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        if name == "check_refund_eligibility":
            self.calls.append(name)
            return ToolResult(name=name, status="error", error="资格服务暂时不可用")
        return await super().execute(name, tool_context=tool_context, **kwargs)


class _DirectEligibilityRegistry:
    def __init__(self, eligible: bool):
        self.eligible = eligible
        self.calls: list[str] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append(name)
        if name == "track_order":
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "status": "PAID", "delivery_state": "NOT_SHIPPED"},
            )
        if name == "check_refund_eligibility":
            assert kwargs == {"order_id": "SO1"}
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "refund_eligibility": self.eligible},
            )
        raise AssertionError(f"unexpected tool {name}")


@pytest.mark.asyncio
async def test_support_workflow_reads_only_safe_customer_scoped_facts_before_agent():
    agent = _FakeAgent()
    registry = _FactRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "我的订单怎么还没发货",
        support_requests=[{"domain": "delivery", "operation": "track_order"}],
        tool_context=object(),
    )

    assert registry.calls == ["track_order"]
    assert result.verified_facts["track_order"]["status"] == "success"
    assert "本轮已核验的业务事实" in agent.calls[0]["system_prompt_extra"]


@pytest.mark.asyncio
async def test_expected_arrival_exposes_unavailable_eta_after_status_is_read():
    agent = _FakeAgent()
    registry = _RefundFactRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "退款什么时候到账",
        support_requests=[{"domain": "refund", "operation": "expected_arrival"}],
        tool_context=object(),
    )

    assert registry.calls == ["track_order", "query_refund_status"]
    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    assert result.workflow_progress["unavailable_capabilities"] == ["query_refund_expected_arrival"]
    assert result.workflow_progress["readiness_satisfied"] is False


@pytest.mark.asyncio
async def test_refund_status_answer_is_rendered_from_decision_facts():
    workflow = SupportWorkflowAgent(_AnswerAgent("退款已经到账支付宝。"), _RefundFactRegistry())

    result = await workflow.run(
        "这笔退款现在什么状态？",
        support_requests=[{"domain": "refund", "operation": "status"}],
        tool_context=object(),
    )

    assert result.workflow_progress["goal_status"] == "resolved"
    assert "处理中" in result.answer
    assert "到账" not in result.answer
    assert "支付宝" not in result.answer


@pytest.mark.asyncio
async def test_selected_subject_is_bound_to_pre_read_order_tool_arguments():
    registry = _RecordingRefundFactRegistry()
    agent = _FakeAgent()
    workflow = SupportWorkflowAgent(agent, registry)

    await workflow.run(
        "第二个",
        support_requests=[{"domain": "refund", "operation": "status"}],
        selected_subjects={"order_id": "SO1"},
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.arguments == [
        ("track_order", {"order_id": "SO1"}),
        ("query_refund_status", {"order_id": "SO1"}),
    ]
    assert agent.calls[0]["tool_context"].selected_order_id == "SO1"


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_direct_refund_eligibility_reads_capability_without_refund_status_guard(eligible):
    agent = _FakeAgent()
    registry = _DirectEligibilityRegistry(eligible)
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "这个订单现在还能退吗",
        support_requests=[{"domain": "refund", "operation": "eligibility"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == ["track_order", "check_refund_eligibility"]
    assert result.workflow_progress["goal_status"] == ("resolved" if eligible else "resolved_with_explanation")
    assert result.workflow_progress["decision_facts"]["refund_eligibility"] is eligible


def test_refund_eligibility_answer_does_not_promote_customer_claim_to_fact():
    result = LoopResult(
        answer="系统核验显示商品未拆封。",
        workflow_progress={
            "goal_status": "resolved",
            "decision_facts": {
                "order_identified": True,
                "refund_eligibility": True,
                "shipping_status": "NOT_SHIPPED",
            },
        },
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-ELIGIBILITY",
                "provenance": "current",
                "source": "check_refund_eligibility",
                "facts": {
                    "order_identified": True,
                    "refund_eligibility": True,
                    "shipping_status": "NOT_SHIPPED",
                },
            }
        ],
    )

    SupportWorkflowAgent._apply_refund_fact_boundary(
        result,
        [{"domain": "refund", "operation": "eligibility"}],
    )

    assert result.answer == "系统核验结果显示，这笔订单当前符合退款资格。订单当前尚未发货。"
    assert "未拆封" not in result.answer


def test_refund_fact_boundary_also_guards_plain_agent_loop_result():
    result = LoopResult(
        answer="退款已经到账支付宝。",
        decision_facts={"refund_status": "COMPLETED", "refund_amount": 799900},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-WORKFLOW",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 799900},
            }
        ],
    )

    SupportWorkflowAgent._apply_refund_fact_boundary(
        result,
        [{"domain": "refund", "operation": "status"}],
    )

    assert result.answer == "系统里的退款记录目前显示已完成。退款金额为 ¥7999.00。"
    assert "到账" not in result.answer
    assert "支付宝" not in result.answer


def test_customer_choice_boundary_and_pending_choices_keep_display_order():
    facts = {
        "track_order": {
            "status": "success",
            "data": {
                "selection_required": True,
                "orders": [
                    {
                        "order_id": "SOREAL_A6",
                        "total_amount": 9200.0,
                        "items": [{"product_name": "戴尔 Inspiron 14 笔记本"}],
                    },
                    {
                        "order_id": "SOREAL_A7",
                        "total_amount": 1899.0,
                        "items": [{"product_name": "Sony WF-1000XM5 无线耳机"}],
                    },
                ],
            },
        }
    }
    choices = SupportWorkflowAgent._pending_order_choices(facts)
    result = LoopResult(
        answer="我来帮您选择。",
        workflow_progress={"next_action": "ASK_CHOICE", "pending_choices": choices},
    )

    SupportWorkflowAgent._apply_customer_choice_boundary(result)

    assert [choice["order_id"] for choice in choices] == ["SOREAL_A6", "SOREAL_A7"]
    assert "1. SOREAL_A6" in result.answer
    assert "2. SOREAL_A7" in result.answer
    assert result.answer.index("SOREAL_A6") < result.answer.index("SOREAL_A7")


def test_declared_unavailable_capability_blocks_even_if_shared_facts_look_complete():
    plan = [
        {"tool": "track_order", "available": True},
        {"tool": "query_expected_ship_time", "available": False},
    ]
    state = {
        "support_requests": [{"domain": "delivery", "operation": "track_order"}],
        "execution_plan": plan,
        "plan_validation": {"coverage_complete": False},
        "required_decision_facts": ["order_identified", "order_status", "shipping_status"],
        "completion_facts": ["order_identified", "order_status", "shipping_status"],
        "verified_facts": {"track_order": {"status": "success"}},
        "decision_facts": {
            "order_identified": True,
            "order_status": "PAID",
            "shipping_status": "NOT_SHIPPED",
        },
        "replan_count": 0,
    }
    progress = SupportWorkflowAgent._evaluate_progress(state, LoopResult(answer=""))

    assert progress["goal_status"] == "blocked"
    assert progress["control_state"] == "BLOCKED"
    assert progress["reason"] == "capability_unavailable"
    assert progress["next_actor"] == "STAFF"


@pytest.mark.asyncio
async def test_refund_request_delivers_self_service_entry_after_eligibility():
    agent = _FakeAgent()
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    with pytest.MonkeyPatch.context() as monkeypatch:

        async def generate_entry(*, customer_user_id: int, order_no: str) -> str:
            assert customer_user_id == 7
            assert order_no == "SO1"
            return "?page=orders&refund_order=SO1"

        monkeypatch.setattr("agent.engines.support_workflow.generate_customer_refund_entry", generate_entry)
        with (
            patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
            patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
        ):
            result = await workflow.run(
                "我要申请退款",
                support_requests=[{"domain": "refund", "operation": "request"}],
                tool_context=SimpleNamespace(user_id=7, role="customer"),
            )

    assert registry.calls == ["track_order", "query_refund_status", "check_refund_eligibility"]
    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["next_actor"] == "NONE"
    assert result.workflow_progress["resolution_type"] == "SELF_SERVICE_HANDOFF"
    assert result.workflow_progress["decision_facts"]["refund_eligibility"] is True
    assert result.answer == (
        "退款资格已核验通过。\n\n"
        "请在官方订单页面自行填写退款原因并确认提交。\n"
        "客服不会代为创建、提交或确认退款。\n\n"
        "[前往我的订单申请退款](?page=orders&refund_order=SO1)"
    )
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_answer",
    [
        "确认后我将为您提交退款申请。",
        "退款入口已生成，确认后我帮您申请退款。\n?page=orders&refund_order=SO1",
    ],
)
async def test_self_service_handoff_replaces_unsafe_llm_answer(unsafe_answer):
    agent = _AnswerAgent(unsafe_answer)
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    with pytest.MonkeyPatch.context() as monkeypatch:

        async def generate_entry(*, customer_user_id: int, order_no: str) -> str:
            return "?page=orders&refund_order=SO1"

        monkeypatch.setattr("agent.engines.support_workflow.generate_customer_refund_entry", generate_entry)
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert "提交退款申请" not in result.answer
    assert "客服不会代为创建、提交或确认退款" in result.answer
    assert "?page=orders&refund_order=SO1" in result.answer


@pytest.mark.asyncio
async def test_self_service_handoff_rejects_untrusted_entry_and_blocks():
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(_AnswerAgent("可以退款。"), registry)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "agent.engines.support_workflow.generate_customer_refund_entry",
            AsyncMock(return_value="https://fake-refund.example.com"),
        )
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    assert result.workflow_progress["resolution_type"] == ""
    assert "https://fake-refund.example.com" not in result.answer
    assert "refund_entry" not in result.workflow_progress["decision_facts"]


@pytest.mark.asyncio
async def test_refund_request_ineligible_is_explained_without_entry_or_write():
    registry = _RefundIneligibleRegistry()
    workflow = SupportWorkflowAgent(_FakeAgent(), registry)

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "resolved_with_explanation"
    assert result.workflow_progress["reason"] == "policy_result_denied_with_explanation"
    assert "refund_entry" not in result.workflow_progress["decision_facts"]
    generate_entry.assert_not_awaited()
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_eligibility_failure_is_blocked_without_fake_entry():
    registry = _RefundEligibilityErrorRegistry()
    workflow = SupportWorkflowAgent(_FakeAgent(), registry)

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "fact_tool_failed"
    assert result.workflow_progress["next_actor"] == "STAFF"
    generate_entry.assert_not_awaited()
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_entry_failure_goes_to_staff_without_customer_wait():
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(_FakeAgent(), registry)

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock(return_value=None)),
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["next_actor"] == "STAFF"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_with_existing_refund_explains_status_without_new_entry():
    registry = _RefundFactRegistry()
    workflow = SupportWorkflowAgent(_FakeAgent(), registry)

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=SimpleNamespace(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["decision_facts"]["refund_status"] == "PROCESSING"
    assert "refund_entry" not in result.workflow_progress["decision_facts"]
    generate_entry.assert_not_awaited()
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_support_workflow_injects_persisted_case_context_as_trusted_state():
    agent = _FakeAgent()
    workflow = SupportWorkflowAgent(agent)

    await workflow.run(
        "第一个订单",
        case_context='{"case_status":"AWAITING_CUSTOMER","pending":{"kind":"choice"}}',
    )

    prompt = agent.calls[0]["system_prompt_extra"]
    assert "当前 Support Case（服务端可信状态" in prompt
    assert '"case_status":"AWAITING_CUSTOMER"' in prompt
    assert "不得据此执行写操作" in prompt


@pytest.mark.asyncio
async def test_support_workflow_builds_plan_and_evaluates_customer_confirmation_boundary():
    agent = _FakeAgent()
    workflow = SupportWorkflowAgent(agent)

    result = await workflow.run(
        "换货改退货",
        support_requests=[
            {
                "domain": "after_sales",
                "operation": "after_sales_transition",
                "desired_outcome": "exchange_to_return",
                "risk": "customer_confirmation",
                "next_step": "ASK_CHOICE",
            }
        ],
    )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["next_action"] == "ESCALATE_OR_EXPLAIN"
    assert result.workflow_progress["required_tools"] == ["track_order", "check_after_sales"]
    assert '"action":"read_fact"' in agent.calls[0]["system_prompt_extra"]


@pytest.mark.asyncio
async def test_support_workflow_blocks_unsupported_request_instead_of_finishing_empty_plan():
    workflow = SupportWorkflowAgent(_FakeAgent())

    result = await workflow.run(
        "退款去向怎么查",
        support_requests=[{"domain": "refund", "operation": "unimplemented_operation"}],
    )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "workflow_unsupported"
    assert result.workflow_progress["unsupported_workflows"] == ["refund.unimplemented_operation"]
    assert result.workflow_progress["plan_validation"]["coverage_complete"] is False


@pytest.mark.asyncio
async def test_support_workflow_routes_clarification_to_customer_without_completing_case():
    workflow = SupportWorkflowAgent(_FakeAgent())

    result = await workflow.run(
        "我已经申请退款了",
        support_requests=[{"domain": "refund", "operation": "clarify"}],
    )

    assert result.workflow_progress["goal_status"] == "awaiting_customer"
    assert result.workflow_progress["next_action"] == "ASK_CLARIFICATION"
    assert result.workflow_progress["next_actor"] == "CUSTOMER"


@pytest.mark.asyncio
async def test_support_workflow_evaluator_counts_successful_agent_tool_observation():
    workflow = SupportWorkflowAgent(_ToolCallingAgent())

    result = await workflow.run(
        "查一下库存",
        support_requests=[{"domain": "inventory", "operation": "check_stock"}],
    )

    assert result.workflow_progress["goal_status"] == "unresolved"
    assert result.workflow_progress["successful_tools"] == ["check_stock"]


def test_support_workflow_enters_confirmation_after_readiness_not_completion():
    workflow = SupportWorkflowAgent(_FakeAgent())
    state = {
        "support_requests": [
            {
                "domain": "after_sales",
                "operation": "after_sales_transition",
                # 即使旧路由错误地标成只读，已注册 Workflow 仍拥有确认边界。
                "risk": "read_only",
            }
        ],
        "execution_plan": [
            {"tool": "track_order", "available": True},
            {"tool": "check_after_sales", "available": True},
            {"tool": "check_exchange_eligibility", "available": False},
        ],
        "required_decision_facts": [
            "order_identified",
            "service_order_identified",
            "service_order_status",
            "exchange_eligibility",
        ],
        "completion_facts": [],
        "verified_facts": {
            "track_order": {"status": "success"},
            "check_after_sales": {"status": "success"},
        },
        "decision_facts": {
            "order_identified": True,
            "service_order_identified": "AS-1",
            "service_order_status": "UNDER_REVIEW",
            "exchange_eligibility": True,
        },
        "plan_validation": {},
    }
    result = workflow._evaluate_progress(state, LoopResult(answer="待确认"))

    assert result["goal_status"] == "blocked"
    assert result["next_action"] == "ESCALATE_OR_EXPLAIN"
    assert result["reason"] == "capability_unavailable"
    assert result["readiness_satisfied"] is True
