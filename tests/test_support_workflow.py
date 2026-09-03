"""SupportWorkflow 的最小编排测试，不连接真实模型或数据库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.engines.loop import LoopResult, StepResult
from agent.engines.support_workflow import SupportWorkflowAgent
from agent.support_control import extract_decision_context, extract_decision_facts
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


class _OperatorToolAgent(_FakeAgent):
    """Small AgentLoop double for reads not already completed by Control Plane."""

    def __init__(self, registry, calls: list[tuple[str, dict]], answer: str = "已核验"):
        super().__init__()
        self.registry = registry
        self.tool_plan = calls
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
        if "本轮 Control Plane 已核验的当前业务事实" in system_prompt_extra:
            return LoopResult(answer=self.answer, total_steps=1)
        verified_facts = {}
        decision_facts = {}
        decision_contexts = []
        steps = []
        for index, (name, arguments) in enumerate(self.tool_plan, start=1):
            tool_result = await self.registry.execute(name, tool_context=tool_context, **arguments)
            if tool_result.is_success:
                data = dict(tool_result.data)
                verified_facts[name] = {"status": "success", "data": data}
                decision_facts.update(tool_result.decision_facts or extract_decision_facts(name, data))
                contexts = tool_result.decision_contexts
                if not contexts:
                    derived = extract_decision_context(name, data, requested_order_id=arguments.get("order_id"))
                    contexts = [derived] if derived else []
                decision_contexts.extend(contexts)
                observation = f"[{name} 结果] ok"
            else:
                verified_facts[name] = {"status": tool_result.status, "error": tool_result.error}
                observation = f"[{name} 错误] {tool_result.error}"
            steps.append(
                StepResult(
                    step=index,
                    tool_calls=[SimpleNamespace(name=name)],
                    observation=observation,
                )
            )
        return LoopResult(
            answer=self.answer,
            steps=steps,
            total_steps=len(steps),
            verified_facts=verified_facts,
            decision_facts=decision_facts,
            decision_contexts=decision_contexts,
        )


class _OrderListRecoveryRegistry:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append((name, kwargs.copy()))
        assert name == "track_order"
        return ToolResult(
            name=name,
            status="success",
            data={
                "count": 2,
                "orders": [
                    {"order_id": "SO-SSD", "status": "PAID", "delivery_state": "NOT_SHIPPED"},
                    {"order_id": "SO-RAM", "status": "PAID", "delivery_state": "NOT_SHIPPED"},
                ],
                "multiple_results": True,
                "selection_required": False,
            },
        )


class _NoToolAgent(_FakeAgent):
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
        return LoopResult(answer="我会继续核验。", total_steps=1)


class _SubjectResolverLLM:
    def __init__(self, content: str):
        self.content = content
        self.calls: list[tuple[list[dict], dict]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


def test_operator_subject_identity_uses_canonical_checkout_line_metadata():
    from agent.support_subjects import match_subject_identity_choices

    choices = [
        {
            "order_id": "SO-SSD",
            # The summary deliberately resembles an old MIN(product_name)
            # projection.  Resolver must use true checkout line metadata.
            "product_name": "内存",
            "items": [
                {
                    "product_name": "三星 SSD 1TB",
                    "catalog_category": "components",
                    "component_category": "solid_state_drive",
                }
            ],
        },
        {
            "order_id": "SO-RAM",
            "product_name": "内存",
            "items": [
                {
                    "product_name": "金士顿内存条",
                    "catalog_category": "components",
                    "component_category": "memory",
                }
            ],
        },
    ]

    assert [choice["order_id"] for choice in match_subject_identity_choices("我想退款刚买的ssd", choices)] == [
        "SO-SSD"
    ]
    assert [choice["order_id"] for choice in match_subject_identity_choices("我想把内存退了", choices)] == [
        "SO-RAM"
    ]


def test_subject_identity_uses_top_level_catalog_category_alias_when_present():
    from agent.support_subjects import match_subject_identity_choices

    choices = [
        {"order_id": "SO-LAPTOP", "product_name": "Acer 传奇", "catalog_category": "laptops"},
        {"order_id": "SO-PHONE", "product_name": "Pixel", "catalog_category": "phones"},
    ]

    assert [choice["order_id"] for choice in match_subject_identity_choices("我想退电脑", choices)] == ["SO-LAPTOP"]


def test_pending_order_choices_keeps_all_bounded_order_candidates():
    facts = {
        "track_order": {
            "status": "success",
            "data": {
                "orders": [
                    {"order_id": f"SO-{index}", "items": [{"product_name": f"商品{index}"}]}
                    for index in range(1, 5)
                ]
            },
        }
    }

    choices = SupportWorkflowAgent._pending_order_choices(facts)

    assert [choice["order_id"] for choice in choices] == ["SO-1", "SO-2", "SO-3", "SO-4"]


@pytest.mark.asyncio
async def test_unique_subject_resolution_projects_selected_production_order_row():
    """A multi-order discovery binds the selected row's real order facts."""
    workflow = SupportWorkflowAgent(_FakeAgent())
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 2,
                    "multiple_results": True,
                    "selection_required": False,
                    "orders": [
                        {
                            "order_id": "SO-SSD",
                            "status": "PAID",
                            "order_status": "PAID",
                            "delivery_state": "NOT_SHIPPED",
                            "payment_provider": "unionpay_test",
                            "order_cancel_supported": False,
                            "items": [
                                {
                                    "product_name": "Acer NVME",
                                    "catalog_category": "components",
                                    "catalog_product_id": "ssd-1",
                                    "component_category": "solid_state_drive",
                                }
                            ],
                        },
                        {
                            "order_id": "SO-RAM",
                            "status": "PAID",
                            "order_status": "PAID",
                            "delivery_state": "NOT_SHIPPED",
                            "payment_provider": "alipay_sandbox",
                            "order_cancel_supported": False,
                            "items": [
                                {
                                    "product_name": "Acer Memory",
                                    "catalog_category": "components",
                                    "catalog_product_id": "ram-1",
                                    "component_category": "memory",
                                }
                            ],
                        },
                    ],
                },
            }
        },
    )

    state = {
        "query": "我想退 SSD",
        "result": result,
        "verified_facts": {},
        "decision_facts": {},
        "decision_contexts": [],
        "selected_subjects": {},
        "operator_observations": [],
        "already_attempted_tools": [],
    }

    updated = await workflow._absorb_observation(state)

    assert updated["selected_subjects"] == {"order_id": "SO-SSD"}
    assert updated["decision_facts"]["order_status"] == "PAID"
    assert updated["decision_facts"]["shipping_status"] == "NOT_SHIPPED"
    assert updated["decision_facts"]["payment_provider"] == "unionpay_test"
    assert updated["decision_facts"]["order_cancel_supported"] is False
    assert [context["subject_id"] for context in updated["decision_contexts"]] == ["SO-SSD"]
    assert result.decision_facts["order_status"] == "PAID"
    assert [context["subject_id"] for context in result.decision_contexts] == ["SO-SSD"]


