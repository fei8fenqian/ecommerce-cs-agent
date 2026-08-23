"""自主工单 Agent 的最小业务闭环测试，不连接数据库或模型服务。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.ticket_resolution import TicketResolutionAgent

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
        patch("agent.ticket_resolution.send_ticket_to_human_queue", new=handoff),
    ):
        assert await agent.process_next() is True

    completed.assert_awaited_once_with("ticket-ai-1", "请先检查电源连接后重试开机。")
    handoff.assert_not_awaited()
    prompt = str(llm.chat.await_args.args[0])
    assert "13800138000" not in prompt


@pytest.mark.asyncio
async def test_missing_knowledge_moves_ticket_to_human_queue() -> None:
    """没有可用知识时，Agent 不调用模型也不编造答案。"""
    llm = SimpleNamespace(chat=AsyncMock())
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=[])),
        patch("agent.ticket_resolution.send_ticket_to_human_queue", new=handoff),
    ):
        assert await agent.process_next() is True

    llm.chat.assert_not_awaited()
    handoff.assert_awaited_once_with("ticket-ai-1")


@pytest.mark.asyncio
async def test_model_escalation_marker_moves_ticket_to_human_queue() -> None:
    """模型明确不能可靠处理时，工单回到人工队列。"""
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="[ESCALATE]")))
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
        patch("agent.ticket_resolution.send_ticket_to_human_queue", new=handoff),
    ):
        assert await agent.process_next() is True

    handoff.assert_awaited_once_with("ticket-ai-1")


@pytest.mark.asyncio
async def test_model_failure_moves_ticket_to_human_queue() -> None:
    """模型服务异常不能让工单卡在 AI 处理中。"""
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("provider secret")))
    agent = TicketResolutionAgent(llm, claim_timeout_seconds=120)
    handoff = AsyncMock(return_value=True)

    with (
        patch("agent.ticket_resolution.claim_next_ticket_for_ai", new=AsyncMock(return_value=TICKET)),
        patch("agent.ticket_resolution.hybrid_search", new=AsyncMock(return_value=KNOWLEDGE)),
        patch("agent.ticket_resolution.send_ticket_to_human_queue", new=handoff),
    ):
        assert await agent.process_next() is True

    handoff.assert_awaited_once_with("ticket-ai-1")
