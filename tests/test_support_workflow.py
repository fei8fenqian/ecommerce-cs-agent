"""SupportWorkflow 的最小编排测试，不连接真实模型或数据库。"""

import pytest

from agent.engines.loop import LoopResult
from agent.engines.support_workflow import SupportWorkflowAgent


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


@pytest.mark.asyncio
async def test_support_workflow_reads_only_safe_customer_scoped_facts_before_agent():
    agent = _FakeAgent()
    registry = _FactRegistry()
    workflow = SupportWorkflowAgent(agent, registry)

    result = await workflow.run(
        "我的订单怎么还没发货",
        support_requests=[{"required_tools": ["track_order", "check_stock"]}],
        tool_context=object(),
    )

    assert registry.calls == ["track_order"]
    assert result.verified_facts["track_order"]["status"] == "success"
    assert "本轮已核验的业务事实" in agent.calls[0]["system_prompt_extra"]


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
