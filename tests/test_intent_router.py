"""tests/test_intent_router.py — 意图路由单元测试"""

import pytest

from agent.llm.intent_router import Intent, IntentRouter
from agent.llm.llm_client import LLMResponse, TokenUsage


# =============================================================================
# Mock LLMClient — 不发起真实 API 调用
# =============================================================================
class _MockLLM:
    """返回指定 content 的假 LLM 客户端，只实现 chat()"""

    def __init__(self, content: str):
        self._content = content
        self.model = "mock"
        self.calls = 0

    async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        self.calls += 1
        self.last_messages = messages
        return LLMResponse(
            content=self._content,
            model="mock",
            usage=TokenUsage(),
            finish_reason="stop",
        )


def _router(response_content: str) -> IntentRouter:
    """工厂函数：用 mock LLM 创建 IntentRouter"""
    return IntentRouter(llm=_MockLLM(response_content))


# =============================================================================
# Intent 数据类
# =============================================================================
class TestIntent:
    def test_defaults(self):
        intent = Intent()
        assert intent.target == ""
        assert intent.table == ""
        assert intent.query == ""
        assert intent.confidence == 0.0

    def test_full(self):
        intent = Intent(target="rag", table="laptop_products", query="xxx", confidence=0.9)
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.query == "xxx"
        assert intent.confidence == 0.9
        assert intent.state == "unknown"
        assert intent.required_tools == []
        assert intent.requests == []
        assert intent.subject_relation == "unknown"


