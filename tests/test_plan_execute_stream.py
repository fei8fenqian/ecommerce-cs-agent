"""Plan-and-Execute 流式事件适配测试。"""

from typing import Any

import pytest

from agent.engines.plan_execute import PlanAndExecuteAgent
from agent.tools_registry import ToolRegistry


class _ModeTupleGraph:
    """模拟新版 LangGraph 的 ``(mode, payload)`` 流事件。"""

    async def astream(self, initial_state: dict[str, Any], *, stream_mode: list[str]):
        assert stream_mode == ["updates", "values"]
        yield ("values", {**initial_state, "plan": []})
        yield ("updates", {"planner": {"plan": []}})
        yield ("values", {**initial_state, "plan": [], "answer": "请检查无线网卡连接"})


@pytest.mark.asyncio
async def test_run_stream_adapts_langgraph_mode_tuples() -> None:
    """新版 LangGraph 的 mode 元组不应被误当作节点字典。"""
    agent = PlanAndExecuteAgent(llm=object(), registry=ToolRegistry())
    agent._graph = _ModeTupleGraph()

    events = [event async for event in agent.run_stream("清灰后 Wi-Fi 信号很差", scenario="troubleshoot")]

    assert events == [
        {"event": "node_complete", "name": "planner", "data": {"planner": {"plan": []}}},
        {
            "event": "done",
            "data": {
                "messages": [],
                "query": "清灰后 Wi-Fi 信号很差",
                "scenario": "troubleshoot",
                "tool_context": None,
                "max_iterations": agent.max_iterations,
                "plan": [],
                "answer": "请检查无线网卡连接",
            },
        },
    ]
