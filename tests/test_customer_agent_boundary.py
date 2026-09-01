"""客户 Agent 的公开信息边界与站内下一步链接测试。"""

from types import SimpleNamespace

from agent.customer_response import compose_customer_response
from agent.decision_context import SUBJECT_CONTEXT_RESET_MARKER
from agent.engines.loop import AgentLoop, LoopResult
from agent.llm.intent_router import Intent
from agent.tools.search_product import _customer_visible_content
from agent.tools_registry import ToolRegistry
from api.chat import (
    _append_customer_action_suffix,
    _apply_customer_refund_fact_boundary,
    _awaiting_staff_response,
    _customer_action_suffix,
)


def test_customer_prompt_forbids_internal_inventory_facts():
    """客户身份必须得到不同于内部人员的模型输出约束。"""
    agent = AgentLoop(llm=object(), registry=ToolRegistry())
    prompt = agent._system_content_for(type("Context", (), {"role": "customer"})())

    assert "绝不提及仓库名称" in prompt
    assert "精确库存数量" in prompt


def test_refund_request_choice_explains_one_order_at_a_time():
    result = LoopResult(
        answer="模型原文",
        workflow_progress={
            "next_action": "ASK_CHOICE",
            "pending_choices": [
                {"order_id": "SO-A", "product_name": "联想拯救者"},
                {"order_id": "SO-B", "product_name": "联想小新"},
            ],
        },
    )

    compose_customer_response(
        result,
        [{"domain": "refund", "operation": "request"}],
    )

    assert "目前一次只能处理一笔退款" in result.answer
    assert "处理完成后可以继续处理另一笔" in result.answer


def test_customer_product_text_strips_internal_warehouse_details():
    """商品检索的客户视图不携带仓库和库存细节。"""
    content = _customer_visible_content("售价 4999 元。华南仓库存 18 台，现货。")

    assert "华南仓" not in content
    assert "库存" not in content
    assert "售价 4999 元" in content


def test_plain_chat_refund_status_is_rendered_from_verified_facts():
    result = LoopResult(
        answer="退款已经到账支付宝。",
        decision_facts={"refund_status": "COMPLETED", "refund_amount": 799900},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-STATUS",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 799900},
            }
        ],
    )
    intent = Intent(target="agent", domain="refund", operation="status")

    _apply_customer_refund_fact_boundary(intent, result)

    assert result.answer == "系统里的退款记录目前显示已完成。退款金额为 ¥7999.00。"
    assert "到账" not in result.answer
    assert "支付宝" not in result.answer


def test_payment_provider_unavailable_is_rendered_without_speculating_failure_reason():
    result = LoopResult(
        answer="可能是余额、银行卡、花呗额度或网络问题，一般会自动同步。",
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-PAYMENT",
                "provenance": "current",
                "source": "check_payment_status",
                "facts": {"payment_status": "PAYMENT_STATUS_UNAVAILABLE", "order_status": "PENDING_PAYMENT"},
            }
        ],
    )

    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="payment", operation="check_payment_status"), result
    )

    assert "支付渠道当前暂时无法确认" in result.answer
    assert all(marker not in result.answer for marker in ("余额", "银行卡", "花呗", "网络", "自动同步"))


def test_pending_payment_refund_path_is_controlled_cancel_handoff():
    result = LoopResult(
        answer="我会帮你申请退款。",
        workflow_progress={"resolution_type": "SELF_SERVICE_ORDER_CANCEL"},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-PENDING",
                "provenance": "current",
                "source": "track_order",
                "facts": {"order_identified": True, "order_status": "PENDING_PAYMENT"},
            }
        ],
    )

    _apply_customer_refund_fact_boundary(Intent(target="agent", domain="refund", operation="request"), result)

    assert "没有已支付款项需要退款" in result.answer
    assert "前往订单页取消" in result.answer
    assert "帮你申请退款" not in result.answer
    assert result.response_control["mode"] == "SELF_SERVICE_ORDER_CANCEL"