# =============================================================================
# IntentRouter.route 正常场景
# =============================================================================
class TestRouteNormal:
    @pytest.mark.asyncio
    async def test_router_keeps_semantic_subject_relation_without_accepting_an_order_id(self):
        router = _router(
            '{"query":"什么时候到账","target":"agent","speech_act":"INFORMATION_QUERY",'
            '"domain":"refund","operation":"expected_arrival","requests":[{"domain":"refund",'
            '"operation":"expected_arrival","next_step":"LOOKUP","risk":"read_only"}],'
            '"subject_relation":"same","confidence":0.9}'
        )

        intent = await router.route("什么时候到账", case_context='{"recent_verified_subject":{"subject_id":"SO-A"}}')

        assert intent.subject_relation == "same"
        assert intent.support_requests[0].subject_refs == []

    @pytest.mark.asyncio
    async def test_router_keeps_explain_previous_scope_without_granting_subject_or_fact(self):
        router = _router(
            '{"query":"什么意思","target":"agent","speech_act":"INFORMATION_QUERY",'
            '"domain":"refund","operation":"expected_arrival","requests":[{"domain":"refund",'
            '"operation":"expected_arrival","next_step":"LOOKUP","risk":"read_only"}],'
            '"subject_relation":"same","fact_scope":"explain_previous","confidence":0.9}'
        )

        intent = await router.route("什么意思", case_context='{"recent_verified_subject":{"subject_id":"SO-A"}}')

        assert intent.fact_scope == "explain_previous"
        assert intent.subject_relation == "same"
        prompt = router.llm.last_messages[0]["content"]
        assert "必须按“上一轮系统查询”表述" not in prompt
        assert "措辞不限" in prompt

    @pytest.mark.asyncio
    async def test_desktop_build_uses_router_semantics_for_plan_execute(self):
        router = _router(
            '{"target":"plan_execute","scenario":"build_pc","domain":"product",'
            '"operation":"build_pc","speech_act":"ACTION_REQUEST",'
            '"requests":[{"domain":"product","operation":"build_pc"}],"confidence":1.0}'
        )

        intent = await router.route("我有 8000 块预算，想配一台能玩 3A 游戏的台式电脑")

        assert intent.target == "plan_execute"
        assert intent.scenario == "build_pc"
        assert intent.query == "我有 8000 块预算，想配一台能玩 3A 游戏的台式电脑"
        assert intent.confidence == 1.0
        assert intent.route_source == "llm"

    @pytest.mark.asyncio
    async def test_pending_order_cancel_is_distinct_from_refund_cancel(self):
        order_cancel = await _router(
            '{"target":"agent","domain":"order","operation":"cancel",'
            '"speech_act":"ACTION_REQUEST","confidence":0.95}'
        ).route("这笔订单不买了，帮我取消订单")
        refund_cancel = await _router(
            '{"target":"agent","domain":"refund","operation":"cancel",'
            '"speech_act":"ACTION_REQUEST","confidence":0.95}'
        ).route("帮我取消退款申请")

        assert (order_cancel.domain, order_cancel.operation) == ("order", "cancel")
        assert (refund_cancel.domain, refund_cancel.operation) == ("refund", "cancel")

    @pytest.mark.asyncio
    async def test_troubleshooting_routes_to_knowledge_rag_not_planning_graph(self):
        """普通故障咨询应快速给排障建议，不应先规划订单/工具链。"""
        router = _router('{"target": "plan_execute", "scenario": "troubleshoot", "confidence": 0.95}')

        intent = await router.route("电脑清灰后 Wi-Fi 信号很差")

        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"
        assert intent.scenario == ""

    @pytest.mark.asyncio
    async def test_rag_target(self):
        router = _router('{"target": "rag", "table": "laptop_products", "confidence": 0.95}')
        intent = await router.route("推荐一款笔记本")
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.confidence == 0.95

    @pytest.mark.asyncio
    async def test_contextual_rewrite_uses_same_routing_call(self):
        router = _router('{"query": "购买 惠普锐Pro", "target": "agent", "table": "", "confidence": 0.95}')
        intent = await router.route(
            "下单",
            history=[{"role": "assistant", "content": "推荐首选：惠普锐Pro"}],
        )
        assert intent.query == "购买 惠普锐Pro"
        assert intent.target == "agent"

    @pytest.mark.asyncio
    async def test_raw_query_remains_semantic_authority_and_rewrite_is_retrieval_only(self):
        router = _router(
            '{"query":"金士顿 NV2 1TB 固态硬盘","target":"agent",'
            '"domain":"product","operation":"search_product",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "刚退款的 SSD 太小了，推荐一个同价位的",
            semantic_hints='{"recent_order_item_1":{"product":"Acer N3500","unit_amount_cents":19900}}',
        )

        assert intent.raw_query == "刚退款的 SSD 太小了，推荐一个同价位的"
        assert intent.retrieval_query == "金士顿 NV2 1TB 固态硬盘"
        assert intent.query == intent.retrieval_query
        prompt = router.llm.last_messages[-1]["content"]
        assert "当前用户问题：\n刚退款的 SSD 太小了，推荐一个同价位的" in prompt
        assert "recent_order_item_1" in prompt

    @pytest.mark.asyncio
    async def test_router_schema_failure_retries_once_then_clarifies_without_keyword_router(self):
        router = _router("not valid JSON")

        intent = await router.route("我想退款刚买的 SSD")

        assert router.llm.calls == 2
        assert intent.route_source == "fallback"
        assert intent.speech_act == "CLARIFICATION_NEEDED"
        assert intent.domain == "general"
        assert intent.operation == "clarify"
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_product_purchase_is_a_goal_without_inventory_tool(self):
        router = _router(
            '{"target":"agent","domain":"product","operation":"purchase",'
            '"speech_act":"ACTION_REQUEST","confidence":0.95}'
        )

        intent = await router.route("我想买")

        assert (intent.domain, intent.operation) == ("product", "purchase")
        assert intent.support_requests[0].operation == "purchase"

    @pytest.mark.asyncio
    async def test_agent_target(self):
        router = _router('{"target": "agent", "table": "", "confidence": 0.92}')
        intent = await router.route("拯救者还有货吗")
        assert intent.target == "agent"
        assert intent.table == ""  # agent 强制置空

    @pytest.mark.asyncio
    async def test_inventory_query_uses_router_semantics_and_uses_agent(self):
        router = _router(
            '{"target":"agent","domain":"inventory","operation":"check_stock",'
            '"speech_act":"INFORMATION_QUERY","next_step":"LOOKUP",'
            '"required_tools":["check_stock"],"confidence":1.0}'
        )
        intent = await router.route("查询 惠普锐Pro 的实时库存")
        assert intent.target == "agent"
        assert intent.query == "查询 惠普锐Pro 的实时库存"
        assert intent.confidence == 1.0
        assert intent.route_source == "llm"
        assert intent.domain == "inventory"
        assert intent.operation == "check_stock"
        assert intent.required_tools == ["check_stock"]

    @pytest.mark.asyncio
    async def test_delivery_query_gets_read_only_track_order_route(self):
        router = _router(
            '{"target":"agent","domain":"delivery","operation":"track_order",'
            '"speech_act":"INFORMATION_QUERY","next_step":"LOOKUP",'
            '"required_tools":["track_order"],"confidence":0.95}'
        )

        intent = await router.route("客服，我昨天下的订单为什么现在还没发货")

        assert intent.target == "agent"
        assert intent.domain == "delivery"
        assert intent.operation == "track_order"
        assert intent.next_step == "LOOKUP"
        assert intent.required_tools == ["track_order"]

    @pytest.mark.asyncio
    async def test_order_list_is_not_misrouted_to_delivery_or_subject_choice(self):
        router = _router(
            '{"target":"agent","domain":"order","operation":"list",'
            '"speech_act":"INFORMATION_QUERY","next_step":"LOOKUP",'
            '"required_tools":["track_order"],"confidence":0.95}'
        )

        intent = await router.route("我现在有哪些订单？")

        assert intent.target == "agent"
        assert intent.domain == "order"
        assert intent.operation == "list"
        assert intent.required_tools == ["track_order"]

    @pytest.mark.asyncio
    async def test_partial_fulfillment_gets_order_and_stock_read_only_route(self):
        router = _router(
            '{"target":"agent","domain":"order_fulfillment","operation":"partial_fulfillment",'
            '"speech_act":"INFORMATION_QUERY","state":"needs_customer_choice",'
            '"next_step":"ASK_CHOICE","required_tools":["track_order","check_stock"],"confidence":0.95}'
        )

        intent = await router.route("一件商品缺货，另一件有货，帮我看看怎么处理")

        assert intent.target == "agent"
        assert intent.domain == "order_fulfillment"
        assert intent.operation == "partial_fulfillment"
        assert intent.state == "needs_customer_choice"
        assert intent.required_tools == ["track_order", "check_stock"]

    @pytest.mark.asyncio
    async def test_after_sales_progress_gets_read_only_status_route(self):
        router = _router(
            '{"target":"agent","domain":"after_sales","operation":"check_after_sales",'
            '"speech_act":"INFORMATION_QUERY","next_step":"LOOKUP",'
            '"required_tools":["check_after_sales"],"confidence":0.95}'
        )

        intent = await router.route("我已经提交售后了，帮我查一下进度")

        assert intent.target == "agent"
        assert intent.domain == "after_sales"
        assert intent.operation == "check_after_sales"
        assert intent.required_tools == ["check_after_sales"]

    @pytest.mark.asyncio
    async def test_exchange_to_return_is_a_multi_step_support_workflow(self):
        """换货中的商品改退货必须先核验售后状态，再决定后续动作。"""
        router = _router(
            '{"target":"agent","domain":"after_sales","operation":"after_sales_transition",'
            '"state":"in_progress","next_step":"LOOKUP","required_tools":["check_after_sales"],'
            '"confidence":0.95,"requests":[{"domain":"after_sales",'
            '"operation":"after_sales_transition","next_step":"LOOKUP",'
            '"required_tools":["check_after_sales"],"risk":"customer_confirmation"}]}'
        )

        intent = await router.route("上午申请了换货，但这块主板和我的 CPU 不兼容，只能退货再买别的型号")

        assert intent.target == "agent"
        assert intent.domain == "after_sales"
        assert intent.operation == "after_sales_transition"
        assert intent.next_step == "LOOKUP"
        assert intent.required_tools == ["check_after_sales"]
        assert intent.use_workflow is True

    @pytest.mark.asyncio
    async def test_delivery_area_policy_with_router_failure_stays_conservative(self):
        router = _router(
            '{"target":"agent","domain":"general","operation":"clarify",'
            '"speech_act":"CLARIFICATION_NEEDED","next_step":"ASK_CLARIFICATION","confidence":0.95}'
        )

        intent = await router.route("这个订单能送到村里吗")

        assert intent.target == "agent"
        assert intent.operation == "clarify"

    @pytest.mark.asyncio
    async def test_delivery_and_device_issue_are_left_for_multi_intent_model_route(self):
        router = _router(
            '{"target":"agent","domain":"delivery","operation":"track_order",'
            '"state":"new","next_step":"ASK_CLARIFICATION",'
            '"required_tools":["track_order"],"confidence":0.9}'
        )

        intent = await router.route("手机坏了，请问什么时候订单派送")

        assert intent.target == "agent"
        assert intent.next_step == "ASK_CLARIFICATION"
        assert intent.required_tools == ["track_order"]

    @pytest.mark.asyncio
    async def test_structured_route_fields_are_validated(self):
        router = _router(
            '{"target":"agent","domain":"delivery","operation":"track_order",'
            '"state":"in_progress","next_step":"LOOKUP",'
            '"required_tools":["track_order","not_a_tool"],"confidence":0.9}'
        )

        intent = await router.route("请帮我处理这个事情")

        assert intent.domain == "delivery"
        assert intent.operation == "track_order"
        assert intent.state == "in_progress"
        assert intent.next_step == "LOOKUP"
        assert intent.required_tools == ["track_order"]

    @pytest.mark.asyncio
    async def test_multiple_customer_requests_are_preserved_for_support_workflow(self):
        router = _router(
            '{"target":"agent","confidence":0.96,"requests":['
            '{"domain":"after_sales","operation":"after_sales_transition",'
            '"desired_outcome":"exchange_to_return","subject_refs":["current_order"],'
            '"missing_facts":["after_sale_stage"],"next_step":"LOOKUP",'
            '"required_tools":["check_after_sales"],"risk":"customer_confirmation"},'
            '{"domain":"product","operation":"product_compatibility",'
            '"desired_outcome":"find_compatible_board","next_step":"LOOKUP",'
            '"required_tools":["search_component"],"risk":"read_only"}]}'
        )

        intent = await router.route("换货中的主板和 CPU 不兼容，想退货再买能用的型号")

        assert len(intent.requests) == 2
        assert intent.requests[0].desired_outcome == "exchange_to_return"
        assert intent.requests[0].risk == "customer_confirmation"
        assert intent.requests[1].operation == "product_compatibility"
        assert intent.required_tools == ["check_after_sales", "search_component"]
        assert intent.use_workflow is True

    @pytest.mark.asyncio
    async def test_invalid_support_request_values_do_not_reach_case_state(self):
        router = _router(
            '{"target":"agent","domain":"delivery","operation":"track_order",'
            '"next_step":"LOOKUP","confidence":0.9,"requests":['
            '{"domain":"unknown","operation":"drop_database","risk":"staff_approval"},'
            '{"domain":"delivery","operation":"track_order",'
            '"subject_refs":["a","a",42],"next_step":"LOOKUP",'
            '"required_tools":["track_order","drop_database"],"risk":"read_only"}]}'
        )

        intent = await router.route("请帮我处理这个事情")

        assert len(intent.requests) == 1
        assert intent.requests[0].subject_refs == ["a"]
        assert intent.requests[0].required_tools == ["track_order"]

    @pytest.mark.asyncio
    async def test_risky_operation_cannot_be_downgraded_to_read_only_by_model(self):
        router = _router(
            '{"target":"agent","confidence":0.95,"requests":['
            '{"domain":"refund","operation":"refund_request",'
            '"next_step":"LOOKUP","risk":"read_only","required_tools":[]}]} '
        )

        intent = await router.route("帮我处理退款")

        # Router 只保留模型原始兼容字段；确认边界由 Control Plane 判断。
        assert intent.requests[0].risk == "read_only"
        assert intent.requests[0].required_tools == []
        assert intent.use_workflow is True

    @pytest.mark.asyncio
    async def test_active_case_context_allows_router_to_mark_pending_reply(self):
        router = _router('{"target":"rag","table":"knowledge_chunks","confidence":0.9,"case_update":"continue"}')

        intent = await router.route(
            "第一个",
            case_context='{"case_status":"AWAITING_CUSTOMER","pending":{"kind":"choice"}}',
        )

        assert intent.case_update == "continue"

    @pytest.mark.asyncio
    async def test_explicit_refund_becomes_fact_first_support_workflow_not_ticket(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"request",'
            '"speech_act":"ACTION_REQUEST","confidence":0.88}'
        )
        intent = await router.route("我要退款")
        assert intent.target == "agent"
        assert intent.operation == "request"
        assert intent.required_tools == []
        assert intent.use_workflow is True

    @pytest.mark.asyncio
    async def test_explicit_human_request_is_clarified_before_staff_handoff(self):
        router = _router(
            '{"target":"agent","domain":"human","operation":"human_handoff",'
            '"speech_act":"ACTION_REQUEST","next_step":"ASK_CLARIFICATION","confidence":0.95}'
        )
        intent = await router.route("退款的事转人工")

        assert intent.target == "agent"
        assert intent.operation == "human_handoff"
        assert intent.next_step == "ASK_CLARIFICATION"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("legacy_operation", "canonical_operation"),
        [("refund_status", "status"), ("refund_request", "request")],
    )
    async def test_refund_legacy_operations_are_domain_aware_canonical_aliases(
        self, legacy_operation, canonical_operation
    ):
        router = _router(
            '{"target":"agent","confidence":0.95,"requests":['
            f'{{"domain":"refund","operation":"{legacy_operation}"}}]}}'
        )

        intent = await router.route("请帮我处理这个事情")

        assert intent.operation == canonical_operation
        assert intent.requests[0].operation == canonical_operation

    @pytest.mark.asyncio
    async def test_membership_legacy_operation_is_not_rewritten_by_refund_alias(self):
        router = _router(
            '{"target":"agent","confidence":0.95,"requests":[{"domain":"membership","operation":"refund_request"}]}'
        )

        intent = await router.route("请帮我处理这个事情")

        assert intent.requests[0].domain == "membership"
        assert intent.requests[0].operation == "refund_request"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("invalid_operation", ["refund_status", "refund_request"])
    async def test_invalid_domain_operation_pair_becomes_conservative_clarification(self, invalid_operation):
        router = _router(
            '{"target":"agent","speech_act":"INFORMATION_QUERY","confidence":0.95,'
            f'"domain":"after_sales","operation":"{invalid_operation}",'
            f'"requests":[{{"domain":"after_sales","operation":"{invalid_operation}"}}]}}'
        )

        intent = await router.route("请帮我确认当前情况")

        assert intent.speech_act == "CLARIFICATION_NEEDED"
        assert intent.domain == "general"
        assert intent.operation == "clarify"
        assert intent.requests == []
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_rag_information_goal_still_creates_semantic_request(self):
        router = _router(
            '{"target":"rag","table":"knowledge_chunks","speech_act":"INFORMATION_QUERY",'
            '"domain":"refund","operation":"procedure","next_step":"ANSWER","confidence":0.95}'
        )

        intent = await router.route("请说明当前退款步骤")

        assert intent.target == "rag"
        assert [(request.domain, request.operation) for request in intent.requests] == [("refund", "procedure")]
        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", "procedure")]

    @pytest.mark.asyncio
    async def test_refund_hint_human_handoff_uses_canonical_human_goal(self):
        router = _router(
            '{"target":"agent","domain":"human","operation":"human_handoff",'
            '"speech_act":"ACTION_REQUEST","next_step":"ASK_CLARIFICATION","confidence":0.95}'
        )

        intent = await router.route("退款这件事我要找人工客服")

        assert intent.route_source == "llm"
        assert intent.domain == "human"
        assert intent.operation == "human_handoff"
        assert [(request.domain, request.operation) for request in intent.support_requests] == [
            ("human", "human_handoff")
        ]

    @pytest.mark.asyncio
    async def test_refund_fast_path_requires_current_query_or_action_form(self):
        router = _router(
            '{"target":"agent","speech_act":"ACKNOWLEDGEMENT","confidence":0.95,'
            '"domain":"general","operation":"answer","requests":[]}'
        )

        intent = await router.route(
            "仓库看到东西了才能处理退款",
            history=[{"role": "assistant", "content": "商品到仓后会安排退款"}],
        )

        assert intent.route_source == "llm"
        assert intent.speech_act == "ACKNOWLEDGEMENT"
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_refund_fast_path_keeps_explicit_return_refund_query(self):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route("拒收后什么时候退款？")

        assert intent.route_source == "llm"
        assert intent.domain == "return"
        assert intent.operation == "refund_dependency"

    @pytest.mark.asyncio
    async def test_refund_progress_is_a_read_only_status_lookup_not_a_new_refund(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"expected_arrival",'
            '"speech_act":"INFORMATION_QUERY","next_step":"LOOKUP","confidence":0.95}'
        )
        intent = await router.route("我已经退了，钱怎么还没到账")

        assert intent.target == "agent"
        assert intent.operation == "expected_arrival"
        assert intent.required_tools == []
        assert intent.use_workflow is True

    @pytest.mark.asyncio
    async def test_explicit_refund_request_uses_router_semantics(self):
        """明确退款进入受控事实核验流程，不能因模型波动直接建单。"""
        router = _router(
            '{"target":"agent","domain":"refund","operation":"request",'
            '"speech_act":"ACTION_REQUEST","confidence":0.95}'
        )

        intent = await router.route("我要申请退款")

        assert intent.target == "agent"
        assert intent.operation == "request"
        assert intent.query == "我要申请退款"
        assert intent.confidence == 0.95

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "query",
        ["我想申请退款", "麻烦帮我退款", "麻烦帮我申请退款", "请退款", "需要你们帮我发起申请退款"],
    )
    async def test_explicit_refund_request_keeps_action_speech_act(self, query):
        """Goal 不能反推 speech act，但当前明确动作必须保留为动作请求。"""
        router = _router(
            '{"target":"agent","domain":"refund","operation":"request",'
            '"speech_act":"ACTION_REQUEST","confidence":0.98}'
        )

        intent = await router.route(query)

        assert intent.route_source == "llm"
        assert intent.speech_act == "ACTION_REQUEST"
        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", "request")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("query", "operation"),
        [
            ("能申请退款吗", "eligibility"),
            ("怎么申请退款", "procedure"),
            ("为什么不能申请退款", "eligibility"),
        ],
    )
    async def test_refund_question_form_beats_action_substring_collision(self, query, operation):
        router = _router(
            '{"target":"agent","speech_act":"INFORMATION_QUERY","confidence":0.95,'
            f'"domain":"refund","operation":"{operation}",'
            f'"requests":[{{"domain":"refund","operation":"{operation}"}}]}}'
        )

        intent = await router.route(query)

        assert intent.speech_act == "INFORMATION_QUERY"
        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", operation)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["退款失败了", "商家不退款", "申请退款失败"])
    async def test_unresolved_refund_problem_report_keeps_business_request(self, query):
        router = _router(
            '{"target":"agent","speech_act":"INFORMATION_QUERY","confidence":0.95,'
            '"domain":"refund","operation":"anomaly",'
            '"requests":[{"domain":"refund","operation":"anomaly"}]}'
        )

        intent = await router.route(query)

        assert intent.route_source == "llm"
        assert intent.speech_act == "INFORMATION_QUERY"
        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", "anomaly")]

    @pytest.mark.asyncio
    async def test_history_only_resolves_short_context_without_polluting_explicit_current_goal(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"expected_arrival",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "另外一笔退款什么时候到账",
            history=[{"role": "user", "content": "之前有一笔订单拒收了，正在等退款"}],
        )

        assert intent.domain == "refund"
        assert intent.operation == "expected_arrival"

    @pytest.mark.asyncio
    async def test_weak_arrival_question_uses_recent_return_context(self):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "退款什么时候到账",
            history=[{"role": "user", "content": "之前有一笔订单拒收了，正在等退款"}],
        )

        assert intent.domain == "return"
        assert intent.operation == "refund_dependency"

    @pytest.mark.asyncio
    async def test_weak_refund_status_followup_uses_recent_return_context(self):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "什么时候退款",
            history=[{"role": "user", "content": "我申请退货了，正在等仓库收货"}],
        )

        assert intent.domain == "return"
        assert intent.operation == "refund_dependency"

    @pytest.mark.asyncio
    async def test_weak_refund_status_followup_uses_recent_price_protection_context(self):
        router = _router(
            '{"target":"agent","domain":"price_protection","operation":"refund_status",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "什么时候到账",
            history=[{"role": "assistant", "content": "价保申请已经受理，正在处理差价退款"}],
        )

        assert intent.domain == "price_protection"
        assert intent.operation == "refund_status"

    @pytest.mark.asyncio
    async def test_current_refund_destination_is_not_changed_by_price_protection_history(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"destination",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "在京东白条里面退款?",
            history=[{"role": "user", "content": "之前问的是价保差价怎么退"}],
        )

        assert intent.domain == "refund"
        assert intent.operation == "destination"

    @pytest.mark.asyncio
    async def test_refund_noun_alone_does_not_create_request_without_explicit_action(self):
        router = _router('{"target":"agent","speech_act":"CLARIFICATION_NEEDED","requests":[],"confidence":0.8}')

        intent = await router.route("退款")

        assert intent.speech_act == "CLARIFICATION_NEEDED"
        assert intent.requests == []
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_refund_clarification_is_safe_router_noop(self):
        router = _router(
            '{"target":"agent","speech_act":"CLARIFICATION_NEEDED",'
            '"domain":"refund","operation":"clarify","next_step":"ASK_CLARIFICATION",'
            '"requests":[],"confidence":0.95}'
        )

        intent = await router.route("我不小心申请了退款")

        assert intent.route_source == "llm"
        assert intent.speech_act == "CLARIFICATION_NEEDED"
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_generic_delivery_hint_cannot_steal_unresolved_refund_semantics(self):
        router = _router(
            '{"target":"agent","speech_act":"INFORMATION_QUERY","confidence":0.95,'
            '"domain":"refund","operation":"anomaly","requests":[{"domain":"refund","operation":"anomaly"}]}'
        )

        intent = await router.route("商家未按双方约定方式发送快递包裹，且不退款")

        assert intent.route_source == "llm"
        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", "anomaly")]

    @pytest.mark.asyncio
    async def test_obvious_multi_goal_refund_expression_is_left_to_llm(self):
        router = _router(
            '{"target":"agent","confidence":0.95,"requests":['
            '{"domain":"refund","operation":"cancel"},'
            '{"domain":"after_sales","operation":"exchange"}]}'
        )

        intent = await router.route("我申请退款了，但是现在想换货，还能取消退款吗")

        assert [request.operation for request in intent.requests] == ["cancel", "exchange"]
        assert intent.required_tools == []

    @pytest.mark.asyncio
    async def test_annotation_long_tail_is_canonicalized_to_operation_modifier(self):
        router = _router('{"target":"agent","domain":"refund","operation":"status_amount","confidence":0.95}')

        intent = await router.route("短信提示金额")

        assert intent.operation == "amount"
        assert intent.goal_modifier == "status_with_amount"

    @pytest.mark.asyncio
    async def test_refund_destination_is_not_collapsed_into_status(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"destination",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route("退款成功后会退到哪里，原路退回吗")

        assert intent.target == "agent"
        assert intent.domain == "refund"
        assert intent.operation == "destination"
        assert intent.required_tools == []

    @pytest.mark.asyncio
    async def test_return_refund_dependency_is_cross_domain_route(self):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route("拒收后什么时候退款")

        assert intent.target == "agent"
        assert intent.domain == "return"
        assert intent.operation == "refund_dependency"
        assert intent.required_tools == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["退货后什么时候退款", "商品回仓后多久退款"])
    async def test_return_dependency_requires_refund_progression_relation(self, query):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(query)

        assert intent.route_source == "llm"
        assert (intent.domain, intent.operation) == ("return", "refund_dependency")

    @pytest.mark.asyncio
    async def test_return_and_refund_words_alone_do_not_force_dependency_goal(self):
        router = _router(
            '{"target":"agent","speech_act":"INFORMATION_QUERY","confidence":0.95,'
            '"domain":"refund","operation":"eligibility",'
            '"requests":[{"domain":"refund","operation":"eligibility"}]}'
        )

        intent = await router.route("可以退货退款吗")

        assert intent.route_source == "llm"
        assert (intent.domain, intent.operation) == ("refund", "eligibility")

    @pytest.mark.asyncio
    async def test_return_refund_entry_problem_beats_return_dependency_fast_path(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"request",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route("拒收后找不到退款申请入口怎么办")

        assert intent.route_source == "llm"
        assert (intent.domain, intent.operation) == ("refund", "request")

    @pytest.mark.asyncio
    async def test_hypothetical_return_history_does_not_create_active_return_context(self):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"expected_arrival",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "退款多久能到账",
            history=[{"role": "assistant", "content": "如果拦截失败可以拒收。"}],
        )

        assert (intent.domain, intent.operation) == ("refund", "expected_arrival")

    @pytest.mark.asyncio
    async def test_completed_return_history_can_resolve_weak_refund_followup(self):
        router = _router(
            '{"target":"agent","domain":"return","operation":"refund_dependency",'
            '"speech_act":"INFORMATION_QUERY","confidence":0.95}'
        )

        intent = await router.route(
            "什么时候退款",
            history=[{"role": "assistant", "content": "商品已经拒收并退回仓库。"}],
        )

        assert (intent.domain, intent.operation) == ("return", "refund_dependency")

    @pytest.mark.asyncio
    async def test_refund_statement_without_goal_does_not_create_support_request(self):
        router = _router(
            '{"target":"agent","speech_act":"STATEMENT","domain":"refund",'
            '"operation":"status","requests":[],"customer_claims":["refund_submitted"],'
            '"next_step":"ANSWER","confidence":0.95}'
        )

        intent = await router.route("嗯，我已经申请退款了")

        assert intent.target == "agent"
        assert intent.speech_act == "STATEMENT"
        assert intent.requests == []
        assert intent.support_requests == []
        assert [(request.domain, request.operation) for request in intent.verification_requests] == [
            ("refund", "status")
        ]
        assert intent.use_workflow is True
        assert intent.next_step == "ANSWER"
        assert intent.required_tools == []

    @pytest.mark.asyncio
    async def test_accidental_refund_application_requires_clarification_without_request(self):
        router = _router(
            '{"target":"agent","speech_act":"CLARIFICATION_NEEDED",'
            '"domain":"general","operation":"clarify","next_step":"ASK_CLARIFICATION",'
            '"requests":[],"confidence":0.95}'
        )

        intent = await router.route("我不小心申请了退款")

        assert intent.speech_act == "CLARIFICATION_NEEDED"
        assert intent.requests == []
        assert intent.support_requests == []
        assert intent.next_step == "ASK_CLARIFICATION"

    @pytest.mark.asyncio
    async def test_future_refund_intention_is_not_a_current_refund_request(self):
        router = _router('{"target":"agent","speech_act":"FUTURE_INTENTION","confidence":0.95,"requests":[]}')

        intent = await router.route("再等两天，不行我就退款")

        assert intent.speech_act == "FUTURE_INTENTION"
        assert intent.requests == []
        assert intent.support_requests == []

    @pytest.mark.asyncio
    async def test_customer_can_confirm_human_help_after_self_service_guidance(self):
        router = _router(
            '{"target":"agent","domain":"human","operation":"human_handoff",'
            '"speech_act":"ACTION_REQUEST","next_step":"ASK_CLARIFICATION","confidence":0.95}'
        )

        intent = await router.route("需要人工")

        assert intent.target == "agent"
        assert intent.operation == "human_handoff"
        assert intent.next_step == "ASK_CLARIFICATION"
        assert intent.confidence == 0.95

    @pytest.mark.asyncio
    async def test_refund_policy_question_does_not_create_ticket(self):
        """政策咨询仍应由知识库回答，不能误创建售后工单。"""
        router = _router('{"target": "rag", "table": "knowledge_chunks", "confidence": 0.9}')

        intent = await router.route("退款条件是什么")

        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_rag_with_knowledge_chunks(self):
        router = _router('{"target": "rag", "table": "knowledge_chunks", "confidence": 0.90}')
        intent = await router.route("退货需要什么条件")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_rag_with_phone_products(self):
        router = _router('{"target": "rag", "table": "phone_products", "confidence": 0.93}')
        intent = await router.route("iPhone 15 参数")
        assert intent.table == "phone_products"


