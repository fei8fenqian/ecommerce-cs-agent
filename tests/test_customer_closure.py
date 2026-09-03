"""Small regressions for payment/pending-order customer closure boundaries."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agent.decision_context import SUBJECT_CONTEXT_RESET_MARKER
from agent.engines.loop import LoopResult
from agent.llm.intent_router import Intent, SupportRequest
from agent.tools_registry import ToolContext, ToolResult
from api.chat import (
    _apply_customer_refund_fact_boundary,
    _await_support_case_customer,
    _immediate_trusted_subject_continuation,
    _trusted_previous_subject_for_resolution,
    _workflow_subject_context_kwargs,
)
from service.support_case_service import SupportCaseService
from store.support_case_store import SupportCase


def _completed_eligibility_case(*, reset: bool = False) -> SupportCase:
    facts = (
        {SUBJECT_CONTEXT_RESET_MARKER: {"from_subject_id": "SO-AMD", "reason": "customer_disputed_subject"}}
        if reset
        else {}
    )
    return SupportCase(
        case_id=uuid4(),
        session_id=UUID("00000000-0000-0000-0000-000000000091"),
        customer_user_id=7,
        status="COMPLETED",
        request_stack=[{"domain": "refund", "operation": "eligibility"}],
        selected_subjects={"order_id": "SO-AMD"},
        verified_facts=facts,
        pending={},
        pending_command={},
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


def _active_eligibility_case() -> SupportCase:
    base = _completed_eligibility_case()
    return SupportCase(**{**base.__dict__, "status": "ACTIVE", "completed_at": None})


@pytest.mark.asyncio
async def test_active_case_subject_is_previous_resolver_context_on_natural_language_turn():
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status")],
        subject_relation="same",
    )

    kwargs = await _workflow_subject_context_kwargs(
        request,
        intent=intent,
        support_case=_active_eligibility_case(),
        recent_case=None,
        tool_context=ToolContext(user_id=7, role="customer"),
        structured_interaction=False,
    )

    assert kwargs == {"previous_subjects": {"order_id": "SO-AMD"}}
    assert "selected_subjects" not in kwargs
    registry.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_structured_choice_remains_current_subject_without_semantic_rebinding():
    registry = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    intent = Intent(target="agent", requests=[SupportRequest(domain="refund", operation="status")])

    kwargs = await _workflow_subject_context_kwargs(
        request,
        intent=intent,
        support_case=_active_eligibility_case(),
        recent_case=None,
        tool_context=ToolContext(user_id=7, role="customer"),
        structured_interaction=True,
    )

    assert kwargs == {"selected_subjects": {"order_id": "SO-AMD"}}
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_claim_verification_persists_pending_choice_request_stack():
    case = _active_eligibility_case()
    service = SupportCaseService()
    service.await_customer = AsyncMock(return_value=case)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(support_case_service=service)))
    intent = Intent(
        target="agent",
        speech_act="STATEMENT",
        domain="refund",
        operation="status",
        customer_claims=["refund_submitted"],
    )

    updated = await _await_support_case_customer(
        request,
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_customer",
            "next_action": "ASK_CHOICE",
            "pending_choices": [
                {"order_id": "SO-A", "product_name": "HUAWEI Mate70"},
                {"order_id": "SO-B", "product_name": "HUAWEI Pura70"},
            ],
        },
    )

    assert updated is case
    kwargs = service.await_customer.await_args.kwargs
    assert kwargs["request_stack"] == [
        {
            "domain": "refund",
            "operation": "status",
            "desired_outcome": "",
            "goal_modifier": "",
            "subject_refs": [],
            "customer_claims": ["refund_submitted"],
            "ambiguities": [],
            "missing_facts": [],
            "next_step": "LOOKUP",
            "required_tools": [],
            "risk": "read_only",
        }
    ]
    assert kwargs["pending"]["kind"] == "customer_choice"
    assert [item["order_id"] for item in kwargs["pending"]["choices"]] == ["SO-A", "SO-B"]



@pytest.mark.asyncio
async def test_trusted_subject_continuation_reuses_recent_validated_subject_for_new_refund_goal():
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="expected_arrival")],
        subject_relation="same",
    )

    inherited = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="我已经申请了，什么时候能退款成功？",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert inherited == {"order_id": "SO-AMD"}
    registry.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_trusted_subject_continuation_carries_only_subject_not_old_transaction_facts():
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    recent = SupportCase(
        **{
            **_completed_eligibility_case().__dict__,
            "request_stack": [{"domain": "refund", "operation": "request"}],
            "verified_facts": {
                "_decision_contexts": [
                    {
                        "subject_type": "order",
                        "subject_id": "SO-AMD",
                        "provenance": "current",
                        "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                    }
                ]
            },
        }
    )
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status")],
        subject_relation="same",
    )

    inherited = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=recent,
        raw_query="退款现在怎么样？",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert inherited == {"order_id": "SO-AMD"}
    assert set(inherited) == {"order_id"}


@pytest.mark.asyncio
async def test_trusted_subject_continuation_rejects_changed_or_disputed_subject():
    registry = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status", subject_refs=["Sony 耳机"])],
        subject_relation="changed",
    )

    changed = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="那 Sony 那笔退款呢",
        tool_context=ToolContext(user_id=7, role="customer"),
    )
    disputed = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(reset=True),
        raw_query="退款现在怎么样？",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert changed is None
    assert disputed is None
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_follow_up_transcript_keeps_subject_but_returns_safe_partial_eta():
    """近似真实链路：自助入口后追问 ETA，不重选订单、不转假人工。"""
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="expected_arrival")],
        subject_relation="same",
    )

    selected = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="我已经申请了，什么时候能退款成功？",
        tool_context=ToolContext(user_id=7, role="customer"),
    )
    assert selected == {"order_id": "SO-AMD"}

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
                "subject_id": "SO-AMD",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "PENDING_MERCHANT_REVIEW"},
            }
        ],
    )
    _apply_customer_refund_fact_boundary(intent, result)

    assert "等待商家核验" in result.answer
    assert "具体到账时间" in result.answer
    assert "三到五天" not in result.answer
    assert "转人工客服处理" not in result.answer
    assert result.response_control["mode"] == "FACT"
    assert result.response_control["subject_id"] == "SO-AMD"
    assert result.response_control["fact_provenance"] == {"refund_status": "current"}


@pytest.mark.asyncio
async def test_trusted_subject_continuation_is_vetoed_by_fresh_unique_identity_contradiction(monkeypatch):
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    monkeypatch.setattr(
        "api.chat._customer_subject_choices",
        AsyncMock(
            return_value=[
                {"order_id": "SO-HW", "product_name": "HUAWEI Mate 70", "recency_rank": 1},
                {"order_id": "SO-AP", "product_name": "Apple iPhone Air 1TB", "recency_rank": 2},
                {"order_id": "SO-AMD", "product_name": "Apple iPhone Air 512GB", "recency_rank": 3},
            ]
        ),
    )
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="request")],
        case_update="continue",
        subject_relation="same",
    )

    inherited = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="我问的是刚刚买的华为手机",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert inherited is None
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_trusted_subject_continuation_is_vetoed_when_fresh_identity_is_ambiguous(monkeypatch):
    registry = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    monkeypatch.setattr(
        "api.chat._customer_subject_choices",
        AsyncMock(
            return_value=[
                {"order_id": "SO-HW-1", "product_name": "HUAWEI Mate 70", "recency_rank": 1},
                {"order_id": "SO-HW-2", "product_name": "HUAWEI Pura 80", "recency_rank": 2},
                {"order_id": "SO-AMD", "product_name": "Apple iPhone Air 512GB", "recency_rank": 3},
            ]
        ),
    )
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="request")],
        case_update="continue",
        subject_relation="same",
    )

    inherited = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="刚买的华为手机我想处理一下",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert inherited is None
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_trusted_subject_continuation_keeps_router_semantics_without_identity_evidence(monkeypatch):
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-AMD"}))
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(registry=registry)))
    monkeypatch.setattr(
        "api.chat._customer_subject_choices",
        AsyncMock(
            return_value=[
                {"order_id": "SO-HW", "product_name": "HUAWEI Mate 70", "recency_rank": 1},
                {"order_id": "SO-AMD", "product_name": "Apple iPhone Air 512GB", "recency_rank": 2},
            ]
        ),
    )
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status")],
        case_update="continue",
        subject_relation="same",
    )

    inherited = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=_completed_eligibility_case(),
        raw_query="退款现在怎么样了",
        tool_context=ToolContext(user_id=7, role="customer"),
    )

    assert inherited == {"order_id": "SO-AMD"}
    registry.execute.assert_awaited_once()