def test_cross_turn_refund_facts_constrain_follow_up_without_new_intent():
    result = LoopResult(answer=("处理中属于正常状态，一般需要 1-7 个工作日完成平台审核，到账后页面会更新。"))
    _apply_customer_refund_fact_boundary(
        Intent(target="agent"),
        result,
        query="退款现在怎么样？",
        session_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
        session_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-FOLLOWUP",
                "provenance": "historical",
                "source": "query_refund_status",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
            }
        ],
    )

    assert result.answer == "根据上一轮系统查询，这笔退款目前还在处理中。退款金额为 ¥8999.00。"
    assert "1-7 个工作日" not in result.answer
    assert "平台审核" not in result.answer


def _historical_refund_case() -> SimpleNamespace:
    context = {
        "subject_type": "order",
        "subject_id": "SO-OLD-ORDER",
        "provenance": "current",
        "source": "query_refund_status",
        "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
    }
    return SimpleNamespace(
        status="ACTIVE",
        request_stack=[{"domain": "refund", "operation": "status"}],
        selected_subjects={"order_id": "SO-OLD-ORDER"},
        verified_facts={"_decision_contexts": [context]},
        pending={},
    )


def test_unrelated_turn_does_not_resurrect_recent_refund_goal_or_facts():
    for query in ("啊啊啊", "有病是吧", "今天天气不错", "谢谢", "xj3%%%乱码"):
        result = LoopResult(answer="系统核验结果显示，这笔订单当前符合退款资格。订单当前尚未发货。")
        _apply_customer_refund_fact_boundary(
            Intent(target="agent"),
            result,
            query=query,
            recent_case=_historical_refund_case(),
        )

        assert "退款资格" not in result.answer
        assert "尚未发货" not in result.answer
        assert "SO-OLD-ORDER" not in result.answer


def test_explicit_refund_follow_up_may_use_historical_fact_with_provenance_label():
    result = LoopResult(answer="也许很快就会到账。")
    _apply_customer_refund_fact_boundary(
        Intent(target="agent"),
        result,
        query="退款现在怎么样？",
        recent_case=_historical_refund_case(),
    )

    assert result.answer.startswith("根据上一轮系统查询")
    assert "目前还在处理中" in result.answer
    assert "8999" in result.answer


def test_awaiting_staff_response_does_not_claim_a_ticket_without_ticket_id():
    case = SimpleNamespace(
        status="AWAITING_STAFF",
        pending={"kind": "staff_handoff", "summary": {}},
        selected_subjects={"order_id": "SO-OLD-ORDER"},
        request_stack=[{"domain": "refund", "operation": "request"}],
    )

    result, presentation, ticket_id = _awaiting_staff_response(case)

    assert ticket_id is None
    assert "已创建人工客服工单" not in result.answer
    assert "已保留后续处理入口" not in result.answer
    assert "回复“转人工”" in result.answer
    assert result.response_control["mode"] == "FACT"
    assert presentation is None


def test_subjectless_legacy_refund_facts_are_not_customer_visible():
    """旧 flat metadata 只能标记退款上下文，不能直接支撑交易结论。"""
    result = LoopResult(
        answer="根据上一轮查询，这笔退款目前还在处理中，退款金额为 ¥8999.00。",
        decision_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
    )

    _apply_customer_refund_fact_boundary(
        Intent(target="agent"),
        result,
        query="页面上显示还在处理中",
        session_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
    )

    assert result.response_control["mode"] == "AWAITING_CUSTOMER"
    assert "处理中" not in result.answer
    assert "8999" not in result.answer
    assert "请提供或确认具体订单号" in result.answer