@pytest.mark.asyncio
async def test_unknown_semantic_resolution_keeps_deterministically_narrowed_choices():
    workflow = SupportWorkflowAgent(_FakeAgent())
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 4,
                    "multiple_results": True,
                    "orders": [
                        {
                            "order_id": "SO-KC",
                            "status": "PAID",
                            "recency_rank": 1,
                            "items": [{"product_name": "金士顿 KC3000 NVMe SSD"}],
                        },
                        {
                            "order_id": "SO-NV2",
                            "status": "PAID",
                            "recency_rank": 5,
                            "items": [{"product_name": "金士顿 NV2 NVMe SSD"}],
                        },
                        {
                            "order_id": "SO-IP16",
                            "status": "PAID",
                            "items": [{"product_name": "苹果 iPhone 16"}],
                        },
                        {
                            "order_id": "SO-ACER",
                            "status": "PAID",
                            "items": [{"product_name": "Acer 笔记本"}],
                        },
                    ],
                },
            }
        },
    )
    state = {
        "query": "金士顿那个",
        "support_requests": [{"domain": "refund", "operation": "request"}],
        "result": result,
        "verified_facts": {},
        "decision_facts": {},
        "decision_contexts": [],
        "selected_subjects": {},
        "operator_observations": [],
        "already_attempted_tools": [],
    }

    updated = await workflow._absorb_observation(state)

    assert updated["subject_resolution_status"] == "ambiguous"
    assert [item["order_id"] for item in updated["subject_choices"]] == ["SO-KC", "SO-NV2"]
    state.update(updated)
    progress = workflow._evaluate_progress(state, result)
    assert progress["next_action"] == "ASK_CHOICE"
    assert [item["order_id"] for item in progress["pending_choices"]] == ["SO-KC", "SO-NV2"]


