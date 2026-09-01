"""复杂客户支持请求的 LangGraph 编排入口。"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agent.customer_response import compose_customer_response
from agent.decision_context import context_facts_for_subject, merge_decision_contexts
from agent.engines.loop import AgentLoop, LoopResult
from agent.support_control import (
    build_execution_plan,
    clarification_required,
    completion_satisfied,
    confirmation_required,
    extract_decision_context,
    extract_decision_facts,
    partial_completion_satisfied,
    readiness_satisfied,
    validate_execution_plan,
)
from agent.tools_registry import ToolContext, ToolRegistry
from service.checkout_refund_service import generate_customer_refund_entry


class SupportWorkflowState(TypedDict, total=False):
    query: str
    history: list[dict[str, Any]]
    context: str
    system_prompt_extra: str
    case_context: str
    support_requests: list[dict[str, Any]]
    workflow_prompt: str
    execution_plan: list[dict[str, Any]]
    required_decision_facts: list[str]
    completion_facts: list[str]
    plan_validation: dict[str, Any]
    replan_count: int
    selected_subjects: dict[str, Any]
    verified_facts: dict[str, Any]
    decision_facts: dict[str, Any]
    decision_contexts: list[dict[str, Any]]
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
        graph.add_node("evaluate", self._evaluate)
        graph.add_edge("prepare_case", "read_facts")
        graph.add_edge("read_facts", "agent_loop")
        graph.add_edge("agent_loop", "evaluate")
        graph.add_conditional_edges(
            "evaluate",
            self._route_after_evaluate,
            {"replan": "agent_loop", "finish": END},
        )
        graph.set_entry_point("prepare_case")
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
        plan, required_facts, completion_facts = build_execution_plan(state.get("support_requests", []))
        plan_validation = validate_execution_plan(state.get("support_requests", []), plan)
        plan_prompt = ""
        if plan:
            plan_prompt = (
                "本轮结构化执行计划（只读事实优先，不能跳过前置条件，也不能把计划当成已执行结果）：\n"
                f"{json.dumps(plan, ensure_ascii=False, separators=(',', ':'))}\n"
                f"计划校验：{json.dumps(plan_validation, ensure_ascii=False, separators=(',', ':'))}\n"
                "每完成一步都要根据工具真实返回判断：已解决、仍缺事实、等待客户选择，或需要人工；"
                "不要把客户自述或计划中的 expected_result 当成事实。"
            )
            if plan_validation.get("unavailable_capabilities"):
                plan_prompt += (
                    "\n以下能力当前不可用。不得用其他字段、支付方式、知识库规则或常识推断缺失事实；"
                    "只能明确说明该事实当前无法核验。若已拿到足以回答当前状态的可信事实，"
                    "请先说明这些事实，再说明具体限制；不要把能力缺口说成已经转人工。"
                )
            if any(
                str(item.get("domain") or "") == "refund" and str(item.get("operation") or "") == "request"
                for item in state.get("support_requests", [])
                if isinstance(item, dict)
            ):
                plan_prompt += (
                    "\n退款申请原因由客户在订单页正式提交时填写；当前聊天阶段不要索要退款原因，"
                    "也不要把客户尚未提交的原因当作资格核验事实。"
                )
        prompt_parts = [state.get("system_prompt_extra", "").strip(), case_prompt, plan_prompt]
        return {
            "workflow_prompt": "\n\n".join(part for part in prompt_parts if part),
            "execution_plan": plan,
            "required_decision_facts": required_facts,
            "completion_facts": completion_facts,
            "plan_validation": plan_validation,
        }

    async def _read_facts(self, state: SupportWorkflowState) -> dict[str, Any]:
        """预先读取不需要模型补参数的客户范围事实。

        这一步只允许“当前登录客户 + 空参数”也安全的只读工具。商品库存、兼容性等
        需要精确商品/型号的查询仍由后续 Agent 明确收集信息后调用，不能把用户口述
        猜成订单或商品事实。
        """
        if self.registry is None or state.get("tool_context") is None:
            return {"verified_facts": {}, "decision_facts": {}}

        allowed = {"track_order", "check_after_sales", "check_payment_status", "query_refund_status"}
        requested_tools: list[str] = []
        for step in state.get("execution_plan", []):
            name = step.get("tool") if isinstance(step, dict) else None
            if isinstance(name, str) and name in allowed and name not in requested_tools:
                requested_tools.append(name)

        facts: dict[str, Any] = {}
        decision_facts: dict[str, Any] = {}
        decision_contexts: list[dict[str, Any]] = []
        bound_order_id = self._selected_order_id(state.get("selected_subjects"))
        explicit_order_id = SupportWorkflowAgent._explicit_order_id(state.get("query", ""))
        order_id_for_query = bound_order_id or explicit_order_id
        skip_paid_refund_reads = False
        for name in requested_tools:
            if skip_paid_refund_reads and name == "query_refund_status":
                continue
            tool_arguments: dict[str, Any] = {}
            if order_id_for_query and name in {"track_order", "query_refund_status", "check_payment_status"}:
                tool_arguments["order_id"] = order_id_for_query
            result = await self.registry.execute(
                name,
                **tool_arguments,
                tool_context=state["tool_context"],
            )
            if result.is_success:
                bounded = self._bounded_data(result.data)
                facts[name] = {"status": "success", "data": bounded}
                decision_facts.update(result.decision_facts or extract_decision_facts(name, bounded))
                result_contexts = result.decision_contexts or []
                if not result_contexts:
                    context = extract_decision_context(
                        name,
                        bounded,
                        requested_order_id=tool_arguments.get("order_id"),
                    )
                    result_contexts = [context] if context else []
                decision_contexts = merge_decision_contexts(decision_contexts, result_contexts)
                # ``track_order`` is deliberately the first read for an unbound
                # request.  Its unique result becomes the server-side subject for
                # subsequent refund reads in this same pass; do not leave the
                # later query subjectless and then lose its facts during binding.
                if not order_id_for_query and name == "track_order":
                    order_id_for_query = self._unique_order_id(bounded)
                # 对尚未付款的订单，退款申请/资格的正确客户结果是“没有已支付
                # 款项需要退款，可前往订单取消”。不再读取不存在的退款记录或
                # 进入已付款订单的退款资格路径。
                if (
                    name == "track_order"
                    and decision_facts.get("order_status") == "PENDING_PAYMENT"
                    and any(
                        str(item.get("domain") or "") == "refund"
                        and str(item.get("operation") or "") in {"request", "eligibility"}
                        for item in state.get("support_requests", [])
                        if isinstance(item, dict)
                    )
                ):
                    skip_paid_refund_reads = True
            else:
                # 查询失败也属于可信流程事实，避免模型凭空说“已查到”。
                facts[name] = {"status": "error", "error": result.error[:300]}

        # 有唯一订单时，退款资格是可安全自动读取的下一层事实；多订单或未知订单
        # 必须先等客户选择，不能把最近一笔订单代入资格判断。
        planned_tools = {
            str(step.get("tool"))
            for step in state.get("execution_plan", [])
            if isinstance(step, dict) and isinstance(step.get("tool"), str)
        }
        selected_order_id = bound_order_id or self._unique_order_id(facts.get("track_order", {}).get("data", {}))
        if (
            "check_refund_eligibility" in planned_tools
            and selected_order_id
            and decision_facts.get("order_status") != "PENDING_PAYMENT"
            and ("query_refund_status" not in planned_tools or decision_facts.get("refund_status") == "NOT_FOUND")
        ):
            result = await self.registry.execute(
                "check_refund_eligibility",
                order_id=selected_order_id,
                tool_context=state["tool_context"],
            )
            if result.is_success:
                bounded = self._bounded_data(result.data)
                facts["check_refund_eligibility"] = {"status": "success", "data": bounded}
                decision_facts.update(
                    result.decision_facts or extract_decision_facts("check_refund_eligibility", bounded)
                )
                result_contexts = result.decision_contexts or []
                if not result_contexts:
                    context = extract_decision_context(
                        "check_refund_eligibility",
                        bounded,
                        requested_order_id=selected_order_id,
                    )
                    result_contexts = [context] if context else []
                decision_contexts = merge_decision_contexts(decision_contexts, result_contexts)
            else:
                facts["check_refund_eligibility"] = {"status": "error", "error": result.error[:300]}

        # 退款申请是 self-service：Control Plane 只在本人订单、无既有退款且资格
        # 已核验通过时交付官方入口。它不创建退款，也不会把入口暴露为 LLM Tool。
        if (
            "generate_refund_entry" in planned_tools
            and selected_order_id
            and decision_facts.get("order_status") != "PENDING_PAYMENT"
            and decision_facts.get("refund_status") == "NOT_FOUND"
            and decision_facts.get("refund_eligibility") is True
        ):
            customer_user_id = getattr(state["tool_context"], "user_id", None)
            if isinstance(customer_user_id, int):
                entry = await generate_customer_refund_entry(
                    customer_user_id=customer_user_id,
                    order_no=selected_order_id,
                    eligibility_already_verified=True,
                )
                if isinstance(entry, str) and re.fullmatch(r"\?page=orders&refund_order=SO[A-Z0-9_-]+", entry):
                    data = {"order_id": selected_order_id, "refund_entry": entry}
                    facts["generate_refund_entry"] = {"status": "success", "data": data}
                    decision_facts.update(extract_decision_facts("generate_refund_entry", data))
                    context = extract_decision_context(
                        "generate_refund_entry",
                        data,
                        requested_order_id=selected_order_id,
                    )
                    if context:
                        decision_contexts = merge_decision_contexts(decision_contexts, [context])
                else:
                    facts["generate_refund_entry"] = {
                        # 入口服务未返回可验证的站内入口，或资格在二次核验时变化；
                        # 这不是成功结果，保留业务分类供 Control Plane fail closed。
                        "status": "unavailable",
                        "error": "当前无法生成可用的官方退款入口，请重新在订单页核验",
                    }
            else:
                facts["generate_refund_entry"] = {"status": "error", "error": "缺少当前客户身份"}

        if not facts:
            return {"verified_facts": {}, "decision_facts": {}, "decision_contexts": []}
        facts_prompt = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        return {
            "verified_facts": facts,
            "decision_facts": decision_facts,
            "decision_contexts": decision_contexts,
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

    @staticmethod
    def _unique_order_id(data: object) -> str:
        """只从唯一、无歧义的订单结果中取 checkout 订单号。"""
        if not isinstance(data, dict) or data.get("selection_required"):
            return ""
        order_id = data.get("order_id")
        if isinstance(order_id, str) and order_id.startswith("SO"):
            return order_id
        order_no = data.get("order_no")
        if isinstance(order_no, str) and order_no.startswith("SO"):
            return order_no
        orders = data.get("orders")
        if data.get("count") == 1 and isinstance(orders, list) and len(orders) == 1:
            candidate = orders[0]
            if isinstance(candidate, dict):
                order_id = candidate.get("order_id") or candidate.get("order_no")
                if isinstance(order_id, str) and order_id.startswith("SO"):
                    return order_id
        return ""

    @staticmethod
    def _selected_order_id(selected_subjects: object) -> str:
        """读取 Case 已确定的订单 subject；不从客户自由文本推断。"""
        if not isinstance(selected_subjects, dict):
            return ""
        order_id = selected_subjects.get("order_id")
        return order_id if isinstance(order_id, str) and order_id.startswith("SO") else ""

    @staticmethod
    def _pending_order_choices(facts: dict[str, Any]) -> list[dict[str, Any]]:
        """把本轮实际查询得到的候选裁成可展示、可持久化的稳定选择帧。"""
        track_data = facts.get("track_order", {}).get("data", {})
        if not isinstance(track_data, dict) or track_data.get("selection_required") is not True:
            return []
        orders = track_data.get("orders")
        if not isinstance(orders, list):
            return []
        choices: list[dict[str, Any]] = []
        for order in orders[:3]:
            if not isinstance(order, dict):
                continue
            order_id = order.get("order_id") or order.get("order_no")
            if not isinstance(order_id, str) or not order_id.startswith("SO"):
                continue
            items = order.get("items")
            product_name = order.get("product_name")
            if not product_name and isinstance(items, list) and items and isinstance(items[0], dict):
                product_name = items[0].get("product_name")
            amount_cents = order.get("amount_cents")
            if amount_cents is None:
                amount = order.get("total_amount")
                try:
                    amount_cents = round(float(amount) * 100) if amount is not None else None
                except (TypeError, ValueError):
                    amount_cents = None
            choice: dict[str, Any] = {"order_id": order_id}
            if isinstance(product_name, str) and product_name.strip():
                choice["product_name"] = product_name.strip()
            if isinstance(amount_cents, int | float) and not isinstance(amount_cents, bool):
                choice["amount_cents"] = int(amount_cents)
            choices.append(choice)
        return choices

    @staticmethod
    def _apply_customer_choice_boundary(result: LoopResult) -> None:
        """兼容旧调用方；实际渲染统一由 customer_response composer 完成。"""
        compose_customer_response(result)

    @staticmethod
    def _explicit_order_id(query: object) -> str:
        """提取客户当前句明确给出的商城订单号，不从历史或客户声明猜订单。"""
        if not isinstance(query, str):
            return ""
        match = re.search(r"SO[A-Z0-9_-]+", query.upper())
        return match.group(0) if match else ""

    async def _run_agent_loop(self, state: SupportWorkflowState) -> dict[str, Any]:
        replan_count = int(state.get("replan_count", 0))
        replan_prompt = ""
        if replan_count:
            replan_prompt = (
                "上一轮执行没有满足目标。请重新检查工具真实结果和结构化计划，"
                "只补做缺失的只读事实；如果仍无法核验，请明确说明阻塞原因，"
                "不要重复声称已完成。"
            )
        agent_tool_context = state.get("tool_context")
        bound_order_id = self._selected_order_id(state.get("selected_subjects"))
        if not bound_order_id:
            # 只有当前这一轮的受控读取形成了唯一 subject 时，才把它绑定到后续
            # AgentLoop。这样模型即使继续调用只读 Tool，也不能把可信订单换成另一笔；
            # 多 subject 或尚未选择时保持未绑定，继续由 Control Plane 要求客户选择。
            current_contexts = merge_decision_contexts(state.get("decision_contexts", []))
            context_subjects = {
                str(item.get("subject_id")) for item in current_contexts if isinstance(item.get("subject_id"), str)
            }
            if len(context_subjects) == 1:
                bound_order_id = next(iter(context_subjects))
        if bound_order_id and isinstance(agent_tool_context, ToolContext):
            agent_tool_context = replace(agent_tool_context, selected_order_id=bound_order_id)
        result = await self.agent.run(
            state["query"],
            context=state.get("context", ""),
            history=state.get("history", []),
            system_prompt_extra="\n\n".join(part for part in (state.get("workflow_prompt", ""), replan_prompt) if part),
            tool_context=agent_tool_context,
        )
        result.verified_facts = state.get("verified_facts", {})
        result.decision_facts = {
            **state.get("decision_facts", {}),
            **result.decision_facts,
        }
        result.decision_contexts = merge_decision_contexts(
            state.get("decision_contexts", []),
            result.decision_contexts,
        )
        result.workflow_progress = self._evaluate_progress(state, result)
        compose_customer_response(
            result,
            state.get("support_requests", []),
            current_contexts=result.decision_contexts,
            selected_subjects=state.get("selected_subjects", {}),
        )
        return {"result": result, "replan_count": replan_count + 1}

    @staticmethod
    def _replace_self_service_handoff(result: LoopResult) -> None:
        """兼容旧调用方；实际渲染统一由 customer_response composer 完成。"""
        compose_customer_response(result)

    @staticmethod
    def _apply_unavailable_capability_boundary(result: LoopResult) -> None:
        """兼容旧调用方；实际渲染统一由 customer_response composer 完成。"""
        compose_customer_response(result)

    @staticmethod
    def _apply_refund_fact_boundary(
        result: LoopResult,
        requests: list[dict[str, Any]],
        *,
        facts_override: dict[str, Any] | None = None,
        stale_context: bool = False,
    ) -> None:
        """兼容旧调用方；实际渲染统一由 customer_response composer 完成。"""
        current_facts = facts_override if isinstance(facts_override, dict) else result.decision_facts
        if not current_facts and isinstance(result.workflow_progress, dict):
            progress_facts = result.workflow_progress.get("decision_facts")
            if isinstance(progress_facts, dict):
                current_facts = progress_facts
        compose_customer_response(
            result,
            requests,
            current_contexts=result.decision_contexts,
            legacy_current_facts=current_facts,
        )

    @staticmethod
    async def _evaluate(state: SupportWorkflowState) -> dict[str, Any]:
        """保留独立评估节点，避免把模型返回文本直接当作完成信号。"""
        result = state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 缺少可评估的执行结果")
        return {"result": result}

    @staticmethod
    async def _route_after_evaluate(state: SupportWorkflowState) -> str:
        """最多重规划一次；反复缺事实时交给 API 的失败/等待边界处理。"""
        result = state.get("result")
        if not isinstance(result, LoopResult):
            return "finish"
        progress = result.workflow_progress
        if progress.get("goal_status") == "unresolved" and int(state.get("replan_count", 0)) < 2:
            return "replan"
        return "finish"

    @staticmethod
    def _bound_decision_facts(state: SupportWorkflowState, result: LoopResult) -> dict[str, Any]:
        """只返回一个已确定订单 subject 的 facts，拒绝跨订单 flat merge。"""
        contexts = merge_decision_contexts(
            state.get("decision_contexts", []),
            result.decision_contexts,
        )
        selected = SupportWorkflowAgent._selected_order_id(state.get("selected_subjects"))
        subject_ids = {str(item.get("subject_id")) for item in contexts if item.get("subject_id")}
        if not selected:
            if len(subject_ids) != 1:
                return (
                    {}
                    if len(subject_ids) > 1
                    else {
                        **state.get("decision_facts", {}),
                        **result.decision_facts,
                    }
                )
            selected = next(iter(subject_ids))
        facts, _ = context_facts_for_subject(contexts, selected)
        return facts

    @staticmethod
    def _evaluate_progress(state: SupportWorkflowState, result: LoopResult) -> dict[str, Any]:
        """在模型回答之后给出确定性的目标状态，不把“有回答”等同于已解决。"""
        requests = [item for item in state.get("support_requests", []) if isinstance(item, dict)]
        required_tools = [
            str(step.get("tool"))
            for step in state.get("execution_plan", [])
            if isinstance(step, dict) and isinstance(step.get("tool"), str) and step.get("available", True) is not False
        ]
        unavailable_capabilities = list(
            dict.fromkeys(
                str(step.get("tool"))
                for step in state.get("execution_plan", [])
                if isinstance(step, dict) and step.get("available") is False and isinstance(step.get("tool"), str)
            )
        )
        plan_validation = state.get("plan_validation", {})
        unsupported_workflows = list(plan_validation.get("unsupported_workflows", []))
        facts = state.get("verified_facts", {})
        decision_facts = SupportWorkflowAgent._bound_decision_facts(state, result)
        observed_successes: set[str] = set()
        observed_failures: set[str] = set()
        actions_taken: list[str] = []
        for step in result.steps:
            for call in step.tool_calls or []:
                tool_name = str(call.name)
                actions_taken.append("tool:" + tool_name)
                observation = str(step.observation or "")
                if f"[{tool_name} 结果]" in observation:
                    observed_successes.add(tool_name)
                elif f"[{tool_name} 错误]" in observation:
                    observed_failures.add(tool_name)

        successful_tools = [
            tool
            for tool in required_tools
            if facts.get(tool, {}).get("status") == "success" or tool in observed_successes
        ]
        failed_tools = [
            tool for tool in required_tools if facts.get(tool, {}).get("status") == "error" or tool in observed_failures
        ]
        runtime_unavailable_tools = [
            tool for tool in required_tools if facts.get(tool, {}).get("status") == "unavailable"
        ]
        selection_required = any(
            isinstance(item, dict)
            and item.get("status") == "success"
            and isinstance(item.get("data"), dict)
            and item["data"].get("selection_required") is True
            for item in facts.values()
        )
        explicit_order_id = SupportWorkflowAgent._explicit_order_id(state.get("query", ""))
        explicit_order_lookup_failed = bool(explicit_order_id and facts.get("track_order", {}).get("status") == "error")
        actions_taken = ["read_fact:" + tool for tool in successful_tools]
        actions_taken.extend("tool:" + tool for tool in observed_successes | observed_failures)

        requires_customer = confirmation_required(requests)
        needs_clarification = clarification_required(requests)
        is_complete = completion_satisfied(requests, decision_facts)
        is_partial = partial_completion_satisfied(requests, decision_facts)
        is_ready = readiness_satisfied(requests, decision_facts)
        if unsupported_workflows:
            # Router 的语义已通过白名单，但 Control Plane 没有对应 SOP 时，
            # 不能让空计划伪装成完成；必须把覆盖缺口暴露给 Case/客户边界。
            goal_status = "blocked"
            next_action = "EXPLAIN_LIMITATION_OR_HANDOFF"
            reason = "workflow_unsupported"
            next_actor = "NONE"
        elif needs_clarification:
            goal_status = "awaiting_customer"
            next_action = "ASK_CLARIFICATION"
            reason = "clarification_required"
            next_actor = "CUSTOMER"
        elif selection_required:
            # 工具已经明确返回多个候选；这不是工具失败，也不能让模型任选一条。
            goal_status = "awaiting_customer"
            next_action = "ASK_CHOICE"
            reason = "selection_required"
            next_actor = "CUSTOMER"
        elif explicit_order_lookup_failed:
            # 精确订单只在当前客户范围内查询。无结果时不透露资源是否存在，
            # 让客户核对本人订单；不能拿无关的本人订单事实完成本轮目标。
            goal_status = "awaiting_customer"
            next_action = "ASK_CUSTOMER_TO_CHECK_ORDER"
            reason = "order_not_found_or_not_owned"
            next_actor = "CUSTOMER"
        elif runtime_unavailable_tools or failed_tools or unavailable_capabilities:
            # A missing read capability is not itself a human handoff.  When a
            # Workflow explicitly declares a safe partial predicate, return the
            # verified facts plus a limitation and let the conversation continue.
            # Otherwise block the requested operation, but only offer—not create—
            # human escalation.  AWAITING_STAFF is reserved for a real ticket.
            gap_reason = "fact_tool_failed" if failed_tools else "capability_unavailable"
            if is_partial:
                goal_status = "resolved_with_limitation"
                next_action = "EXPLAIN_LIMITATION"
                reason = gap_reason
                next_actor = "NONE"
            else:
                goal_status = "blocked"
                next_action = "EXPLAIN_LIMITATION_OR_HANDOFF"
                reason = gap_reason
                next_actor = "NONE"
        elif is_complete:
            # Completion 以独立的业务事实谓词为准，而不是以计划中的每个能力是否
            # 都执行过为准。典型例子是 refund.request：如果已经查到既有退款记录，
            # 就应解释该退款状态，不能继续要求资格查询或生成新的申请入口。
            pending_payment_refund = decision_facts.get("order_status") == "PENDING_PAYMENT" and any(
                str(request.get("domain") or "") == "refund"
                and str(request.get("operation") or "") in {"request", "eligibility"}
                for request in requests
            )
            pending_order_cancel = (
                any(
                    str(request.get("domain") or "") == "order" and str(request.get("operation") or "") == "cancel"
                    for request in requests
                )
                and decision_facts.get("order_status") == "PENDING_PAYMENT"
            )
            if pending_payment_refund:
                goal_status = "resolved_with_explanation"
                next_action = "SELF_SERVICE_ORDER_CANCEL"
                reason = "pending_payment_has_no_refund"
            elif pending_order_cancel:
                goal_status = "resolved"
                next_action = "SELF_SERVICE_ORDER_CANCEL"
                reason = "pending_order_cancel_handoff"
            elif (
                decision_facts.get("exchange_eligibility") is False
                or decision_facts.get("price_protection_eligibility") is False
                or decision_facts.get("refund_eligibility") is False
                or decision_facts.get("refund_cancel_eligibility") is False
            ):
                goal_status = "resolved_with_explanation"
                next_action = "ANSWER"
                reason = "policy_result_denied_with_explanation"
            else:
                goal_status = "resolved"
                if any(
                    str(request.get("domain") or "") == "refund" and str(request.get("operation") or "") == "request"
                    for request in requests
                ) and isinstance(decision_facts.get("refund_entry"), str):
                    next_action = "SELF_SERVICE_HANDOFF"
                    reason = "refund_entry_delivered"
                else:
                    next_action = "ANSWER"
                    reason = "completion_criteria_satisfied"
            next_actor = "NONE"
        elif required_tools and len(successful_tools) < len(required_tools):
            goal_status = "unresolved"
            next_action = "LOOKUP"
            reason = "required_fact_missing"
            next_actor = "SYSTEM" if int(state.get("replan_count", 0)) == 0 else "NONE"
        elif requires_customer and is_ready:
            goal_status = "awaiting_confirmation"
            next_action = "AWAITING_CONFIRMATION"
            reason = "readiness_criteria_satisfied"
            next_actor = "CUSTOMER"
        elif requests:
            goal_status = "unresolved"
            next_action = "LOOKUP_OR_EXPLAIN_LIMIT"
            reason = "completion_criteria_not_satisfied"
            next_actor = "SYSTEM" if int(state.get("replan_count", 0)) == 0 else "NONE"
        elif "[ESCALATE]" in result.answer:
            goal_status = "blocked"
            next_action = "EXPLAIN_LIMITATION_OR_HANDOFF"
            reason = "model_requested_escalation"
            next_actor = "NONE"
        else:
            goal_status = "resolved"
            next_action = "ANSWER"
            reason = "workflow_completion_facts_available"
            next_actor = "NONE"

        control_state = {
            "blocked": "BLOCKED",
            "unresolved": "NEED_FACT",
            "awaiting_customer": "AWAITING_CUSTOMER",
            "awaiting_confirmation": "AWAITING_CONFIRMATION",
            "resolved": "RESOLVED",
            "resolved_with_explanation": "RESOLVED",
            "resolved_with_limitation": "RESOLVED_WITH_LIMITATION",
        }.get(goal_status, "READY_TO_EXECUTE")

        return {
            "goal": next(
                (str(item.get("desired_outcome") or item.get("operation") or "customer_support") for item in requests),
                "customer_support",
            ),
            "goal_status": goal_status,
            "control_state": control_state,
            "next_action": next_action,
            "next_actor": next_actor,
            "reason": reason,
            "required_tools": required_tools,
            "required_decision_facts": state.get("required_decision_facts", []),
            "completion_facts": state.get("completion_facts", []),
            "decision_facts": decision_facts,
            "decision_contexts": merge_decision_contexts(
                state.get("decision_contexts", []),
                result.decision_contexts,
            ),
            "successful_tools": successful_tools,
            "failed_tools": failed_tools,
            "missing_facts": []
            if is_complete
            else [fact for fact in state.get("required_decision_facts", []) if decision_facts.get(fact) in (None, "")],
            "missing_capabilities": [tool for tool in required_tools if tool not in successful_tools]
            + unavailable_capabilities,
            "unavailable_capabilities": list(dict.fromkeys(unavailable_capabilities + runtime_unavailable_tools)),
            "unsupported_workflows": unsupported_workflows,
            "readiness_satisfied": is_ready,
            "plan_validation": plan_validation,
            "pending_choices": SupportWorkflowAgent._pending_order_choices(facts),
            "actions_taken": actions_taken[:16],
            "resolution_type": (
                "SELF_SERVICE_ORDER_CANCEL"
                if is_complete
                and (
                    (
                        decision_facts.get("order_status") == "PENDING_PAYMENT"
                        and any(
                            str(request.get("domain") or "") == "order"
                            and str(request.get("operation") or "") == "cancel"
                            for request in requests
                        )
                    )
                    or (
                        decision_facts.get("order_status") == "PENDING_PAYMENT"
                        and any(
                            str(request.get("domain") or "") == "refund"
                            and str(request.get("operation") or "") in {"request", "eligibility"}
                            for request in requests
                        )
                    )
                )
                else "SELF_SERVICE_HANDOFF"
                if is_complete
                and any(
                    str(request.get("domain") or "") == "refund" and str(request.get("operation") or "") == "request"
                    for request in requests
                )
                and isinstance(decision_facts.get("refund_entry"), str)
                else ""
            ),
        }

    async def run(
        self,
        query: str,
        *,
        context: str = "",
        history: list[dict[str, Any]] | None = None,
        system_prompt_extra: str = "",
        case_context: str = "",
        support_requests: list[dict[str, Any]] | None = None,
        selected_subjects: dict[str, Any] | None = None,
        tool_context: ToolContext | None = None,
    ) -> LoopResult:
        """执行一次复杂客服图；Case 在下一轮通过服务层恢复。"""
        selected = self._selected_order_id(selected_subjects or {})
        if selected and isinstance(tool_context, ToolContext):
            # Case subject is authoritative for every read in this workflow,
            # including the deterministic pre-read phase.  Do not wait until
            # AgentLoop to add the binding: Registry must enforce the same
            # order mismatch guard for _read_facts as it does for model calls.
            tool_context = replace(tool_context, selected_order_id=selected)
        state: SupportWorkflowState = {
            "query": query,
            "context": context,
            "history": history or [],
            "system_prompt_extra": system_prompt_extra,
            "case_context": case_context,
            "support_requests": support_requests or [],
            "selected_subjects": selected_subjects or {},
            "tool_context": tool_context,
        }
        final_state = await self._graph.ainvoke(state)
        result = final_state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 未返回 Agent 结果")
        return result
