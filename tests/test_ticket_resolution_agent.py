"""自主工单 Agent 的最小业务闭环测试，不连接数据库或模型服务。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.ticket_resolution import TicketResolutionAgent
from store.ticket_store import claim_next_ticket_for_ai

TICKET = {
    "ticket_id": "ticket-ai-1",
    "customer_user_id": 101,
    "issue": "设备无法开机，请联系 13800138000。",
    "urgency": "medium",
}
KNOWLEDGE = [
    {
        "title": "无法开机排查",
        "content": "先检查电源连接，再长按电源键十秒后重试。",
    }
]


@pytest.mark.asyncio
async def test_ai_queue_claim_only_selects_explicitly_agent_queued_tickets() -> None:
    """旧的“待处理”工单不能因开启 worker 被 AI 自动领取。"""
    cursor = SimpleNamespace(fetchone=AsyncMock(return_value=None))
    connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(return_value=cursor),
    )

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        assert await claim_next_ticket_for_ai(claim_timeout_seconds=120) is None

    sql = str(connection.execute.await_args.args[0])
    assert "status = 'AI待处理'" in sql
    assert "status = '待处理'" not in sql


@pytest.mark.asyncio
async def test_supported_ticket_is_answered_and_closed() -> None:
    """知识充足时，Agent 应写 AI 消息并将工单结案。"""
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="请先检查电源连接后重试开机。")))
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    completed = AsyncMock(return_value=True)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
        patch("agent.ticket_resolution.complete_ai_ticket", new=completed),
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
    ):
        assert await agent.process_next() is True

    completed.assert_awaited_once_with("ticket-ai-1", "请先检查电源连接后重试开机。")
    handoff.assert_not_awaited()
    prompt = str(llm.chat.await_args.args[0])
    assert "13800138000" not in prompt


@pytest.mark.asyncio
async def test_missing_knowledge_explains_handoff_to_customer() -> None:
    """没有可用知识时，Agent 仍须给客户留下下一步说明。"""
    llm = SimpleNamespace(chat=AsyncMock())
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=[])),
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
    ):
        assert await agent.process_next() is True

    llm.chat.assert_not_awaited()
    handoff.assert_awaited_once()
    handoff_call = handoff.await_args
    assert handoff_call is not None
    ticket_id, customer_reply = handoff_call.args
    assert ticket_id == "ticket-ai-1"
    assert "设备型号" in customer_reply
    assert "人工客服" in customer_reply


@pytest.mark.asyncio
async def test_model_escalation_marker_moves_ticket_to_human_queue() -> None:
    """模型明确不能可靠处理时，工单回到人工队列。"""
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="[ESCALATE]")))
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
    ):
        assert await agent.process_next() is True

    handoff.assert_awaited_once()
    handoff_call = handoff.await_args
    assert handoff_call is not None
    assert "人工客服" in handoff_call.args[1]


@pytest.mark.asyncio
async def test_model_failure_moves_ticket_to_human_queue() -> None:
    """模型服务异常不能让工单卡在 AI 处理中。"""
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("provider secret")))
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
    ):
        assert await agent.process_next() is True

    handoff.assert_awaited_once()
    handoff_call = handoff.await_args
    assert handoff_call is not None
    assert "智能诊断暂时不可用" in handoff_call.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issue", "reason"),
    [
        ("请转人工客服处理", "EXPLICIT_HUMAN_REQUEST"),
        ("我要求退款并查询支付", "ORDER_OR_PAYMENT_ACTION"),
        ("我要投诉你们，问题一直没有解决", "COMPLAINT_OR_DISPUTE"),
    ],
)
async def test_deterministic_high_risk_requests_skip_llm(issue: str, reason: str) -> None:
    """高风险或明确人工诉求不应先让模型自由回答。"""
    llm = SimpleNamespace(chat=AsyncMock())
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)
    ticket = {**TICKET, "issue": issue}

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=ticket)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)) as search,
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
        patch("agent.ticket_resolution.logger") as logger,
    ):
        assert await agent.process_next() is True

    search.assert_awaited_once()
    llm.chat.assert_not_awaited()
    handoff.assert_awaited_once()
    assert logger.info.call_args.kwargs["extra"]["escalation_reason"] == reason


@pytest.mark.asyncio
async def test_repeated_unresolved_follow_up_is_escalated() -> None:
    """AI 已回复后客户再次追问且无依据时，不循环生成低质量回复。"""
    llm = SimpleNamespace(chat=AsyncMock())
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)
    ticket = {**TICKET, "previous_ai_reply": True}

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=ticket)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=[])),
        patch("agent.ticket_resolution.escalate_ai_ticket", new=handoff),
    ):
        assert await agent.process_next() is True

    llm.chat.assert_not_awaited()
    handoff.assert_awaited_once()
