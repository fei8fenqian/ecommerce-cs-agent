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
from api.chat import _apply_customer_refund_fact_boundary, _immediate_trusted_subject_continuation
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
