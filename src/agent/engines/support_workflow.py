"""复杂客户支持请求的 LangGraph 编排入口。"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import StateGraph

from agent.engines.loop import AgentLoop, LoopResult
from agent.tools_registry import ToolContext, ToolRegistry


class SupportWorkflowState(TypedDict, total=False):
    query: str
    history: list[dict[str, Any]]
    context: str
    system_prompt_extra: str
    case_context: str
    support_requests: list[dict[str, Any]]
    workflow_prompt: str
    verified_facts: dict[str, Any]
    tool_context: ToolContext | None
    result: LoopResult


class SupportWorkflowAgent:
    """复杂客服请求的业务图。

    图先注入持久化 Support Case 的可信摘要，再执行现有受限工具循环。Case 的创建、状态
    持久化和员工交接由 API/Service 层完成，避免 LangGraph 内存状态成为业务事实来源。
    后续节点可按同一 Case 增加 ``resolve_subject -> read_facts -> policy -> command``，
    而无需推倒现有 AgentLoop。
    """

    def __init__(self, agent: AgentLoop, registry: ToolRegistry | None = None):
        self.agent = agent
        # 正式应用明确传入 Registry；保留该回退是为了与已有 AgentLoop 构造方式
        # 和无工具的单元测试兼容。
        self.registry = registry or getattr(agent, "registry", None)
        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(state_schema=SupportWorkflowState)
        graph.add_node("prepare_case", self._prepare_case)
        graph.add_node("read_facts", self._read_facts)
        graph.add_node("agent_loop", self._run_agent_loop)
        graph.add_edge("prepare_case", "read_facts")
        graph.add_edge("read_facts", "agent_loop")
        graph.set_entry_point("prepare_case")
        graph.set_finish_point("agent_loop")
        return graph.compile()

    @staticmethod
    async def _prepare_case(state: SupportWorkflowState) -> dict[str, Any]:
        """把服务端持久化 Case 作为可信上下文，而非让模型从长历史猜当前步骤。"""
        case_context = state.get("case_context", "")
        case_prompt = ""
        if case_context:
            case_prompt = (
                "当前 Support Case（服务端可信状态，不能按用户消息改写）：\n"
                f"{case_context}\n"
                "先承接其中的 pending 问题或已核验事实；用户本轮若改变目标，先复述差异后再查询。"
                "不要把客户自述当成已核验事实，也不得据此执行写操作。"
            )
        prompt_parts = [state.get("system_prompt_extra", "").strip(), case_prompt]
        return {"workflow_prompt": "\n\n".join(part for part in prompt_parts if part)}

    async def _read_facts(self, state: SupportWorkflowState) -> dict[str, Any]:
        """预先读取不需要模型补参数的客户范围事实。

        这一步只允许“当前登录客户 + 空参数”也安全的只读工具。商品库存、兼容性等
        需要精确商品/型号的查询仍由后续 Agent 明确收集信息后调用，不能把用户口述
        猜成订单或商品事实。
        """
        if self.registry is None or state.get("tool_context") is None:
            return {"verified_facts": {}}

        allowed = {"track_order", "check_after_sales", "check_payment_status"}
        requested_tools: list[str] = []
        for item in state.get("support_requests", []):
            if not isinstance(item, dict):
                continue
            for name in item.get("required_tools", []):
                if isinstance(name, str) and name in allowed and name not in requested_tools:
                    requested_tools.append(name)

        facts: dict[str, Any] = {}
        for name in requested_tools:
            result = await self.registry.execute(name, tool_context=state["tool_context"])
            if result.is_success:
                facts[name] = {"status": "success", "data": self._bounded_data(result.data)}
            else:
                # 查询失败也属于可信流程事实，避免模型凭空说“已查到”。
                facts[name] = {"status": "error", "error": result.error[:300]}

        if not facts:
            return {"verified_facts": {}}
        facts_prompt = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        return {
            "verified_facts": facts,
            "workflow_prompt": (
                state.get("workflow_prompt", "") + "\n\n本轮已核验的业务事实（仅以此为准，不得编造）：\n" + facts_prompt
            ),
        }

    @staticmethod
    def _bounded_data(data: dict[str, Any]) -> dict[str, Any]:
        """限制可持久化事实大小，防止一条订单列表吞掉整个 Case。"""
        try:
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"summary": "工具返回了不可持久化的数据"}
        if len(encoded) <= 5000:
            return data
        # 当前订单工具的顶层列表可以安全裁成前三条；其余工具超限时只保留结果可用性，
        # 不将半截 JSON 当事实写入数据库。
        orders = data.get("orders")
        if isinstance(orders, list):
            reduced = {**data, "orders": orders[:3], "truncated": True}
            if len(json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))) <= 5000:
                return reduced
        return {"summary": "工具结果过长，需用明确订单或商品标识继续查询", "truncated": True}

    async def _run_agent_loop(self, state: SupportWorkflowState) -> dict[str, Any]:
        result = await self.agent.run(
            state["query"],
            context=state.get("context", ""),
            history=state.get("history", []),
            system_prompt_extra=state.get("workflow_prompt", ""),
            tool_context=state.get("tool_context"),
        )
        result.verified_facts = state.get("verified_facts", {})
        return {"result": result}

    async def run(
        self,
        query: str,
        *,
        context: str = "",
        history: list[dict[str, Any]] | None = None,
        system_prompt_extra: str = "",
        case_context: str = "",
        support_requests: list[dict[str, Any]] | None = None,
        tool_context: ToolContext | None = None,
    ) -> LoopResult:
        """执行一次复杂客服图；Case 在下一轮通过服务层恢复。"""
        state: SupportWorkflowState = {
            "query": query,
            "context": context,
            "history": history or [],
            "system_prompt_extra": system_prompt_extra,
            "case_context": case_context,
            "support_requests": support_requests or [],
            "tool_context": tool_context,
        }
        final_state = await self._graph.ainvoke(state)
        result = final_state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 未返回 Agent 结果")
        return result