@pytest.mark.asyncio
async def test_explicit_recency_can_resolve_within_identity_matched_subset():
    agent = _FakeAgent()
    resolver = _SubjectResolverLLM(
        '{"status":"resolved","selected_ref":"order_candidate_1","ambiguous_refs":[]}'
    )
    agent.llm = resolver
    workflow = SupportWorkflowAgent(agent)
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 3,
                    "multiple_results": True,
                    "orders": [
                        {
                            "order_id": "SO-KC",
                            "status": "PAID",
                            "order_status": "PAID",
                            "delivery_state": "NOT_SHIPPED",
                            "recency_rank": 1,
                            "items": [{"product_name": "金士顿 KC3000 NVMe SSD"}],
                        },
                        {
                            "order_id": "SO-NV2",
                            "status": "PAID",
                            "order_status": "PAID",
                            "delivery_state": "NOT_SHIPPED",
                            "recency_rank": 6,
                            "items": [{"product_name": "金士顿 NV2 NVMe SSD"}],
                        },
                        {
                            "order_id": "SO-IP16",
                            "status": "PAID",
                            "items": [{"product_name": "苹果 iPhone 16"}],
                        },
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": "我想把刚下单的金士顿退了 可以吗",
            "support_requests": [{"domain": "refund", "operation": "request"}],
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated["selected_subjects"] == {"order_id": "SO-KC"}
    assert updated["subject_resolution_status"] == "resolved"
    assert resolver.calls


@pytest.mark.asyncio
async def test_unique_cooling_subject_resolution_uses_component_metadata():
    """Real checkout component rows resolve cooling products without name special cases."""
    workflow = SupportWorkflowAgent(_FakeAgent())
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 3,
                    "multiple_results": True,
                    "orders": [
                        {
                            "order_id": "SO-COOLER",
                            "status": "PAID",
                            "order_status": "PAID",
                            "delivery_state": "NOT_SHIPPED",
                            "payment_provider": "unionpay_test",
                            "order_cancel_supported": False,
                            "items": [
                                {
                                    "product_name": "Tt 飓风",
                                    "catalog_category": "components",
                                    "catalog_product_id": "cooler-1",
                                    "component_category": "cooling_product",
                                }
                            ],
                        },
                        {
                            "order_id": "SO-LAPTOP",
                            "status": "PAID",
                            "items": [{"product_name": "Acer 笔记本", "catalog_category": "laptops"}],
                        },
                        {
                            "order_id": "SO-MEMORY",
                            "status": "PAID",
                            "items": [
                                {
                                    "product_name": "Acer 内存",
                                    "catalog_category": "components",
                                    "component_category": "memory",
                                }
                            ],
                        },
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": "帮我看看我新买的散热器什么时候能到货",
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated["selected_subjects"] == {"order_id": "SO-COOLER"}
    assert updated["decision_facts"]["order_status"] == "PAID"
    assert updated["decision_facts"]["shipping_status"] == "NOT_SHIPPED"
    assert updated["decision_facts"]["payment_provider"] == "unionpay_test"
    assert updated["decision_facts"]["order_cancel_supported"] is False
    assert [context["subject_id"] for context in updated["decision_contexts"]] == ["SO-COOLER"]
    assert result.decision_facts["shipping_status"] == "NOT_SHIPPED"


def test_single_order_listing_is_a_completed_order_list_fact():
    facts = extract_decision_facts(
        "track_order",
        {
            "count": 1,
            "orders": [{"order_id": "SO-ONE", "status": "PAID", "delivery_state": "NOT_SHIPPED"}],
            "selection_required": False,
        },
    )

    assert facts["orders_listed"] is True
    assert facts["order_count"] == 1
    assert facts["order_identified"] is True


@pytest.mark.asyncio
async def test_order_list_has_bounded_read_recovery_when_operator_forgets_discovery():
    registry = _OrderListRecoveryRegistry()
    agent = _NoToolAgent()
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "我现在有哪些订单",
        support_requests=[{"domain": "order", "operation": "list"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == [("track_order", {})]
    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["decision_facts"]["orders_listed"] is True


@pytest.mark.asyncio
async def test_subject_not_bound_failure_recovers_customer_order_discovery():
    """An internal subject guard failure must recover discovery, not block the customer."""
    workflow = SupportWorkflowAgent(_NoToolAgent())
    state = {
        "query": "我想退款刚买的 SSD",
        "support_requests": [{"domain": "refund", "operation": "status"}],
        "selected_subjects": {},
        "decision_facts": {},
        "decision_contexts": [],
        "verified_facts": {
            "query_refund_status": {"status": "error", "error": "subject_not_bound"},
        },
        "result": LoopResult(
            answer="我会查询退款状态。",
            verified_facts={
                "query_refund_status": {"status": "error", "error": "subject_not_bound"},
            },
            steps=[
                StepResult(
                    step=1,
                    tool_calls=[SimpleNamespace(name="query_refund_status", arguments={"order_id": "SO-MODEL"})],
                    observation="[query_refund_status 错误] subject_not_bound",
                )
            ],
        ),
        "replan_count": 1,
    }
    state.update(await workflow._prepare_case(state))

    progress = workflow._evaluate_progress(state, state["result"])

    assert progress["goal_status"] == "unresolved"
    assert progress["reason"] == "needs_subject_discovery"
    assert progress["next_action"] == "LOOKUP"
    state["result"].workflow_progress = progress
    assert workflow._safe_recovery_request(state, state["result"]) == ("track_order", {})


@pytest.mark.asyncio
async def test_bounded_recovery_uses_server_bound_subject_context():
    class _RecoveryRegistry:
        def __init__(self):
            self.context = None
            self.arguments = None

        async def execute(self, name, *, tool_context=None, **kwargs):
            from agent.tools_registry import ToolResult

            assert name == "check_refund_eligibility"
            self.context = tool_context
            self.arguments = kwargs
            return ToolResult(
                name=name,
                status="success",
                data={"order_id": "SO1", "refund_eligibility": True},
            )

    registry = _RecoveryRegistry()
    workflow = SupportWorkflowAgent(_NoToolAgent(), registry)
    state = {
        "query": "我想退款",
        "support_requests": [{"domain": "refund", "operation": "request"}],
        "selected_subjects": {"order_id": "SO1"},
        "tool_context": ToolContext(
            user_id=7,
            role="customer",
            allowed_tools=frozenset({"track_order", "query_refund_status", "check_refund_eligibility"}),
        ),
        "decision_facts": {"order_identified": True, "refund_status": "NOT_FOUND"},
        "decision_contexts": [],
        "verified_facts": {},
        "already_attempted_tools": ["track_order", "query_refund_status"],
        "operator_observations": [],
        "recovery_count": 0,
        "result": LoopResult(
            answer="还需要核验退款资格。",
            decision_facts={"order_identified": True, "refund_status": "NOT_FOUND"},
            workflow_progress={
                "goal_status": "unresolved",
                "missing_facts": ["refund_eligibility", "refund_entry"],
            },
        ),
    }
    state.update(await workflow._prepare_case(state))

    updated = await workflow._operator_recovery(state)

    assert registry.arguments == {"order_id": "SO1"}
    assert registry.context is not None
    assert registry.context.selected_order_id == "SO1"
    assert registry.context.require_bound_subject is True
    assert updated["decision_facts"]["refund_eligibility"] is True


def test_refund_operator_prompt_exposes_affordance_map_not_fixed_step_instruction():
    from agent.support_control import build_policy_envelope

    envelope = build_policy_envelope([{"domain": "refund", "operation": "request"}])
    affordances = {item["fact"]: item for item in envelope.fact_affordances}
    assert affordances["order_identified"]["capability"] == "track_order"
    assert affordances["refund_status"]["preconditions"] == ["order_identified"]
    assert affordances["refund_entry"]["server_controlled"] is True
    assert "generate_refund_entry" not in envelope.allowed_tools


def test_server_bound_subject_satisfies_order_identified_without_subject_context():
    result = LoopResult(
        answer="已查询",
        decision_facts={"orders_listed": True, "order_count": 4},
    )
    state = {
        "selected_subjects": {"order_id": "SO-SSD"},
        "decision_contexts": [],
        "decision_facts": {"orders_listed": True, "order_count": 4},
    }

    facts = SupportWorkflowAgent._bound_decision_facts(state, result)

    assert facts == {"order_identified": True}


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
        self.contexts: list[ToolContext | None] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        self.arguments.append((name, kwargs.copy()))
        self.contexts.append(tool_context)
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


class _PendingPaymentRegistry:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append(name)
        if name == "track_order":
            return ToolResult(
                name=name,
                status="success",
                data={
                    "order_id": "SO-PENDING",
                    "status": "PENDING_PAYMENT",
                    "delivery_state": "NOT_SHIPPED",
                    "payment_provider": "alipay_sandbox",
                    "order_cancel_supported": True,
                },
            )
        raise AssertionError(f"pending-payment refund path must not call {name}")


class _PaymentRegistry:
    def __init__(self, payment_result: str):
        self.payment_result = payment_result
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, name, *, tool_context=None, **kwargs):
        from agent.tools_registry import ToolResult

        self.calls.append((name, kwargs.copy()))
        assert name == "check_payment_status"
        return ToolResult(
            name=name,
            status="success",
            data={
                "payment_result": self.payment_result,
                "order": {"order_id": "SO-PAYMENT", "status": "PENDING_PAYMENT"},
            },
        )


@pytest.mark.asyncio
async def test_support_workflow_pre_reads_safe_mandatory_current_facts_before_operator():
    agent = _FakeAgent()
    registry = _FactRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "我的订单现在什么状态",
        support_requests=[{"domain": "order", "operation": "status"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == ["track_order"]
    assert result.verified_facts["track_order"]["status"] == "success"
    assert "允许的只读工具" in agent.calls[0]["system_prompt_extra"]
    assert "本轮 Control Plane 已核验的当前业务事实" in agent.calls[0]["system_prompt_extra"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_payload",
    [
        {"domain": "payment", "operation": "check_payment_status"},
        {"domain": "delivery", "operation": "track_order"},
        {"domain": "after_sales", "operation": "check_after_sales"},
    ],
)
async def test_phase_a_eager_read_rollout_does_not_expand_other_workflow_domains(request_payload):
    registry = SimpleNamespace(execute=AsyncMock())
    workflow = SupportWorkflowAgent(_FakeAgent(), registry)
    state = {
        "support_requests": [request_payload],
        "system_prompt_extra": "",
        "case_context": "",
        "selected_subjects": {},
        "previous_subjects": {},
        "tool_context": ToolContext(user_id=7, role="customer"),
    }
    prepared = await workflow._prepare_case(state)
    update = await workflow._read_facts({**state, **prepared})

    registry.execute.assert_not_awaited()
    assert update["verified_facts"] == {}


@pytest.mark.asyncio
async def test_persisted_case_subject_is_only_continuity_context_on_natural_language_turn():
    prepared = await SupportWorkflowAgent._prepare_case(
        {
            "support_requests": [{"domain": "refund", "operation": "status"}],
            "system_prompt_extra": "",
            "case_context": '{"selected_subjects":{"order_id":"SO-IP"}}',
            "selected_subjects": {},
            "previous_subjects": {"order_id": "SO-IP"},
        }
    )

    prompt = prepared["workflow_prompt"]
    assert "上一轮已验证的身份连续性上下文" in prompt
    assert "OrderSubjectResolver" in prompt


@pytest.mark.asyncio
async def test_expected_arrival_exposes_unavailable_eta_after_status_is_read():
    registry = _RefundFactRegistry()
    agent = _OperatorToolAgent(registry, [("track_order", {}), ("query_refund_status", {"order_id": "SO1"})])
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "退款什么时候到账",
        support_requests=[{"domain": "refund", "operation": "expected_arrival"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == ["track_order", "query_refund_status"]
    assert result.workflow_progress["goal_status"] == "resolved_with_limitation"
    assert result.workflow_progress["control_state"] == "RESOLVED_WITH_LIMITATION"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    assert result.workflow_progress["unavailable_capabilities"] == ["query_refund_expected_arrival"]
    assert result.workflow_progress["readiness_satisfied"] is False
    assert "等待商家核验" not in result.answer or "具体到账时间" in result.answer


@pytest.mark.asyncio
async def test_operator_may_explain_previous_verified_refund_fact_without_pre_reading_it():
    agent = _AnswerAgent(
        "根据刚才查询结果，这笔退款当时处于等待商家核验状态。这表示申请已经进入处理环节，"
        "但当前不能据此确认具体什么时候完成或到账。"
    )
    workflow = SupportWorkflowAgent(agent)

    result = await workflow.run(
        "这是什么意思？",
        support_requests=[{"domain": "refund", "operation": "expected_arrival"}],
        selected_subjects={"order_id": "SO1"},
        allow_historical_explanation=True,
        historical_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO1",
                "provenance": "historical",
                "source": "query_refund_status",
                "facts": {"order_identified": True, "refund_status": "PENDING_MERCHANT_REVIEW"},
            }
        ],
    )

    assert result.workflow_progress["goal_status"] == "resolved_with_limitation"
    assert result.workflow_progress["reason"] == "historical_fact_explanation"
    assert result.verified_facts == {}
    assert "根据刚才查询结果" in result.answer
    assert "具体什么时候完成或到账" in result.answer


@pytest.mark.asyncio
async def test_refund_status_answer_is_rendered_from_decision_facts():
    registry = _RefundFactRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [("track_order", {}), ("query_refund_status", {"order_id": "SO1"})],
            "退款已经到账支付宝。",
        ),
        registry,
    )

    result = await workflow.run(
        "这笔退款现在什么状态？",
        support_requests=[{"domain": "refund", "operation": "status"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert result.workflow_progress["goal_status"] == "resolved"
    assert "处理中" in result.answer
    assert "到账" not in result.answer
    assert "支付宝" not in result.answer


@pytest.mark.asyncio
async def test_selected_subject_is_bound_to_pre_read_order_tool_arguments():
    registry = _RecordingRefundFactRegistry()
    agent = _OperatorToolAgent(
        registry,
        [("track_order", {"order_id": "SO1"}), ("query_refund_status", {"order_id": "SO1"})],
    )
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
    assert registry.contexts
    assert all(context.selected_order_id == "SO1" for context in registry.contexts if context is not None)
    assert agent.calls[0]["tool_context"].selected_order_id == "SO1"


@pytest.mark.asyncio
@pytest.mark.parametrize("eligible", [True, False])
async def test_direct_refund_eligibility_reads_capability_without_refund_status_guard(eligible):
    registry = _DirectEligibilityRegistry(eligible)
    agent = _OperatorToolAgent(registry, [("track_order", {}), ("check_refund_eligibility", {"order_id": "SO1"})])
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "这个订单现在还能退吗",
        support_requests=[{"domain": "refund", "operation": "eligibility"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == ["track_order", "check_refund_eligibility"]
    assert result.workflow_progress["goal_status"] == ("resolved" if eligible else "resolved_with_explanation")
    assert result.workflow_progress["decision_facts"]["refund_eligibility"] is eligible


@pytest.mark.asyncio
async def test_pending_payment_refund_request_stops_before_refund_lookup_or_eligibility():
    registry = _PendingPaymentRegistry()
    workflow = SupportWorkflowAgent(_OperatorToolAgent(registry, [("track_order", {})], "我会替你退款"), registry)

    result = await workflow.run(
        "我想退这台电脑",
        support_requests=[{"domain": "refund", "operation": "request"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == ["track_order"]
    assert result.workflow_progress["goal_status"] == "resolved_with_explanation"
    assert result.workflow_progress["resolution_type"] == "SELF_SERVICE_ORDER_CANCEL"
    assert "没有已支付款项需要退款" in result.answer


@pytest.mark.asyncio
async def test_payment_status_unavailable_is_a_resolved_unknown_fact_not_a_failed_tool():
    registry = _PaymentRegistry("PAYMENT_STATUS_UNAVAILABLE")
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(registry, [("check_payment_status", {})], "余额或网络有问题"),
        registry,
    )

    result = await workflow.run(
        "支付失败怎么回事",
        support_requests=[{"domain": "payment", "operation": "check_payment_status"}],
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert registry.calls == [("check_payment_status", {})]
    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["failed_tools"] == []
    assert "支付渠道当前暂时无法确认" in result.answer
    assert "余额" not in result.answer


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


def test_declared_unavailable_capability_degrades_when_workflow_declares_safe_partial_answer():
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

    assert progress["goal_status"] == "resolved_with_limitation"
    assert progress["control_state"] == "RESOLVED_WITH_LIMITATION"
    assert progress["reason"] == "capability_unavailable"
    assert progress["next_actor"] == "NONE"


@pytest.mark.asyncio
async def test_refund_request_delivers_self_service_entry_after_eligibility():
    registry = _RefundRequestRegistry()
    agent = _OperatorToolAgent(
        registry,
        [
            ("track_order", {}),
            ("query_refund_status", {"order_id": "SO1"}),
            ("check_refund_eligibility", {"order_id": "SO1"}),
        ],
    )
    workflow = SupportWorkflowAgent(agent, registry)

    with pytest.MonkeyPatch.context() as monkeypatch:

        async def generate_entry(*, customer_user_id: int, order_no: str, eligibility_already_verified: bool) -> str:
            assert customer_user_id == 7
            assert order_no == "SO1"
            assert eligibility_already_verified is True
            return "?page=orders&refund_order=SO1"

        monkeypatch.setattr("agent.engines.support_workflow.generate_customer_refund_entry", generate_entry)
        with (
            patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
            patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
        ):
            result = await workflow.run(
                "我要申请退款",
                support_requests=[{"domain": "refund", "operation": "request"}],
                tool_context=ToolContext(user_id=7, role="customer"),
            )

    assert registry.calls == ["track_order", "query_refund_status", "check_refund_eligibility"]
    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["next_actor"] == "NONE"
    assert result.workflow_progress["resolution_type"] == "SELF_SERVICE_HANDOFF"
    assert result.workflow_progress["decision_facts"]["refund_eligibility"] is True
    assert result.answer == "已核验"
    assert result.response_control["mode"] == "SELF_SERVICE_HANDOFF"
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_generates_entry_from_current_eligibility_fact():
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [
                ("track_order", {}),
                ("query_refund_status", {"order_id": "SO1"}),
                ("check_refund_eligibility", {"order_id": "SO1"}),
            ],
        ),
        registry,
    )
    with patch(
        "agent.engines.support_workflow.generate_customer_refund_entry",
        new=AsyncMock(return_value="?page=orders&refund_order=SO1"),
    ) as generate_entry:
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.workflow_progress["resolution_type"] == "SELF_SERVICE_HANDOFF"
    generate_entry.assert_awaited_once_with(
        customer_user_id=7,
        order_no="SO1",
        eligibility_already_verified=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_answer",
    [
        "确认后我将为您提交退款申请。",
        "退款入口已生成，确认后我帮您申请退款。\n?page=orders&refund_order=SO1",
    ],
)
async def test_self_service_handoff_replaces_unsafe_llm_answer(unsafe_answer):
    registry = _RefundRequestRegistry()
    agent = _OperatorToolAgent(
        registry,
        [
            ("track_order", {}),
            ("query_refund_status", {"order_id": "SO1"}),
            ("check_refund_eligibility", {"order_id": "SO1"}),
        ],
        unsafe_answer,
    )
    workflow = SupportWorkflowAgent(agent, registry)

    with pytest.MonkeyPatch.context() as monkeypatch:

        async def generate_entry(*, customer_user_id: int, order_no: str, eligibility_already_verified: bool) -> str:
            assert eligibility_already_verified is True
            return "?page=orders&refund_order=SO1"

        monkeypatch.setattr("agent.engines.support_workflow.generate_customer_refund_entry", generate_entry)
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert "提交退款申请" not in result.answer
    assert "客服不会代为创建、提交或确认退款" in result.answer
    assert "?page=orders&refund_order=SO1" in result.answer


@pytest.mark.asyncio
async def test_self_service_handoff_rejects_untrusted_entry_and_blocks():
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [
                ("track_order", {}),
                ("query_refund_status", {"order_id": "SO1"}),
                ("check_refund_eligibility", {"order_id": "SO1"}),
            ],
            "可以退款。",
        ),
        registry,
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "agent.engines.support_workflow.generate_customer_refund_entry",
            AsyncMock(return_value="https://fake-refund.example.com"),
        )
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    assert result.workflow_progress["resolution_type"] == ""
    assert "https://fake-refund.example.com" not in result.answer
    assert "refund_entry" not in result.workflow_progress["decision_facts"]


@pytest.mark.asyncio
async def test_refund_request_ineligible_is_explained_without_entry_or_write():
    registry = _RefundIneligibleRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [
                ("track_order", {}),
                ("query_refund_status", {"order_id": "SO1"}),
                ("check_refund_eligibility", {"order_id": "SO1"}),
            ],
        ),
        registry,
    )

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
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
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [
                ("track_order", {}),
                ("query_refund_status", {"order_id": "SO1"}),
                ("check_refund_eligibility", {"order_id": "SO1"}),
            ],
        ),
        registry,
    )

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["reason"] == "fact_tool_failed"
    assert result.workflow_progress["next_actor"] == "NONE"
    generate_entry.assert_not_awaited()
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_entry_failure_is_a_safe_capability_limit():
    registry = _RefundRequestRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(
            registry,
            [
                ("track_order", {}),
                ("query_refund_status", {"order_id": "SO1"}),
                ("check_refund_eligibility", {"order_id": "SO1"}),
            ],
        ),
        registry,
    )

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock(return_value=None)),
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    assert result.workflow_progress["goal_status"] == "blocked"
    assert result.workflow_progress["next_actor"] == "NONE"
    assert result.workflow_progress["reason"] == "capability_unavailable"
    request_refund.assert_not_awaited()
    confirm_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_request_with_existing_refund_explains_status_without_new_entry():
    registry = _RefundFactRegistry()
    workflow = SupportWorkflowAgent(
        _OperatorToolAgent(registry, [("track_order", {}), ("query_refund_status", {"order_id": "SO1"})]),
        registry,
    )

    with (
        patch("agent.engines.support_workflow.generate_customer_refund_entry", new=AsyncMock()) as generate_entry,
        patch("service.checkout_refund_service.request_customer_refund", new=AsyncMock()) as request_refund,
        patch("service.checkout_refund_service.confirm_customer_refund", new=AsyncMock()) as confirm_refund,
    ):
        result = await workflow.run(
            "我要申请退款",
            support_requests=[{"domain": "refund", "operation": "request"}],
            tool_context=ToolContext(user_id=7, role="customer"),
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
    assert result.workflow_progress["next_action"] == "EXPLAIN_LIMITATION_OR_HANDOFF"
    assert result.workflow_progress["required_tools"] == ["track_order", "check_after_sales"]
    assert "本轮业务契约（服务端强制，不是工具执行顺序）" in agent.calls[0]["system_prompt_extra"]
    assert '允许的只读工具：["track_order", "check_after_sales"]' in agent.calls[0]["system_prompt_extra"]


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
    assert result["next_action"] == "EXPLAIN_LIMITATION_OR_HANDOFF"
    assert result["reason"] == "capability_unavailable"
    assert result["readiness_satisfied"] is True

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "我还想处理另一部 iPhone 的退款",
        "剩下那部 iPhone 也申请退款",
        "改成处理别的那部 iPhone",
        "不是刚才那台 iPhone，换另一部处理",
    ],
)
async def test_changed_subject_excludes_previous_verified_order_before_resolution(query):
    agent = _FakeAgent()
    agent.llm = _SubjectResolverLLM(
        '{"status":"resolved","selected_ref":"order_candidate_2","ambiguous_refs":[]}'
    )
    workflow = SupportWorkflowAgent(agent)
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 3,
                    "multiple_results": True,
                    "orders": [
                        {
                            "order_id": "SO-IP16",
                            "status": "PAID",
                            "items": [{"product_name": "苹果 iPhone 16"}],
                        },
                        {
                            "order_id": "SO-IP17",
                            "status": "PAID",
                            "items": [{"product_name": "苹果 iPhone 17"}],
                        },
                        {
                            "order_id": "SO-ACER",
                            "status": "PAID",
                            "items": [{"product_name": "Acer 笔记本"}],
                        },
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": query,
            "support_requests": [{"domain": "refund", "operation": "request"}],
            # Router relation is deliberately non-authoritative.  Even a stale
            # "same" label cannot override the current natural-language switch.
            "subject_relation": "same",
            "previous_subjects": {"order_id": "SO-IP16"},
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated["selected_subjects"] == {"order_id": "SO-IP17"}
    assert updated["subject_resolution_status"] == "resolved"


@pytest.mark.asyncio
async def test_unique_new_identity_falls_back_to_server_candidate_when_resolver_is_unknown():
    """A resolver miss cannot erase a uniquely proven switch to another owned order."""
    agent = _FakeAgent()
    agent.llm = _SubjectResolverLLM('{"status":"unknown","selected_ref":"","ambiguous_refs":[]}')
    workflow = SupportWorkflowAgent(agent)
    result = LoopResult(
        answer="已查询订单",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 3,
                    "multiple_results": True,
                    "orders": [
                        {"order_id": "SO-IP16", "status": "PAID", "items": [{"product_name": "苹果 iPhone 16"}]},
                        {"order_id": "SO-IP17", "status": "PAID", "items": [{"product_name": "苹果 iPhone 17"}]},
                        {"order_id": "SO-ACER", "status": "PAID", "items": [{"product_name": "Acer 笔记本"}]},
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": "我想把 iphone16 退了",
            "support_requests": [{"domain": "refund", "operation": "request"}],
            "previous_subjects": {"order_id": "SO-IP17"},
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated["selected_subjects"] == {"order_id": "SO-IP16"}
    assert updated["subject_resolution_status"] == "resolved"


@pytest.mark.asyncio
async def test_unique_match_to_previous_subject_does_not_override_resolver_unknown():
    """Server fallback must never turn lexical stickiness into previous-subject authority."""
    agent = _FakeAgent()
    agent.llm = _SubjectResolverLLM('{"status":"unknown","selected_ref":"","ambiguous_refs":[]}')
    workflow = SupportWorkflowAgent(agent)
    result = LoopResult(
        answer="我还不能确认是哪一笔。",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 2,
                    "multiple_results": True,
                    "orders": [
                        {"order_id": "SO-IP16", "status": "PAID", "items": [{"product_name": "苹果 iPhone 16"}]},
                        {"order_id": "SO-ACER", "status": "PAID", "items": [{"product_name": "Acer 笔记本"}]},
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": "另一部 iphone16",
            "support_requests": [{"domain": "refund", "operation": "request"}],
            "previous_subjects": {"order_id": "SO-IP16"},
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated.get("selected_subjects", {}) == {}
    assert updated["subject_resolution_status"] == "unknown"


@pytest.mark.asyncio
async def test_changed_subject_exclusion_does_not_promote_unrelated_only_alternative():
    agent = _FakeAgent()
    agent.llm = _SubjectResolverLLM('{"status":"unknown","selected_ref":"","ambiguous_refs":[]}')
    workflow = SupportWorkflowAgent(agent)
    result = LoopResult(
        answer="我还没定位到你说的那部手机。",
        verified_facts={
            "track_order": {
                "status": "success",
                "data": {
                    "count": 2,
                    "multiple_results": True,
                    "orders": [
                        {"order_id": "SO-IP16", "status": "PAID", "items": [{"product_name": "苹果 iPhone 16"}]},
                        {"order_id": "SO-ACER", "status": "PAID", "items": [{"product_name": "Acer 笔记本"}]},
                    ],
                },
            }
        },
    )

    updated = await workflow._absorb_observation(
        {
            "query": "我还想处理另一部 iPhone 的退款",
            "support_requests": [{"domain": "refund", "operation": "request"}],
            "subject_relation": "changed",
            "previous_subjects": {"order_id": "SO-IP16"},
            "result": result,
            "verified_facts": {},
            "decision_facts": {},
            "decision_contexts": [],
            "selected_subjects": {},
            "operator_observations": [],
            "already_attempted_tools": [],
        }
    )

    assert updated.get("selected_subjects", {}) == {}
    assert updated["subject_resolution_status"] == "unknown"

@pytest.mark.asyncio
async def test_structured_selected_subject_not_found_refund_status_recovers_eligibility_and_entry():
    """Server-bound choice completes refund mandatory reads without recovery."""
    registry = _RefundRequestRegistry()

    workflow = SupportWorkflowAgent(_FakeAgent(), registry)

    async def generate_entry(*, customer_user_id: int, order_no: str, eligibility_already_verified: bool) -> str:
        assert customer_user_id == 7
        assert order_no == "SO1"
        assert eligibility_already_verified is True
        return "?page=orders&refund_order=SO1"

    with patch("agent.engines.support_workflow.generate_customer_refund_entry", new=generate_entry):
        result = await workflow.run(
            "已选择订单：SO1",
            support_requests=[{"domain": "refund", "operation": "request"}],
            selected_subjects={"order_id": "SO1"},
            tool_context=ToolContext(
                user_id=7,
                role="customer",
                allowed_tools=frozenset({"query_refund_status", "check_refund_eligibility"}),
            ),
        )

    assert registry.calls == ["track_order", "query_refund_status", "check_refund_eligibility"]
    assert result.workflow_progress["decision_facts"]["refund_status"] == "NOT_FOUND"
    assert result.workflow_progress["decision_facts"]["refund_eligibility"] is True
    assert result.workflow_progress["decision_facts"]["refund_entry"] == "?page=orders&refund_order=SO1"
    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["resolution_type"] == "SELF_SERVICE_HANDOFF"
    assert result.workflow_progress["recovery_count"] == 0