def test_superseded_subject_cannot_reenter_renderer_through_session_flat_facts():
    """新请求不能借旧 session flat metadata 恢复已被客户否定的订单。"""
    result = LoopResult(answer="这笔退款记录目前显示处理中，金额为 ¥8999.00。")
    case = SimpleNamespace(
        status="ACTIVE",
        request_stack=[],
        selected_subjects={},
        verified_facts={
            SUBJECT_CONTEXT_RESET_MARKER: {
                "from_subject_id": "SO-A",
                "reason": "customer_disputed_subject",
            },
            "_decision_contexts": [
                {
                    "subject_type": "order",
                    "subject_id": "SO-A",
                    "provenance": "historical",
                    "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                }
            ],
        },
        pending={},
    )

    _apply_customer_refund_fact_boundary(
        Intent(target="agent"),
        result,
        query="换货",
        recent_case=case,
        session_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
        session_contexts=[],
    )

    assert result.response_control["mode"] == "AWAITING_CUSTOMER"
    assert "PROCESSING" not in result.answer
    assert "8999" not in result.answer


def test_subjectless_legacy_refund_entry_is_not_customer_visible():
    """旧 flat metadata 中的退款入口也不能绕过 subject binding。"""
    entry = "?page=orders&refund_order=SO-LEGACY"
    result = LoopResult(
        answer="可以，确认后我帮您提交退款。",
        decision_facts={"refund_entry": entry},
        workflow_progress={"resolution_type": "SELF_SERVICE_HANDOFF"},
    )

    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="request"),
        result,
    )

    assert result.response_control["mode"] == "FACT"
    assert entry not in result.answer
    assert "无法生成可用的官方退款入口" in result.answer


def test_refund_fact_boundary_falls_back_from_legacy_operation_to_status():
    """旧兼容字段不能绕过 status 的 customer-visible 事实边界。"""
    result = LoopResult(
        answer="退款已提交，一般几个工作日到账。",
        decision_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-LEGACY-OP",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
            }
        ],
    )
    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="refund", speech_act="STATEMENT"),
        result,
    )

    assert result.answer == "这笔退款目前还在处理中。退款金额为 ¥8999.00。"
    assert "几个工作日" not in result.answer


def test_refund_fact_boundary_also_applies_to_blocked_workflow_facts():
    """blocked 不等于允许模型把已核验资格扩写成全额退款。"""
    result = LoopResult(
        answer="未发货，且符合全额退款资格。",
        decision_facts={"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-BLOCKED",
                "provenance": "current",
                "source": "check_refund_eligibility",
                "facts": {"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
            }
        ],
        workflow_progress={"goal_status": "blocked", "reason": "capability_unavailable"},
    )
    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="after_sales", operation="after_sales_transition"),
        result,
    )

    assert "系统核验结果显示，这笔订单当前符合退款资格。" in result.answer
    assert "订单当前尚未发货。" in result.answer
    assert "如需人工客服协助，请回复“转人工”。" in result.answer
    assert "全额" not in result.answer


def test_blocked_response_names_missing_capability_instead_of_generic_contradiction():
    result = LoopResult(
        answer="我会继续处理。",
        decision_facts={"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-BLOCKED-ENTRY",
                "provenance": "current",
                "source": "check_refund_eligibility",
                "facts": {"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
            }
        ],
        workflow_progress={
            "goal_status": "blocked",
            "reason": "capability_unavailable",
            "missing_facts": ["refund_entry"],
        },
    )

    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="request"),
        result,
    )

    assert result.answer == (
        "这笔订单当前符合退款资格且尚未发货，但暂时无法生成退款入口。请从订单页稍后重试；如仍无法操作，可转人工。"
    )
    assert "当前还无法完成这项业务核验" not in result.answer


def test_refund_eta_capability_gap_returns_verified_status_with_limitation_not_staff_handoff():
    result = LoopResult(
        answer="模型猜测三到五天到账。",
        workflow_progress={
            "goal_status": "resolved_with_limitation",
            "control_state": "RESOLVED_WITH_LIMITATION",
            "next_action": "EXPLAIN_LIMITATION",
            "next_actor": "NONE",
            "reason": "capability_unavailable",
            "unavailable_capabilities": ["query_refund_expected_arrival"],
        },
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-ETA",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PENDING_MERCHANT_REVIEW"},
            }
        ],
    )

    _apply_customer_refund_fact_boundary(Intent(target="agent", domain="refund", operation="expected_arrival"), result)

    assert "等待商家核验" in result.answer
    assert "具体到账时间" in result.answer
    assert "三到五天" not in result.answer
    assert result.response_control["mode"] == "FACT"