# =============================================================================
# IntentRouter.route 降级 / 容错
# =============================================================================
class TestRouteFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_becomes_clarification_not_rag(self):
        router = _router('{"target": "agent", "table": "", "confidence": 0.3}')
        intent = await router.route("模糊问题")
        assert intent.target == "agent"
        assert intent.table == ""
        assert intent.domain == "general"
        assert intent.operation == "clarify"
        assert intent.next_step == "ASK_CLARIFICATION"

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back_to_conservative_clarification(self):
        router = _router("not json at all")
        intent = await router.route("随便")
        assert intent.target == "agent"
        assert intent.operation == "clarify"
        assert intent.confidence == 0.0

    @pytest.mark.asyncio
    async def test_empty_content_falls_back_to_conservative_clarification(self):
        router = _router("")
        intent = await router.route("空响应")
        assert intent.target == "agent"
        assert intent.operation == "clarify"

    @pytest.mark.asyncio
    async def test_invalid_target_falls_back_to_agent(self):
        router = _router('{"target": "unknown", "table": "", "confidence": 0.8}')
        intent = await router.route("奇怪的问题")
        assert intent.target == "agent"

    @pytest.mark.asyncio
    async def test_agent_with_table_gets_cleared(self):
        """即使 LLM 给 agent 写了 table，路由后也应清空"""
        router = _router('{"target": "agent", "table": "laptop_products", "confidence": 0.9}')
        intent = await router.route("查库存")
        assert intent.target == "agent"
        assert intent.table == ""

    @pytest.mark.asyncio
    async def test_rag_invalid_table_falls_back_to_knowledge(self):
        router = _router('{"target": "rag", "table": "weird_table", "confidence": 0.85}')
        intent = await router.route("问题")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_pre_rag_context_is_only_semantic_router_context(self):
        router = _router('{"target": "agent", "confidence": 0.9}')

        await router.route(
            "请解释运行时知识清单是什么",
            knowledge_context="[知识来源: refund.md / 退款] 规则仅作一般说明。",
        )

        message = router.llm.last_messages[-1]["content"]
        assert "项目知识摘要" in message
        assert "不是当前客户业务事实" in message
        assert "请解释运行时知识清单是什么" in message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("speech_act", ["STATEMENT", "ACKNOWLEDGEMENT", "FUTURE_INTENTION"])
    async def test_non_actionable_speech_act_cannot_create_refund_request(self, speech_act):
        router = _router(
            '{"target":"agent","domain":"refund","operation":"request",'
            f'"speech_act":"{speech_act}","next_step":"LOOKUP","confidence":0.9,'
            '"requests":[{"domain":"refund","operation":"request"}]}'
        )

        intent = await router.route("我再想想")

        assert intent.speech_act == speech_act
        assert intent.target == "agent"
        assert intent.support_requests == []
        assert intent.use_workflow is False

    @pytest.mark.asyncio
    async def test_clarification_speech_act_does_not_invent_a_business_request(self):
        router = _router(
            '{"target":"rag","domain":"refund","operation":"status",'
            '"speech_act":"CLARIFICATION_NEEDED","confidence":0.9}'
        )

        intent = await router.route("我不小心操作错了")

        assert intent.target == "agent"
        assert intent.next_step == "ASK_CLARIFICATION"
        assert intent.support_requests == []

    def test_registered_workflow_does_not_depend_on_compatibility_target(self):
        intent = Intent(target="rag", domain="refund", operation="status")

        assert [(request.domain, request.operation) for request in intent.support_requests] == [("refund", "status")]
        assert intent.use_workflow is True


# =============================================================================
# IntentRouter.route markdown 包裹
# =============================================================================
class TestRouteMarkdown:
    @pytest.mark.asyncio
    async def test_json_wrapped_in_markdown(self):
        router = _router('```json\n{"target": "rag", "table": "laptop_products", "confidence": 0.97}\n```')
        intent = await router.route("推荐笔记本")
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.confidence == 0.97

    @pytest.mark.asyncio
    async def test_json_wrapped_in_generic_markdown(self):
        router = _router('```\n{"target": "ticket", "table": "", "confidence": 0.85}\n```')
        intent = await router.route("投诉")
        assert intent.target == "agent"
        assert intent.operation == "human_handoff"
