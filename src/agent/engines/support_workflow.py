"""复杂客户支持请求的 LangGraph 编排入口。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agent.customer_response import compose_customer_response
from agent.decision_context import context_facts_for_subject, merge_decision_contexts
from agent.engines.loop import AgentLoop, LoopResult, StepResult
from agent.order_subject_resolver import resolve_order_subject
from agent.support_control import (
    build_execution_plan,
    build_policy_envelope,
    clarification_required,
    completion_satisfied,
    confirmation_required,
    extract_decision_context,
    extract_decision_facts,
    partial_completion_satisfied,
    readiness_satisfied,
    validate_execution_plan,
)
from agent.support_subjects import match_subject_identity_choices
from agent.tools_registry import ToolContext, ToolRegistry
from service.checkout_refund_service import generate_customer_refund_entry

_workflow_logger = logging.getLogger(__name__)


def _log_order_subject_result(
    *,
    status: str,
    candidate_count: int,
    ambiguous_count: int = 0,
    resolved_subject: bool = False,
) -> None:
    """Log subject-resolution shape only; never log customer prose or order IDs."""
    _workflow_logger.info(
        "support order subject trace",
        extra={
            "support_event": "order_subject_resolution",
            "subject_status": status,
            "candidate_count": candidate_count,
            "ambiguous_count": ambiguous_count,
            "resolved_subject": resolved_subject,
        },
    )


class SupportWorkflowState(TypedDict, total=False):
    query: str
    history: list[dict[str, Any]]
    context: str
    system_prompt_extra: str
    case_context: str
    support_requests: list[dict[str, Any]]
    workflow_prompt: str
    policy_envelope: dict[str, Any]
    historical_contexts: list[dict[str, Any]]
    historical_explanation_facts: dict[str, Any]
    allow_historical_explanation: bool
    execution_plan: list[dict[str, Any]]
    required_decision_facts: list[str]
    completion_facts: list[str]
    plan_validation: dict[str, Any]
    replan_count: int
    selected_subjects: dict[str, Any]
    previous_subjects: dict[str, Any]
    product_subject_context: dict[str, Any]
    subject_relation: str
    verified_facts: dict[str, Any]
    decision_facts: dict[str, Any]
    decision_contexts: list[dict[str, Any]]
    tool_context: ToolContext | None
    operator_observations: list[dict[str, Any]]
    already_attempted_tools: list[str]
    recovery_count: int
    subject_bound_this_pass: bool
    subject_resolution_attempted: bool
    subject_resolution_status: str
    subject_choices: list[dict[str, Any]]
    selected_subject_label: str
    result: LoopResult


def _has_verified_precondition(facts: dict[str, Any], name: object) -> bool:
    """Check policy affordance prerequisites without treating missing facts as truthy."""
    if name == "authenticated_customer_context":
        return True
    return isinstance(name, str) and facts.get(name) not in (None, "")


class SupportWorkflowAgent:
    """复杂客服请求的业务图。

    图先注入持久化 Support Case 和业务契约，再由现有 AgentLoop 选择受限只读工具。
    Control Plane 不再把 WorkflowDefinition 当成自然语言请求的执行脚本；它只控制
    可见能力、完成条件、subject/ownership 与后置受控动作。
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
        graph.add_node("operator_loop", self._run_agent_loop)
        graph.add_node("absorb_observation", self._absorb_observation)
        graph.add_node("operator_recovery", self._operator_recovery)
        graph.add_node("controlled_action", self._apply_controlled_actions)
        graph.add_node("evaluate", self._evaluate)
        graph.add_node("finalize_response", self._finalize_response)
        graph.add_edge("prepare_case", "read_facts")
        graph.add_edge("read_facts", "operator_loop")
        graph.add_edge("operator_loop", "absorb_observation")
        graph.add_edge("absorb_observation", "controlled_action")
        graph.add_edge("controlled_action", "evaluate")
        graph.add_conditional_edges(
            "evaluate",
            self._route_after_evaluate,
            {"replan": "read_facts", "recovery": "operator_recovery", "finish": "finalize_response"},
        )
        # A bounded read is a Control Plane fact completion, not a customer
        # response.  Give the Operator one fresh turn with that trusted
        # observation before server-controlled actions and final composition.
        graph.add_edge("operator_recovery", "operator_loop")
        graph.add_edge("finalize_response", END)
        graph.set_entry_point("prepare_case")
        return graph.compile()

    @staticmethod
    async def _prepare_case(state: SupportWorkflowState) -> dict[str, Any]:
        """把服务端持久化 Case 作为可信上下文，而非让模型从长历史猜当前步骤。"""
        case_context = state.get("case_context", "")
        case_prompt = ""
        if case_context:
            current_selected_order = SupportWorkflowAgent._selected_order_id(state.get("selected_subjects"))
            subject_note = (
                "本轮 selected_subjects 已由服务端结构化交互验证并绑定，可作为当前订单身份；"
                if current_selected_order
                else "Case 摘要中的 selected_subjects 只是上一轮已验证的身份连续性上下文；"
                "本轮自然语言是否继续或切换订单必须以 OrderSubjectResolver + 当前客户候选的结果为准，"
                "不能因为 Case 里曾选中过某笔就继续粘住它；"
            )
            case_prompt = (
                "当前 Support Case（服务端持久化状态）：\n"
                f"{case_context}\n"
                f"{subject_note}"
                "request_stack/pending 只能按服务端状态机推进；用户本轮若改变目标，先识别差异后再处理。"
                "不要把客户自述当成已核验事实，也不得据此执行写操作。"
            )
        requests = state.get("support_requests", [])
        # The plan is authoritative only for required facts/capabilities.  The
        # Control Plane eagerly executes the safe current-order reads it can
        # satisfy without model-invented parameters; open-ended/tool-specific
        # inputs remain available to the Operator inside the same envelope.
        plan, required_facts, completion_facts = build_execution_plan(requests)
        plan_validation = validate_execution_plan(state.get("support_requests", []), plan)
        envelope = build_policy_envelope(requests)
        _workflow_logger.info(
            "support workflow plan trace",
            extra={
                "support_event": "workflow_plan",
                "support_request_count": len(requests),
                "execution_plan_count": len(plan),
                "planned_tools": [
                    str(step.get("tool") or "") for step in plan if isinstance(step, dict) and step.get("tool")
                ],
                "unsupported_workflow_count": len(envelope.unsupported_workflows),
            },
        )
        plan_prompt = ""
        if plan or envelope.unsupported_workflows:
            plan_prompt = (
                "本轮业务契约（服务端强制，不是工具执行顺序）：\n"
                f"允许的只读工具：{json.dumps(envelope.allowed_tools, ensure_ascii=False)}\n"
                f"完成所需事实：{json.dumps(envelope.completion_facts, ensure_ascii=False)}\n"
                f"事实/能力地图：{json.dumps(envelope.fact_affordances, ensure_ascii=False)}\n"
                f"当前不可用能力：{json.dumps(envelope.unavailable_capabilities, ensure_ascii=False)}\n"
                "Control Plane 会先取得无需模型补参数的 mandatory current facts；你只在这些事实仍不足、且"
                "确实需要语义参数时，从允许能力中补充合法只读查询。缺失事实只是下一步规划信号，绝不能"
                "直接念给客户。没有可信订单绑定时，track_order() 可仅查询当前客户订单；"
                "多笔结果不能由你自行挑选，服务端会根据用户原话解析或要求客户选择。"
                "工具真实结果才是交易事实；不要把客户自述、知识库一般规则或本契约当成当前订单事实。"
            )
            if envelope.unavailable_capabilities:
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
        selected_order_id = SupportWorkflowAgent._selected_order_id(state.get("selected_subjects"))
        # Explanation turns may deliberately keep the prior order as Resolver
        # context rather than a current binding.  Historical scoping may use
        # that server-verified previous identity, but it still never satisfies
        # current mutable-fact requirements.
        historical_subject_id = selected_order_id or SupportWorkflowAgent._selected_order_id(
            state.get("previous_subjects")
        )
        historical_explanation_facts: dict[str, Any] = {}
        if state.get("allow_historical_explanation") and historical_subject_id:
            # Historical transaction facts are never merged into current facts
            # or evaluator completion.  They are a narrowly scoped explanation
            # aid for a Router-classified follow-up such as “这是什么意思？”.
            historical_explanation_facts, _ = context_facts_for_subject(
                state.get("historical_contexts", []),
                historical_subject_id,
                provenance="historical",
            )
            if historical_explanation_facts:
                prompt_parts.append(
                    "上一轮同一订单的已核验事实（只可解释上一轮结论，不能表述为当前实时状态；"
                    "若引用请明确说明这是前面已经核验的结果）：\n"
                    + json.dumps(historical_explanation_facts, ensure_ascii=False, separators=(",", ":"))
                )
        return {
            "workflow_prompt": "\n\n".join(part for part in prompt_parts if part),
            "execution_plan": plan,
            "required_decision_facts": required_facts,
            "completion_facts": completion_facts,
            "plan_validation": plan_validation,
            "policy_envelope": {
                "allowed_tools": list(envelope.allowed_tools),
                "unavailable_capabilities": list(envelope.unavailable_capabilities),
                "unsupported_workflows": list(envelope.unsupported_workflows),
                "write_confirmation_required": envelope.write_confirmation_required,
                "fact_affordances": list(envelope.fact_affordances),
            },
            "historical_explanation_facts": historical_explanation_facts,
        }

    async def _read_facts(self, state: SupportWorkflowState) -> dict[str, Any]:
        """Acquire mandatory current facts before the Operator explains them.

        ``track_order`` may first act as authenticated discovery.  Once a
        server-owned candidate is resolved, all subject-sensitive reads are
        rebound to that verified order in the same Control Plane pass.  Mutable
        transaction facts are read for the current turn and are never satisfied
        by historical Case/session observations.
        """
        if self.registry is None or state.get("tool_context") is None:
            return {}

        # Phase-A rollout is intentionally narrow.  Connecting ``read_facts``
        # must not silently change payment/after-sales/delivery orchestration
        # that has not gone through the same real-conversation acceptance gate.
        # Those workflows keep their existing Operator path for now; Order and
        # Refund are the only domains whose mandatory reads are promoted here.
        eager_workflow_prefixes = ("order.", "refund.")
        allowed = {"track_order", "query_refund_status"}
        requested_tools: list[str] = []
        for step in state.get("execution_plan", []):
            name = step.get("tool") if isinstance(step, dict) else None
            workflow = str(step.get("workflow") or "") if isinstance(step, dict) else ""
            if (
                workflow.startswith(eager_workflow_prefixes)
                and isinstance(name, str)
                and name in allowed
                and name not in requested_tools
            ):
                requested_tools.append(name)

        _workflow_logger.info(
            "support workflow read plan trace",
            extra={
                "support_event": "workflow_read_plan",
                "requested_tools": list(requested_tools),
                "registry_present": self.registry is not None,
                "tool_context_present": state.get("tool_context") is not None,
            },
        )

        facts: dict[str, Any] = dict(state.get("verified_facts", {}))
        decision_facts: dict[str, Any] = dict(state.get("decision_facts", {}))
        decision_contexts = merge_decision_contexts(state.get("decision_contexts", []))
        selected = dict(state.get("selected_subjects", {}))
        previously_selected = self._selected_order_id(selected)
        resolution_status = str(state.get("subject_resolution_status") or "")
        narrowed_choices = list(state.get("subject_choices", []))
        resolution_attempted = bool(state.get("subject_resolution_attempted"))
        selected_subject_label = str(state.get("selected_subject_label") or "")
        attempted = list(state.get("already_attempted_tools", []))

        explicit_order_id = self._explicit_order_id(state.get("query", ""))
        selected_order_id = previously_selected
        read_tool_context = state["tool_context"]
        if selected_order_id:
            read_tool_context = replace(
                read_tool_context,
                selected_order_id=selected_order_id,
                require_bound_subject=True,
            )
        skip_paid_refund_reads = False

        def absorb_tool_result(
            name: str, result: Any, *, requested_order_id: str | None = None
        ) -> dict[str, Any] | None:
            nonlocal decision_contexts
            if not result.is_success:
                facts[name] = {"status": "error", "error": str(result.error or "")[:300]}
                return None
            bounded = self._bounded_data(result.data)
            facts[name] = {"status": "success", "data": bounded}
            decision_facts.update(result.decision_facts or extract_decision_facts(name, bounded))
            result_contexts = result.decision_contexts or []
            if not result_contexts:
                context = extract_decision_context(name, bounded, requested_order_id=requested_order_id)
                result_contexts = [context] if context else []
            decision_contexts = merge_decision_contexts(decision_contexts, result_contexts)
            return bounded

        for name in requested_tools:
            # A completed current-turn read is not repeated during a replan.
            if name in facts:
                continue
            if skip_paid_refund_reads and name == "query_refund_status":
                continue

            tool_arguments: dict[str, Any] = {}
            requested_order_id: str | None = None
            if name == "track_order":
                requested_order_id = selected_order_id or explicit_order_id or None
                if requested_order_id:
                    tool_arguments["order_id"] = requested_order_id
            else:
                # Subject-sensitive reads never run until discovery + resolver
                # has produced one server-validated order binding.
                if not selected_order_id:
                    continue
                requested_order_id = selected_order_id
                if name in {"query_refund_status", "check_payment_status"}:
                    tool_arguments["order_id"] = selected_order_id

            result = await self.registry.execute(
                name,
                **tool_arguments,
                tool_context=read_tool_context,
            )
            if name not in attempted:
                attempted.append(name)
            bounded = absorb_tool_result(name, result, requested_order_id=requested_order_id)
            _workflow_logger.info(
                "support workflow read result trace",
                extra={
                    "support_event": "workflow_read_result",
                    "tool": name,
                    "status": str(result.status or ""),
                    "requested_subject_present": bool(requested_order_id),
                    "candidate_count": (
                        len(self._pending_order_choices(facts)) if name == "track_order" and bounded is not None else 0
                    ),
                },
            )
            if bounded is None:
                continue

            if name == "track_order":
                choices = self._pending_order_choices(facts)
                # A typed order id is still untrusted until the authenticated
                # track_order result proves that exact subject belongs here.
                if not selected_order_id and explicit_order_id:
                    unique = self._unique_order_id(bounded)
                    if unique == explicit_order_id:
                        selected_order_id = unique
                        selected["order_id"] = unique
                        resolution_status = "resolved"
                        resolution_attempted = True

                if not selected_order_id and choices:
                    resolved_id, resolved_status, resolved_choices = await self._resolve_order_choices(state, choices)
                    _log_order_subject_result(
                        status=resolved_status or "unknown",
                        candidate_count=len(choices),
                        ambiguous_count=len(resolved_choices),
                        resolved_subject=bool(resolved_id),
                    )
                    resolution_attempted = True
                    resolution_status = resolved_status
                    narrowed_choices = resolved_choices
                    if resolved_id:
                        selected_order_id = resolved_id
                        selected["order_id"] = resolved_id

                if selected_order_id:
                    read_tool_context = replace(
                        state["tool_context"],
                        selected_order_id=selected_order_id,
                        require_bound_subject=True,
                    )
                    selected_candidate = next(
                        (choice for choice in choices if choice.get("order_id") == selected_order_id),
                        None,
                    )
                    if isinstance(selected_candidate, dict):
                        label = selected_candidate.get("product_name")
                        if isinstance(label, str) and label.strip():
                            selected_subject_label = label.strip()
                        candidate_facts = extract_decision_facts("track_order", selected_candidate)
                        if candidate_facts:
                            decision_facts.update(candidate_facts)
                        candidate_context = extract_decision_context(
                            "track_order",
                            selected_candidate,
                            requested_order_id=selected_order_id,
                        )
                        if candidate_context:
                            decision_contexts = merge_decision_contexts(decision_contexts, [candidate_context])

                if decision_facts.get("order_status") == "PENDING_PAYMENT" and any(
                    str(item.get("domain") or "") == "refund"
                    and str(item.get("operation") or "") in {"request", "eligibility"}
                    for item in state.get("support_requests", [])
                    if isinstance(item, dict)
                ):
                    skip_paid_refund_reads = True

        planned_tools = {
            str(step.get("tool"))
            for step in state.get("execution_plan", [])
            if isinstance(step, dict)
            and str(step.get("workflow") or "").startswith(eager_workflow_prefixes)
            and isinstance(step.get("tool"), str)
        }
        if (
            "check_refund_eligibility" in planned_tools
            and "check_refund_eligibility" not in facts
            and selected_order_id
            and decision_facts.get("order_status") != "PENDING_PAYMENT"
            and ("query_refund_status" not in planned_tools or decision_facts.get("refund_status") == "NOT_FOUND")
        ):
            result = await self.registry.execute(
                "check_refund_eligibility",
                order_id=selected_order_id,
                tool_context=read_tool_context,
            )
            if "check_refund_eligibility" not in attempted:
                attempted.append("check_refund_eligibility")
            absorb_tool_result("check_refund_eligibility", result, requested_order_id=selected_order_id)

        # ``read_facts`` stops at authoritative reads.  ``refund_entry`` is a
        # server-owned action/URL projection and therefore has exactly one
        # execution owner: ``_apply_controlled_actions`` after the Operator
        # turn.  Keeping that boundary here prevents read planning from also
        # becoming an action engine.

        facts_prompt = json.dumps(facts, ensure_ascii=False, separators=(",", ":")) if facts else ""
        update: dict[str, Any] = {
            "verified_facts": facts,
            "decision_facts": decision_facts,
            "decision_contexts": decision_contexts,
            "selected_subjects": selected,
            "already_attempted_tools": attempted[-16:],
            "subject_bound_this_pass": not previously_selected and bool(selected_order_id),
            "subject_resolution_attempted": resolution_attempted,
            "subject_resolution_status": resolution_status,
            "subject_choices": narrowed_choices,
            "selected_subject_label": selected_subject_label,
        }
        if facts_prompt:
            update["workflow_prompt"] = (
                state.get("workflow_prompt", "")
                + "\n\n本轮 Control Plane 已核验的当前业务事实（仅以此为准，不得编造；历史事实不在此处）：\n"
                + facts_prompt
            )
        return update

    @staticmethod
    def _bounded_data(data: dict[str, Any]) -> dict[str, Any]:
        """限制可持久化事实大小，防止一条订单列表吞掉整个 Case。"""
        try:
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"summary": "工具返回了不可持久化的数据"}
        if len(encoded) <= 5000:
            return data
        # 当前订单工具的顶层列表可以安全裁成前十条；其余工具超限时只保留结果可用性，
        # 不将半截 JSON 当事实写入数据库。
        orders = data.get("orders")
        if isinstance(orders, list):
            reduced = {**data, "orders": orders[:10], "truncated": True}
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
        if not isinstance(track_data, dict):
            return []
        orders = track_data.get("orders")
        if not isinstance(orders, list):
            return []
        choices: list[dict[str, Any]] = []
        for order in orders:
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
            recency_rank = order.get("recency_rank")
            if isinstance(recency_rank, int) and not isinstance(recency_rank, bool) and recency_rank > 0:
                choice["recency_rank"] = recency_rank
            if isinstance(product_name, str) and product_name.strip():
                choice["product_name"] = product_name.strip()
            if isinstance(amount_cents, int | float) and not isinstance(amount_cents, bool):
                choice["amount_cents"] = int(amount_cents)
            if isinstance(items, list):
                choice["items"] = [
                    {
                        key: item[key]
                        for key in (
                            "product_name",
                            "catalog_category",
                            "catalog_product_id",
                            "component_category",
                        )
                        if isinstance(item, dict) and isinstance(item.get(key), str)
                    }
                    for item in items[:10]
                    if isinstance(item, dict)
                ]
            choices.append(choice)
        return choices

    async def _resolve_order_choices(
        self,
        state: SupportWorkflowState,
        choices: list[dict[str, Any]],
    ) -> tuple[str, str, list[dict[str, Any]]]:
        """Resolve one authenticated order candidate without Router authority.

        Deterministic identity matching may prove a unique candidate.  All
        relative/semantic cases are delegated to ``OrderSubjectResolver``, which
        sees only server-owned candidate refs plus the previous verified subject
        as context.  Ambiguity is preserved rather than resolved by recency or
        list order unless the customer's own message explicitly provides that
        evidence.
        """
        if not choices:
            return "", "unknown", []
        requires_singular_subject = any(
            str(item.get("domain") or "") in {"refund", "delivery", "payment", "after_sales"}
            or (str(item.get("domain") or "") == "order" and str(item.get("operation") or "") != "list")
            for item in state.get("support_requests", [])
            if isinstance(item, dict)
        )
        if not requires_singular_subject:
            return "", "", []

        query = str(state.get("query", ""))
        previous_order_id = self._selected_order_id(state.get("previous_subjects"))
        product_subject_context = state.get("product_subject_context")
        if not isinstance(product_subject_context, dict):
            product_subject_context = {}
        matches = match_subject_identity_choices(query, choices)
        # With no previous subject, a unique identity match is already a
        # deterministic proof.  With a previous subject, however, expressions
        # such as "另一部 iPhone" may still identity-match the old order; the
        # Resolver must decide continuation vs switch from the full candidate
        # frame instead of letting that lexical match become authority.
        if len(matches) == 1 and not previous_order_id:
            candidate_id = matches[0].get("order_id")
            if isinstance(candidate_id, str) and candidate_id.startswith("SO"):
                return candidate_id, "resolved", []

        # Preserve the existing narrow lexical frame for ordinary service
        # turns.  Only the documented Ecommerce -> Service handoff needs the
        # complete authenticated frame: a secondary goal in the same utterance
        # may otherwise cause lexical matching to remove the order that the
        # recent canonical product context is helping the Resolver interpret.
        # The product context is never itself an order binding.
        resolution_pool = (
            choices if previous_order_id or product_subject_context else (matches if len(matches) > 1 else choices)
        )
        _workflow_logger.info(
            "support order subject input trace",
            extra={
                "support_event": "order_subject_input",
                "candidate_count": len(choices),
                "identity_match_count": len(matches),
                "resolution_pool_count": len(resolution_pool),
                "previous_subject_present": bool(previous_order_id),
                "product_context_present": bool(product_subject_context),
            },
        )
        previous_ref = next(
            (
                f"order_candidate_{index}"
                for index, candidate in enumerate(resolution_pool, start=1)
                if candidate.get("order_id") == previous_order_id
            ),
            "",
        )
        llm = getattr(self.agent, "llm", None)
        if llm is not None:
            resolution = await resolve_order_subject(
                llm,
                query,
                resolution_pool,
                previous_subject_ref=previous_ref,
                recent_product_context=product_subject_context,
            )
            if resolution.status == "resolved":
                ref_to_choice = {
                    f"order_candidate_{index}": candidate for index, candidate in enumerate(resolution_pool, start=1)
                }
                candidate = ref_to_choice.get(resolution.selected_ref)
                candidate_id = candidate.get("order_id") if isinstance(candidate, dict) else None
                if isinstance(candidate_id, str) and candidate_id.startswith("SO"):
                    return candidate_id, "resolved", []
            elif resolution.status == "ambiguous":
                refs = set(resolution.ambiguous_refs)
                narrowed = [
                    candidate
                    for index, candidate in enumerate(resolution_pool, start=1)
                    if f"order_candidate_{index}" in refs
                ]
                if len(narrowed) >= 2:
                    return "", "ambiguous", narrowed

        # A deterministic identity match with multiple candidates proves the
        # ambiguity even when the semantic resolver is unavailable/uncertain.
        if len(matches) > 1:
            return "", "ambiguous", list(matches)
        # The Resolver owns semantic continuation/switch interpretation, but a
        # valid ``unknown`` result must not erase identity evidence the Server
        # can already prove from its authenticated candidate set.  If the
        # current utterance uniquely identifies a *different* candidate than
        # the previous subject, binding that candidate is a deterministic
        # reference-validation fallback, not a second semantic router.
        #
        # Deliberately do not apply this to the previous subject itself: a
        # phrase such as "另一部 iPhone16" may lexical-match the old order while
        # semantically asking to switch.  In that case unresolved/clarification
        # is safer than silently sticking to the previous order.
        if len(matches) == 1 and previous_order_id:
            candidate_id = matches[0].get("order_id")
            if isinstance(candidate_id, str) and candidate_id.startswith("SO") and candidate_id != previous_order_id:
                return candidate_id, "resolved", []
        # Test doubles and degraded environments may not provide an LLM.  A
        # sole authenticated order is then the only safe fallback; with a real
        # resolver, an explicit conflicting description can still return unknown.
        if llm is None and len(choices) == 1:
            candidate_id = choices[0].get("order_id")
            if isinstance(candidate_id, str) and candidate_id.startswith("SO"):
                return candidate_id, "resolved", []
        return "", "unknown", []

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
                "上一轮执行没有满足目标。请重新检查业务契约、可信 Tool observation、"
                "尚缺事实和当前允许能力，自行决定是否补做合法只读查询；"
                "如果仍无法核验，请明确说明阻塞原因，"
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
        if replan_count or state.get("operator_observations"):
            previous_result = state.get("result")
            previous_progress = (
                previous_result.workflow_progress
                if isinstance(previous_result, LoopResult) and isinstance(previous_result.workflow_progress, dict)
                else {}
            )
            operator_state = {
                "server_bound_order_id": bound_order_id or None,
                "trusted_decision_facts": state.get("decision_facts", {}),
                "missing_facts_for_current_goal": previous_progress.get("missing_facts", []),
                "unavailable_capabilities": previous_progress.get("unavailable_capabilities", []),
                "already_attempted_tools": state.get("already_attempted_tools", []),
            }
            replan_prompt = (
                replan_prompt
                + "\n同一次处理的当前受信执行状态（只用于下一步规划，不是客户文案）：\n"
                + json.dumps(operator_state, ensure_ascii=False, separators=(",", ":"))
                + "\n已绑定订单只能使用服务端给出的这一笔；若仍缺少事实，请从事实/能力地图中选择合法只读能力，"
                "不要把内部 missing_facts 原样回复客户。"
            )
        if isinstance(agent_tool_context, ToolContext):
            envelope_tools = {
                str(name) for name in state.get("policy_envelope", {}).get("allowed_tools", []) if isinstance(name, str)
            }
            # A customer support Workflow never expands the caller's global
            # capability set.  The envelope only narrows the already authorised
            # tool schemas exposed to the LLM.
            caller_allowed = agent_tool_context.allowed_tools
            allowed_tools = envelope_tools if caller_allowed is None else envelope_tools & set(caller_allowed)
            # Keep subject-sensitive reads unavailable until the observation
            # control node binds a server-owned candidate.  This must be true
            # even during the first Operator turn: a model must not call
            # track_order() and query another order in the same unbound batch.
            agent_tool_context = replace(
                agent_tool_context,
                allowed_tools=frozenset(allowed_tools),
                selected_order_id=bound_order_id or None,
                require_bound_subject=True,
            )
        observation_context = state.get("operator_observations", [])
        observation_prompt = ""
        if observation_context:
            observation_prompt = (
                "同一次客服处理内已经发生的可信工具观察（不要重复 discovery，也不得把其中订单号自行换绑）：\n"
                + json.dumps(observation_context[-8:], ensure_ascii=False, separators=(",", ":"))
            )
        result = await self.agent.run(
            state["query"],
            context=state.get("context", ""),
            history=state.get("history", []),
            system_prompt_extra="\n\n".join(
                part for part in (state.get("workflow_prompt", ""), observation_prompt, replan_prompt) if part
            ),
            tool_context=agent_tool_context,
        )
        result.verified_facts = {
            **state.get("verified_facts", {}),
            **result.verified_facts,
        }
        result.decision_facts = {
            **state.get("decision_facts", {}),
            **result.decision_facts,
        }
        result.decision_contexts = merge_decision_contexts(
            state.get("decision_contexts", []),
            result.decision_contexts,
        )
        # This flag belongs to the previous absorb node only.  Clearing it here
        # prevents a single subject binding from causing an unbounded replan loop.
        return {
            "result": result,
            "replan_count": replan_count + 1,
            "subject_bound_this_pass": False,
        }

    async def _absorb_observation(self, state: SupportWorkflowState) -> dict[str, Any]:
        """Normalize any remaining Operator observation into server-owned state.

        Mandatory transactional discovery normally happens in ``_read_facts``.
        This node is the bounded fallback for an Operator-permitted read that was
        not already completed there.  A model-supplied order id never becomes a
        binding: only a server-owned candidate/decision context can do so.
        """
        result = state.get("result")
        if not isinstance(result, LoopResult):
            return {}
        facts = {**state.get("verified_facts", {}), **result.verified_facts}
        decision_facts = {
            **state.get("decision_facts", {}),
            **result.decision_facts,
        }
        contexts = merge_decision_contexts(state.get("decision_contexts", []), result.decision_contexts)
        # AgentLoop deliberately keeps the raw, bounded ToolResult payload and
        # does not make every individual tool implement the same fact-extraction
        # boilerplate.  The observation boundary is the authoritative place to
        # normalize those trusted payloads for Control Plane evaluation.  This
        # is especially important after a subject is bound: the second Operator
        # turn must not leave query_refund_status/check_refund_eligibility as
        # empty ``decision_facts`` merely because the tool returned a valid
        # domain payload without precomputed projections.
        requested_by_tool: dict[str, str] = {}
        for step in result.steps:
            for call in step.tool_calls or []:
                tool_name = str(getattr(call, "name", call))
                arguments = getattr(call, "arguments", {})
                requested_order_id = arguments.get("order_id") if isinstance(arguments, dict) else None
                if isinstance(requested_order_id, str) and requested_order_id.startswith("SO"):
                    requested_by_tool[tool_name] = requested_order_id
        for tool_name, raw in result.verified_facts.items():
            if not isinstance(raw, dict) or raw.get("status") != "success":
                continue
            data = raw.get("data")
            if not isinstance(data, dict):
                continue
            bounded = self._bounded_data(data)
            derived_facts = extract_decision_facts(tool_name, bounded)
            if derived_facts:
                decision_facts.update(derived_facts)
            requested_order_id = requested_by_tool.get(tool_name)
            if not requested_order_id:
                requested_order_id = self._selected_order_id(state.get("selected_subjects")) or None
            context = extract_decision_context(
                tool_name,
                bounded,
                requested_order_id=requested_order_id,
            )
            if context:
                contexts = merge_decision_contexts(contexts, [context])
        result.decision_facts = dict(decision_facts)
        result.decision_contexts = contexts
        selected = dict(state.get("selected_subjects", {}))
        choices = self._pending_order_choices(facts)
        narrowed_choices = list(state.get("subject_choices", []))
        resolution_status = str(state.get("subject_resolution_status") or "")
        selected_candidate: dict[str, Any] | None = None
        product_label: object = ""
        order_id = self._selected_order_id(selected)
        if not order_id and choices and not state.get("subject_resolution_attempted"):
            resolved_id, resolved_status, resolved_choices = await self._resolve_order_choices(state, choices)
            _log_order_subject_result(
                status=resolved_status or "unknown",
                candidate_count=len(choices),
                ambiguous_count=len(resolved_choices),
                resolved_subject=bool(resolved_id),
            )
            resolution_status = resolved_status
            narrowed_choices = resolved_choices
            if resolved_id:
                selected["order_id"] = resolved_id
        resolution_attempted = bool(state.get("subject_resolution_attempted")) or (
            bool(choices) and resolution_status in {"resolved", "ambiguous", "unknown"}
        )
        if not order_id and not self._selected_order_id(selected):
            # A precise order lookup can return a subject-bound decision context
            # without the list-shaped ``orders`` payload.  It is still a
            # server-owned observation, so it may bind one subject; the model
            # itself never becomes the authority.
            context_subjects = {
                str(item.get("subject_id"))
                for item in contexts
                if isinstance(item, dict)
                and isinstance(item.get("subject_id"), str)
                and str(item.get("subject_id")).startswith("SO")
            }
            if len(context_subjects) == 1:
                selected["order_id"] = next(iter(context_subjects))
        selected_order_id = self._selected_order_id(selected)
        if selected_order_id and choices:
            # A multi-order discovery result intentionally has no singular
            # context.  Once the server-side resolver has matched one
            # server-owned candidate, project only that row into a
            # subject-bound context.  This preserves the current order/status
            # facts already returned by ``track_order`` without allowing the
            # model to choose or invent an order id.
            track_data = facts.get("track_order", {}).get("data", {})
            track_orders = track_data.get("orders") if isinstance(track_data, dict) else None
            selected_candidate = next(
                (
                    candidate
                    for candidate in track_orders or []
                    if isinstance(candidate, dict)
                    if candidate.get("order_id") == selected_order_id
                ),
                None,
            )
            if selected_candidate is not None:
                product_label = selected_candidate.get("product_name")
                candidate_facts = extract_decision_facts("track_order", selected_candidate)
                candidate_context = extract_decision_context(
                    "track_order",
                    selected_candidate,
                    requested_order_id=selected_order_id,
                )
                if candidate_facts:
                    decision_facts.update(candidate_facts)
                if candidate_context:
                    contexts = merge_decision_contexts(contexts, [candidate_context])
        # The graph state carries these values to the next node, but the same
        # LoopResult is also consumed by the API persistence and customer
        # response boundaries.  Keep both views synchronized after subject
        # projection; otherwise a real multi-order discovery binds the order
        # while the API still sees only the non-subject listing facts.
        result.decision_facts = dict(decision_facts)
        result.decision_contexts = contexts
        subject_bound = not order_id and bool(self._selected_order_id(selected))
        observations: list[dict[str, Any]] = list(state.get("operator_observations", []))
        for step in result.steps:
            for call in step.tool_calls or []:
                tool_name = getattr(call, "name", call)
                observations.append(
                    {
                        "tool": str(tool_name),
                        "observation": str(step.observation or "")[:1200],
                    }
                )
        attempted_calls = [
            str(getattr(call, "name", call)) for step in result.steps for call in (step.tool_calls or [])
        ]
        attempted = list(dict.fromkeys([*state.get("already_attempted_tools", []), *attempted_calls]))
        return {
            "verified_facts": facts,
            "decision_facts": decision_facts,
            "decision_contexts": contexts,
            "selected_subjects": selected,
            "operator_observations": observations[-12:],
            "already_attempted_tools": attempted[-16:],
            "subject_bound_this_pass": subject_bound,
            "subject_resolution_attempted": resolution_attempted,
            "subject_resolution_status": resolution_status,
            "subject_choices": narrowed_choices,
            "selected_subject_label": str(product_label).strip() if selected_candidate and product_label else "",
        }

    @staticmethod
    def _safe_recovery_request(
        state: SupportWorkflowState,
        result: LoopResult,
    ) -> tuple[str, dict[str, Any]] | None:
        """Return one bounded read fallback after the Operator had a real turn.

        Recovery is deliberately derived from the policy affordance map and the
        current server-bound subject.  It may recover a missed read, but it can
        never invent an order id, choose among candidates, or invoke a write.
        """
        progress = result.workflow_progress if isinstance(result.workflow_progress, dict) else {}
        if progress.get("goal_status") != "unresolved":
            return None
        if int(state.get("recovery_count", 0)) >= 1:
            return None
        attempted = {str(name) for name in state.get("already_attempted_tools", [])}
        allowed = {
            str(name) for name in state.get("policy_envelope", {}).get("allowed_tools", []) if isinstance(name, str)
        }
        decision_facts = progress.get("decision_facts")
        if not isinstance(decision_facts, dict):
            decision_facts = {}
        selected_order_id = SupportWorkflowAgent._selected_order_id(state.get("selected_subjects"))
        effective_facts = dict(decision_facts)
        if selected_order_id:
            # A selected subject is server-owned state; it satisfies the
            # subject precondition for a bounded read without fabricating a
            # transaction fact such as status or amount.
            effective_facts.setdefault("order_identified", True)
        missing_facts = {str(fact) for fact in progress.get("missing_facts", []) if isinstance(fact, str)}
        if progress.get("reason") == "needs_subject_discovery":
            # A subject-sensitive tool may have been proposed before the
            # Operator discovered a customer-owned order.  Discovery is the
            # only safe recovery here; it does not authorize the failed tool
            # or bind any order from the model's arguments.
            missing_facts.add("order_identified")
        safe_subject_reads = {
            "query_refund_status",
            "check_payment_status",
            "check_refund_eligibility",
            "track_order",
        }
        for affordance in state.get("policy_envelope", {}).get("fact_affordances", []):
            if not isinstance(affordance, dict):
                continue
            capability = affordance.get("capability")
            if (
                not isinstance(capability, str)
                or capability not in safe_subject_reads
                or capability not in allowed
                or capability in attempted
                or affordance.get("available") is not True
                or affordance.get("read_only") is not True
                or affordance.get("server_controlled") is True
                or affordance.get("fact") not in missing_facts
                or not all(
                    _has_verified_precondition(effective_facts, precondition)
                    for precondition in affordance.get("preconditions", [])
                )
            ):
                continue
            if capability == "track_order":
                return capability, ({"order_id": selected_order_id} if selected_order_id else {})
            if selected_order_id:
                return capability, {"order_id": selected_order_id}
        return None

    async def _operator_recovery(self, state: SupportWorkflowState) -> dict[str, Any]:
        """Execute at most one safe, read-only affordance after Operator turns."""
        result = state.get("result")
        tool_context = state.get("tool_context")
        if not isinstance(result, LoopResult) or not isinstance(tool_context, ToolContext) or self.registry is None:
            return {}
        recovery = self._safe_recovery_request(state, result)
        if recovery is None:
            return {}
        capability, arguments = recovery
        selected_order_id = self._selected_order_id(state.get("selected_subjects"))
        recovery_tool_context = tool_context
        if selected_order_id:
            # Subject resolution is server-owned state.  Recovery must execute
            # with the same binding enforced for the Operator's next turn;
            # otherwise Registry correctly rejects the read as subject_not_bound.
            recovery_tool_context = replace(
                tool_context,
                selected_order_id=selected_order_id,
                require_bound_subject=True,
            )
        recovered = await self.registry.execute(
            capability,
            **arguments,
            tool_context=recovery_tool_context,
        )
        if recovered.is_success:
            data = self._bounded_data(recovered.data)
            result.verified_facts[capability] = {"status": "success", "data": data}
            result.decision_facts.update(recovered.decision_facts or extract_decision_facts(capability, data))
            contexts = recovered.decision_contexts
            if not contexts:
                context = extract_decision_context(capability, data, requested_order_id=arguments.get("order_id"))
                contexts = [context] if context else []
            result.decision_contexts = merge_decision_contexts(result.decision_contexts, contexts)
            observation = f"[{capability} 结果] bounded read recovery"
        else:
            result.verified_facts[capability] = {"status": "error", "error": recovered.error[:300]}
            observation = f"[{capability} 错误] {recovered.error[:300]}"
        result.steps.append(StepResult(step=len(result.steps) + 1, tool_calls=[], observation=observation))
        observations = [
            *state.get("operator_observations", []),
            {"tool": capability, "observation": observation[:1200]},
        ]
        return {
            "result": result,
            # Recovery facts are trusted Tool observations.  Promote them into
            # graph state before the fresh Operator turn so planning sees the
            # completed fact instead of the pre-recovery snapshot.
            "verified_facts": {**state.get("verified_facts", {}), **result.verified_facts},
            "decision_facts": {**state.get("decision_facts", {}), **result.decision_facts},
            "decision_contexts": merge_decision_contexts(
                state.get("decision_contexts", []),
                result.decision_contexts,
            ),
            "already_attempted_tools": [*state.get("already_attempted_tools", []), capability],
            "operator_observations": observations[-12:],
            "recovery_count": int(state.get("recovery_count", 0)) + 1,
        }

    async def _apply_controlled_actions(self, state: SupportWorkflowState) -> dict[str, Any]:
        """Apply server-controlled transitions after, never before, operator reasoning.

        ``generate_refund_entry`` is intentionally not an LLM tool: it is a
        trusted navigation projection that may exist only after current,
        subject-bound eligibility facts have been obtained.  It does not create
        a refund or call a payment provider.
        """
        result = state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 缺少 Operator 结果")
        requests = [item for item in state.get("support_requests", []) if isinstance(item, dict)]
        needs_entry = any(
            str(item.get("domain") or "") == "refund" and str(item.get("operation") or "") == "request"
            for item in requests
        )
        if not needs_entry:
            return {"result": result}
        facts = self._bound_decision_facts(state, result)
        selected_order_id = self._selected_order_id(state.get("selected_subjects"))
        if not selected_order_id:
            contexts = merge_decision_contexts(result.decision_contexts)
            subject_ids = {
                str(item.get("subject_id"))
                for item in contexts
                if isinstance(item, dict) and isinstance(item.get("subject_id"), str)
            }
            if len(subject_ids) == 1:
                selected_order_id = next(iter(subject_ids))
        tool_context = state.get("tool_context")
        if (
            not selected_order_id
            or facts.get("refund_status") != "NOT_FOUND"
            or facts.get("refund_eligibility") is not True
            or facts.get("order_status") == "PENDING_PAYMENT"
            or not isinstance(tool_context, ToolContext)
        ):
            return {"result": result}
        entry = await generate_customer_refund_entry(
            customer_user_id=tool_context.user_id,
            order_no=selected_order_id,
            eligibility_already_verified=True,
        )
        if not isinstance(entry, str) or not re.fullmatch(r"\?page=orders&refund_order=SO[A-Z0-9_-]+", entry):
            result.verified_facts["generate_refund_entry"] = {
                "status": "unavailable",
                "error": "当前无法生成可用的官方退款入口，请重新在订单页核验",
            }
            return {"result": result}
        data = {"order_id": selected_order_id, "refund_entry": entry}
        result.verified_facts["generate_refund_entry"] = {"status": "success", "data": data}
        result.decision_facts.update(extract_decision_facts("generate_refund_entry", data))
        context = extract_decision_context("generate_refund_entry", data, requested_order_id=selected_order_id)
        if context:
            result.decision_contexts = merge_decision_contexts(result.decision_contexts, [context])
        return {"result": result}

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
        """评估业务状态；中间轮不得提前改写客户最终文案。"""
        result = state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 缺少可评估的执行结果")
        result.workflow_progress = SupportWorkflowAgent._evaluate_progress(state, result)
        return {"result": result}

    @staticmethod
    async def _finalize_response(state: SupportWorkflowState) -> dict[str, Any]:
        """Only the terminal workflow state may compose customer-visible prose."""
        result = state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 缺少可渲染的执行结果")
        compose_customer_response(
            result,
            state.get("support_requests", []),
            current_contexts=result.decision_contexts,
            historical_contexts=state.get("historical_contexts", []),
            selected_subjects=state.get("selected_subjects", {}),
        )
        return {"result": result}

    @staticmethod
    async def _route_after_evaluate(state: SupportWorkflowState) -> str:
        """最多重规划一次；反复缺事实时交给 API 的失败/等待边界处理。"""
        result = state.get("result")
        if not isinstance(result, LoopResult):
            return "finish"
        progress = result.workflow_progress
        if (
            state.get("subject_bound_this_pass") is True
            and progress.get("goal_status") in {"unresolved", "blocked"}
            and int(state.get("replan_count", 0)) < 3
        ):
            # Subject control is an observation boundary.  Once it binds a
            # server-owned candidate, give the Operator a fresh turn with the
            # bound ToolContext before exposing any missing-fact limitation.
            return "replan"
        if (
            progress.get("goal_status") == "unresolved"
            and int(state.get("replan_count", 0)) >= 1
            and SupportWorkflowAgent._safe_recovery_request(state, result) is not None
        ):
            return "recovery"
        if (
            progress.get("goal_status") == "unresolved"
            and int(state.get("recovery_count", 0)) > 0
            and int(state.get("replan_count", 0)) < 3
        ):
            return "replan"
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
        if selected.startswith("SO"):
            # The selected id is already a server-owned, ownership-verified
            # subject.  It satisfies the subject precondition even when the
            # discovery observation was a multi-order listing and therefore
            # intentionally produced no subject-bound context.  This does not
            # infer any transaction fact such as status, amount, or eligibility.
            facts = {**facts, "order_identified": True}
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
        facts = {
            **state.get("verified_facts", {}),
            **result.verified_facts,
        }
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
        pending_choices = list(state.get("subject_choices", [])) or SupportWorkflowAgent._pending_order_choices(facts)
        selected_order_id = SupportWorkflowAgent._selected_order_id(state.get("selected_subjects"))
        needs_singular_subject = any(
            str(item.get("domain") or "") in {"refund", "delivery", "payment", "after_sales"}
            or (str(item.get("domain") or "") == "order" and str(item.get("operation") or "") != "list")
            for item in requests
        )
        subject_resolution_status = str(state.get("subject_resolution_status") or "")
        subject_unknown = needs_singular_subject and not selected_order_id and subject_resolution_status == "unknown"
        selection_required = (
            needs_singular_subject
            and not selected_order_id
            and subject_resolution_status != "unknown"
            and len(pending_choices) > 1
        )
        explicit_order_id = SupportWorkflowAgent._explicit_order_id(state.get("query", ""))
        explicit_order_lookup_failed = bool(explicit_order_id and facts.get("track_order", {}).get("status") == "error")
        subject_binding_failed = any(
            isinstance(item, dict) and item.get("status") == "error" and item.get("error") == "subject_not_bound"
            for item in facts.values()
        ) or any("subject_not_bound" in str(step.observation or "") for step in result.steps)
        actions_taken = ["read_fact:" + tool for tool in successful_tools]
        actions_taken.extend("tool:" + tool for tool in observed_successes | observed_failures)

        requires_customer = confirmation_required(requests)
        needs_clarification = clarification_required(requests)
        is_complete = completion_satisfied(requests, decision_facts)
        is_partial = partial_completion_satisfied(requests, decision_facts)
        is_ready = readiness_satisfied(requests, decision_facts)
        historical_explanation_facts = state.get("historical_explanation_facts", {})
        historical_explanation_available = bool(
            state.get("allow_historical_explanation")
            and isinstance(historical_explanation_facts, dict)
            and historical_explanation_facts
            and all(
                str(item.get("domain") or "") == "refund"
                and str(item.get("operation") or "")
                in {"status", "expected_arrival", "processing_time", "anomaly", "destination", "eligibility"}
                for item in requests
            )
        )
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
        elif subject_unknown:
            goal_status = "awaiting_customer"
            next_action = "ASK_CLARIFICATION"
            reason = "subject_resolution_unknown"
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
        elif needs_singular_subject and not selected_order_id and subject_binding_failed:
            # ``subject_not_bound`` is an internal control rejection, not a
            # customer-facing business failure.  The Operator already had a
            # planning turn; let the bounded discovery recovery obtain the
            # current customer's candidates before evaluating refund/delivery
            # facts again.
            goal_status = "unresolved"
            next_action = "LOOKUP"
            reason = "needs_subject_discovery"
            next_actor = "SYSTEM" if int(state.get("replan_count", 0)) < 2 else "NONE"
        elif historical_explanation_available:
            # This is deliberately lower priority than a fresh selection or
            # explicit-order failure.  It only lets the Operator explain a
            # previously verified same-subject fact; it never satisfies a
            # dynamic status query or turns historical facts into current ones.
            goal_status = "resolved_with_limitation"
            next_action = "EXPLAIN_PREVIOUS_FACT"
            reason = "historical_fact_explanation"
            next_actor = "NONE"
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
                if decision_facts.get("order_cancel_supported") is True:
                    next_action = "SELF_SERVICE_ORDER_CANCEL"
                    reason = "pending_payment_has_no_refund"
                else:
                    next_action = "EXPLAIN_ORDER_CANCEL_UNAVAILABLE"
                    reason = "pending_payment_cancel_unavailable"
            elif pending_order_cancel:
                goal_status = "resolved"
                if decision_facts.get("order_cancel_supported") is True:
                    next_action = "SELF_SERVICE_ORDER_CANCEL"
                    reason = "pending_order_cancel_handoff"
                else:
                    next_action = "EXPLAIN_ORDER_CANCEL_UNAVAILABLE"
                    reason = "pending_order_cancel_unavailable"
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
        elif needs_singular_subject and not selected_order_id and pending_choices:
            goal_status = "awaiting_customer"
            next_action = "ASK_CHOICE"
            reason = "subject_not_resolved"
            next_actor = "CUSTOMER"
        elif any(decision_facts.get(fact) in (None, "") for fact in state.get("completion_facts", [])):
            # Tool-plan coverage is diagnostic only.  A missing completion fact
            # is recoverable when its policy affordance is an available,
            # read-only producer whose preconditions are already verified.
            recoverable = any(
                item.get("fact") in state.get("completion_facts", [])
                and item.get("available") is True
                and item.get("read_only") is True
                and item.get("server_controlled") is not True
                and all(
                    _has_verified_precondition(decision_facts, precondition)
                    for precondition in item.get("preconditions", [])
                )
                for item in state.get("policy_envelope", {}).get("fact_affordances", [])
                if isinstance(item, dict)
            )
            goal_status = "unresolved"
            next_action = "LOOKUP" if recoverable else "EXPLAIN_LIMITATION"
            reason = "recoverable_read_gap" if recoverable else "completion_criteria_not_satisfied"
            next_actor = "SYSTEM" if recoverable and int(state.get("replan_count", 0)) < 2 else "NONE"
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
            "selected_subjects": dict(state.get("selected_subjects", {})),
            "successful_tools": successful_tools,
            "failed_tools": failed_tools,
            "missing_facts": []
            if is_complete
            else [
                fact
                for fact in state.get("required_decision_facts", [])
                if decision_facts.get(fact) in (None, "")
                # In an explicitly Router-classified explanation turn, a
                # historical fact remains historical but is not “missing” for
                # explaining the previous answer.  Do keep genuinely absent
                # detail (for example ETA) visible as the limitation.
                and not (historical_explanation_available and fact in historical_explanation_facts)
            ],
            "missing_capabilities": [tool for tool in required_tools if tool not in successful_tools]
            + unavailable_capabilities,
            "unavailable_capabilities": list(dict.fromkeys(unavailable_capabilities + runtime_unavailable_tools)),
            "unsupported_workflows": unsupported_workflows,
            "readiness_satisfied": is_ready,
            "subject_resolution_status": subject_resolution_status,
            "selected_subject_label": state.get("selected_subject_label", ""),
            "historical_explanation": historical_explanation_available,
            "plan_validation": plan_validation,
            "pending_choices": pending_choices if selection_required else [],
            "actions_taken": actions_taken[:16],
            "recovery_count": int(state.get("recovery_count", 0)),
            "resolution_type": (
                "SELF_SERVICE_ORDER_CANCEL"
                if is_complete
                and decision_facts.get("order_cancel_supported") is True
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
                else "ORDER_CANCEL_UNAVAILABLE"
                if is_complete
                and decision_facts.get("order_status") == "PENDING_PAYMENT"
                and any(
                    str(request.get("domain") or "") == "order"
                    and str(request.get("operation") or "") == "cancel"
                    or str(request.get("domain") or "") == "refund"
                    and str(request.get("operation") or "") in {"request", "eligibility"}
                    for request in requests
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
        previous_subjects: dict[str, Any] | None = None,
        product_subject_context: dict[str, Any] | None = None,
        subject_relation: str = "unknown",
        tool_context: ToolContext | None = None,
        historical_contexts: list[dict[str, Any]] | None = None,
        allow_historical_explanation: bool = False,
    ) -> LoopResult:
        """执行一次复杂客服图；Case 在下一轮通过服务层恢复。"""
        selected = self._selected_order_id(selected_subjects or {})
        if selected and isinstance(tool_context, ToolContext):
            # ``selected_subjects`` reaches this boundary only for a current
            # server-validated binding (for example a structured UI choice).
            # Natural-language continuity is carried separately through
            # ``previous_subjects`` and must pass OrderSubjectResolver again.
            # Registry enforces the current binding for both deterministic
            # pre-reads and any later model-selected read.
            tool_context = replace(tool_context, selected_order_id=selected, require_bound_subject=True)
        elif isinstance(tool_context, ToolContext):
            tool_context = replace(tool_context, require_bound_subject=True)
        state: SupportWorkflowState = {
            "query": query,
            "context": context,
            "history": history or [],
            "system_prompt_extra": system_prompt_extra,
            "case_context": case_context,
            "support_requests": support_requests or [],
            "selected_subjects": selected_subjects or {},
            "previous_subjects": previous_subjects or {},
            "product_subject_context": product_subject_context or {},
            "subject_relation": subject_relation if subject_relation in {"same", "changed", "unknown"} else "unknown",
            "tool_context": tool_context,
            "historical_contexts": historical_contexts or [],
            "allow_historical_explanation": allow_historical_explanation,
        }
        final_state = await self._graph.ainvoke(state)
        result = final_state.get("result")
        if not isinstance(result, LoopResult):
            raise RuntimeError("客服 Workflow 未返回 Agent 结果")
        return result