def test_refund_procedure_is_deterministic_and_does_not_request_reason():
    result = LoopResult(answer="请提供订单号和退款原因，我来帮您处理。")

    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="procedure"),
        result,
    )

    assert "我的订单" in result.answer
    assert "退款原因" in result.answer
    assert "请提供订单号" not in result.answer
    assert result.response_control["mode"] == "FACT"


def test_refund_request_without_subject_does_not_ask_for_reason_in_chat():
    result = LoopResult(answer="请提供订单号和退款原因，我来帮您处理。")

    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="request"),
        result,
    )

    assert "请先选择或提供要处理的订单" in result.answer
    assert "请提供退款原因" not in result.answer


def test_existing_refund_request_uses_deterministic_explanation():
    result = LoopResult(
        answer="通常不需要重复提交，重复申请可能导致处理混乱。",
        decision_facts={"refund_status": "PROCESSING", "refund_amount": 899900},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-EXISTING",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
            }
        ],
        workflow_progress={"goal_status": "resolved"},
    )
    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="refund", operation="request"),
        result,
    )

    assert (
        result.answer
        == "系统已查到这笔订单存在退款记录，当前退款状态为处理中。退款金额为 ¥8999.00。客服不会重复创建另一笔退款。"
    )
    assert "通常" not in result.answer
    assert "处理混乱" not in result.answer


def test_eligibility_boundary_does_not_claim_full_refund():
    result = LoopResult(
        answer="未发货，且符合全额退款资格。",
        decision_facts={
            "refund_eligibility": True,
            "shipping_status": "NOT_SHIPPED",
        },
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-TEST-ELIGIBILITY",
                "provenance": "current",
                "source": "check_refund_eligibility",
                "facts": {"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
            }
        ],
    )
    _apply_customer_refund_fact_boundary(
        Intent(target="agent", domain="after_sales", operation="after_sales_transition"),
        result,
    )

    assert "全额" not in result.answer
    assert result.answer == "系统核验结果显示，这笔订单当前符合退款资格。订单当前尚未发货。"


def test_customer_action_links_point_to_first_party_pages():
    """购买、订单和售后诉求必须获得稳定的站内下一步。"""
    assert "?page=catalog" in _customer_action_suffix("rag", "laptop_products", "我想买这台")
    assert "?page=orders" in _customer_action_suffix("agent", "", "帮我查物流")
    assert "?page=tickets" in _customer_action_suffix("ticket", "", "我要退款")


def test_raw_refund_words_do_not_append_generic_refund_application_link():
    for query in ("我已经申请退款了", "退款现在什么状态", "我不想退款"):
        assert _customer_action_suffix("agent", "", query) == ""


def test_trusted_refund_handoff_entry_is_preserved_without_generic_suffix():
    entry = "?page=orders&refund_order=SO-1"
    answer = f"退款资格已核验。\n\n[前往我的订单申请退款]({entry})"

    assert entry in _customer_action_suffix("agent", "", "我要退款", trusted_refund_entry=entry)
    assert _append_customer_action_suffix(answer, "agent", "", "我要退款") == answer


def test_customer_action_link_is_not_duplicated_when_answer_already_contains_it():
    answer = "请前往订单页申请退款。\n\n[前往我的订单申请退款](?page=orders)"

    result = _append_customer_action_suffix(answer, "agent", "", "确认退款")

    assert result == answer


