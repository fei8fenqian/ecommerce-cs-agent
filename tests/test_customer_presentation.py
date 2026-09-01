"""CustomerPresentation V1 的纯 projection 测试。"""

from types import SimpleNamespace

import pytest

from agent.customer_presentation import build_customer_presentation
from agent.engines.loop import LoopResult
from api.chat import ChatRequest


def test_choice_projection_keeps_server_display_order_and_only_safe_fields():
    result = LoopResult(
        answer="内部模型文本",
        response_control={"mode": "ASK_CHOICE"},
        workflow_progress={
            "next_action": "ASK_CHOICE",
            "pending_choices": [
                {"order_id": "SO-A", "product_name": "戴尔笔记本", "amount_cents": 920000},
                {"order_id": "SO-B", "product_name": "Sony 手机", "amount_cents": 189900},
            ],
        },
    )

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "status"}])

    assert presentation is not None
    assert presentation["kind"] == "choice"
    assert [item["subject"]["subject_id"] for item in presentation["options"]] == ["SO-A", "SO-B"]
    assert "response_control" not in str(presentation)
    assert "workflow_progress" not in str(presentation)
    assert "amount_cents" not in str(presentation)


def test_status_projection_uses_allow_listed_customer_labels():
    result = LoopResult(
        answer="模型自由回答",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 920000},
            }
        ],
    )

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "status"}])

    assert presentation == {
        "version": 1,
        "kind": "status",
        "title": "退款状态",
        "subject": {"subject_type": "order", "subject_id": "SO-A", "title": "当前订单", "subtitle": "SO-A"},
        "status": "处理中",
        "details": [{"label": "退款金额", "value": "¥9200.00"}],
    }


def test_status_projection_rejects_unbound_flat_transaction_facts():
    result = LoopResult(
        answer="模型文本",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_facts={"refund_status": "PROCESSING", "refund_amount": 920000},
    )

    assert build_customer_presentation(result, [{"domain": "refund", "operation": "status"}]) is None


def test_historical_transaction_context_does_not_create_current_status_card():
    result = LoopResult(
        answer="根据上一轮系统查询，这笔退款目前还在处理中。",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "historical",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
            }
        ],
    )

    assert build_customer_presentation(result, [{"domain": "refund", "operation": "status"}]) is None


def test_current_transaction_context_creates_status_card():
    result = LoopResult(
        answer="当前查询结果。",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"refund_status": "PROCESSING"},
            }
        ],
    )

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "status"}])

    assert presentation is not None
    assert presentation["kind"] == "status"
    assert presentation["status"] == "处理中"


def test_historical_eligibility_cannot_join_current_shipping_for_card():
    result = LoopResult(
        answer="当前订单尚未发货。",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"shipping_status": "NOT_SHIPPED"},
            },
            {
                "subject_id": "SO-A",
                "provenance": "historical",
                "facts": {"refund_eligibility": True},
            },
        ],
    )

    assert build_customer_presentation(result, [{"domain": "refund", "operation": "eligibility"}]) is None


def test_current_eligibility_does_not_join_historical_shipping():
    result = LoopResult(
        answer="当前符合退款资格。",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"refund_eligibility": True},
            },
            {
                "subject_id": "SO-A",
                "provenance": "historical",
                "facts": {"shipping_status": "NOT_SHIPPED"},
            },
        ],
    )

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "eligibility"}])

    assert presentation is not None
    assert presentation["status"] == "符合退款资格"
    assert presentation["details"] == []


def test_current_fact_wins_over_historical_same_fact():
    result = LoopResult(
        answer="当前查询结果。",
        response_control={"mode": "FACT", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "historical",
                "facts": {"refund_status": "PROCESSING"},
            },
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"refund_status": "COMPLETED"},
            },
        ],
    )

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "status"}])

    assert presentation is not None
    assert presentation["status"] == "已完成"