def test_pending_customer_choice_precedes_transaction_fact_renderer():
    result = LoopResult(
        answer="系统里的退款记录目前显示已完成。",
        decision_facts={"refund_status": "COMPLETED", "refund_amount": 220000},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-A",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 920000},
            },
            {
                "subject_type": "order",
                "subject_id": "SO-B",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 220000},
            },
        ],
        workflow_progress={"goal_status": "resolved", "next_action": "ANSWER"},
    )
    case = SimpleNamespace(
        status="AWAITING_CUSTOMER",
        selected_subjects={},
        pending={
            "kind": "customer_choice",
            "choices": [
                {"order_id": "SO-A", "product_name": "戴尔笔记本", "amount_cents": 920000},
                {"order_id": "SO-B", "product_name": "Sony 手机", "amount_cents": 220000},
            ],
        },
    )

    compose_customer_response(
        result,
        [{"domain": "refund", "operation": "status"}],
        case=case,
    )

    assert "选择" in result.answer
    assert "1. SO-A" in result.answer
    assert "2. SO-B" in result.answer
    assert "退款记录目前显示已完成" not in result.answer
    assert "¥9200.00" in result.answer
    assert "¥2200.00" in result.answer


def test_multiple_subject_contexts_fail_closed_before_refund_fact_rendering():
    result = LoopResult(
        answer="我查到了一笔退款。",
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-A",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 920000},
            },
            {
                "subject_type": "order",
                "subject_id": "SO-B",
                "provenance": "historical",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 220000},
            },
        ],
        workflow_progress={"goal_status": "resolved", "next_action": "ANSWER"},
    )

    compose_customer_response(
        result,
        [{"domain": "refund", "operation": "status"}],
    )

    assert "请先确认要查询的订单" in result.answer
    assert "处理中" not in result.answer
    assert "已完成" not in result.answer
    assert "¥9200.00" not in result.answer
    assert "¥2200.00" not in result.answer


def test_unbound_refund_transaction_claim_is_replaced_by_shared_boundary():
    """没有 Tool fact/subject 时，普通 AgentLoop 不得输出任意一笔退款状态。"""
    result = LoopResult(answer=("我查询到您名下有两笔退款记录：一笔处理中，另一笔已完成。请问您要查哪一笔？"))
    intent = Intent(target="rag")

    _apply_customer_refund_fact_boundary(
        intent,
        result,
        query="戴尔笔记本和 Sony 耳机这两件都有退款",
    )

    assert result.response_control["mode"] == "AWAITING_CUSTOMER"
    assert "请提供或确认具体订单号" in result.answer
    assert "处理中" not in result.answer
    assert "已完成" not in result.answer
    assert "两笔退款记录" not in result.answer


def test_unbound_refund_markdown_table_claim_is_replaced_by_shared_boundary():
    """多订单 Markdown 表格也必须先确认 subject，不能泄露任一笔交易事实。"""
    result = LoopResult(
        answer=(
            "我查询到您名下有 **2 笔退款记录**，情况如下：\n\n"
            "| 订单号 | 退款金额 | 状态 |\n"
            "|---|---|---|\n"
            "| SOREAL_A6 | ¥9,200.00 | **处理中**（PROCESSING） |\n"
            "| SOREAL_A7 | ¥1,899.00 | **已完成**（COMPLETED） |"
        )
    )
    intent = Intent(target="rag")

    _apply_customer_refund_fact_boundary(
        intent,
        result,
        query="戴尔笔记本和 Sony 耳机这两件都有退款",
    )

    assert result.response_control["mode"] == "AWAITING_CUSTOMER"
    assert "请提供或确认具体订单号" in result.answer
    assert "处理中" not in result.answer
    assert "已完成" not in result.answer
    assert "SOREAL_A6" not in result.answer


def test_unbound_compact_transaction_rows_are_replaced_by_shared_boundary():
    """没有前导“几笔退款记录”时，订单号同行事实也不能泄露。"""
    result = LoopResult(answer=("查询结果如下：\n| SOREAL_A6 | ¥9200.00 | 处理中 |\n| SOREAL_A7 | ¥1899.00 | 已完成 |"))

    _apply_customer_refund_fact_boundary(
        Intent(target="rag"),
        result,
        query="戴尔笔记本和 Sony 耳机这两件都有退款",
    )

    assert result.response_control["mode"] == "AWAITING_CUSTOMER"
    assert "请提供或确认具体订单号" in result.answer
    assert "SOREAL_A6" not in result.answer
    assert "处理中" not in result.answer
    assert "已完成" not in result.answer