def test_status_projection_does_not_mix_facts_from_another_order():
    result = LoopResult(
        answer="已切换到另一笔订单。",
        response_control={"mode": "FACT", "subject_id": "SO-B"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "historical",
                "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
            },
            {
                "subject_id": "SO-B",
                "provenance": "current",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 189900},
            },
        ],
    )
    case = SimpleNamespace(selected_subjects={"order_id": "SO-B"}, pending={}, status="COMPLETED")

    presentation = build_customer_presentation(
        result,
        [{"domain": "refund", "operation": "status"}],
        case=case,
    )

    assert presentation is not None
    assert presentation["subject"]["subject_id"] == "SO-B"
    assert presentation["status"] == "已完成"
    assert presentation["details"] == [{"label": "退款金额", "value": "¥1899.00"}]


def test_persisted_case_choices_override_regenerated_workflow_order():
    result = LoopResult(
        answer="请选择订单",
        response_control={"mode": "ASK_CHOICE"},
        workflow_progress={
            "next_action": "ASK_CHOICE",
            "pending_choices": [
                {"order_id": "SO-B", "product_name": "Sony 手机"},
                {"order_id": "SO-A", "product_name": "戴尔笔记本"},
            ],
        },
    )
    case = SimpleNamespace(
        status="AWAITING_CUSTOMER",
        selected_subjects={},
        pending={
            "kind": "customer_choice",
            "choices": [
                {"order_id": "SO-A", "product_name": "戴尔笔记本"},
                {"order_id": "SO-B", "product_name": "Sony 手机"},
            ],
        },
    )

    presentation = build_customer_presentation(
        result,
        [{"domain": "refund", "operation": "status"}],
        case=case,
    )

    assert presentation is not None
    assert [item["subject"]["subject_id"] for item in presentation["options"]] == ["SO-A", "SO-B"]


def test_chat_request_allows_only_query_or_subject_choice():
    assert ChatRequest(query="查询退款").history_content == "查询退款"
    interaction_request = ChatRequest(
        interaction={"type": "subject_choice", "subject_type": "order", "subject_id": "SO-A"}
    )
    assert interaction_request.history_content == "已选择订单：SO-A"

    with pytest.raises(ValueError):
        ChatRequest()
    with pytest.raises(ValueError):
        ChatRequest(
            query="查询退款",
            interaction={"type": "subject_choice", "subject_type": "order", "subject_id": "SO-A"},
        )


def test_handoff_projection_requires_a_trusted_order_entry():
    result = LoopResult(
        answer="模型文本",
        response_control={"mode": "SELF_SERVICE_HANDOFF", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"refund_entry": "?page=orders&refund_order=SO-A"},
            }
        ],
    )
    case = SimpleNamespace(selected_subjects={"order_id": "SO-A"}, pending={}, status="COMPLETED")

    presentation = build_customer_presentation(result, [{"domain": "refund", "operation": "request"}], case=case)

    assert presentation is not None
    assert presentation["kind"] == "action"
    action = presentation["actions"][0]
    assert action["destination"] == "orders"
    assert action["target"] == {"order_id": "SO-A", "focus": "refund"}

    result.decision_contexts[0]["facts"]["refund_entry"] = "https://example.invalid/refund"
    assert build_customer_presentation(result, [{"domain": "refund", "operation": "request"}], case=case) is None


def test_pending_payment_cancel_handoff_projects_typed_order_action():
    result = LoopResult(
        answer="前往订单取消",
        response_control={"mode": "SELF_SERVICE_ORDER_CANCEL", "subject_id": "SO-A"},
        decision_contexts=[
            {
                "subject_id": "SO-A",
                "provenance": "current",
                "facts": {"order_status": "PENDING_PAYMENT"},
            }
        ],
    )
    case = SimpleNamespace(selected_subjects={"order_id": "SO-A"}, pending={}, status="COMPLETED")

    presentation = build_customer_presentation(result, [{"domain": "order", "operation": "cancel"}], case=case)

    assert presentation is not None
    assert presentation["kind"] == "action"
    assert presentation["actions"][0]["target"] == {"order_id": "SO-A", "focus": "cancel"}
