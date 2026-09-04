import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from agent.customer_presentation import (
    CustomerPresentation,
    SubjectChoiceInteraction,
    build_customer_presentation,
)
from agent.customer_response import compose_customer_response
from agent.decision_context import (
    SUBJECT_CONTEXT_RESET_MARKER,
    context_facts_for_subject,
    historicalize_decision_contexts,
    merge_decision_contexts,
)
from agent.engines.loop import LoopResult
from agent.evidence import resolve_evidence
from agent.llm.intent_router import Intent, IntentRouter, build_route_instruction
from agent.llm.resolve import resolve_stock_follow_up
from agent.llm.sentiment import build_escalation_prompt, detect_sentiment
from agent.order_subject_resolver import resolve_order_subject
from agent.product_context import (
    attach_candidate_frame,
    filter_candidates_by_context,
    ordinal_choice_ref,
    product_context_entity,
    product_context_from_entities,
    set_choice_refs,
    update_product_context,
)
from agent.product_identity import (
    canonical_product_candidate,
    canonical_product_identity,
    dedupe_product_candidates,
    match_product_candidate,
    stored_product_identity,
)
from agent.product_resolver import ProductResolution, ProductResolver, candidate_for_ref
from agent.rag.knowledge_context import format_knowledge_context
from agent.rag.retrieve import hybrid_search, pre_retrieve_knowledge
from agent.subject_correction import (
    SubjectCorrection,
    detect_subject_correction,
    resolve_subject_description,
)
from agent.support_control import confirmation_required
from agent.support_subjects import (
    MAX_ORDER_CHOICE_OPTIONS,
    looks_like_bare_subject_description,
    looks_like_pending_subject_choice,
    match_pending_subject_choices,
    match_subject_identity_choices,
    resolve_pending_subject_choice,
)
from agent.tools_registry import ToolContext
from config import settings
from exceptions import DependencyUnavailableError, LLMError
from infra.redis_client import get_redis
from log_config import get_request_id
from service.customer_support_policy import (
    CustomerSupportAction,
    decide_customer_support_action,
)
from service.support_case_service import SupportCaseService
from service.ticket_escalation import TicketEscalationReason, classify_ticket_escalation
from store.checkout_store import list_customer_checkout_orders
from store.product_catalog_store import build_public_product_context, get_product_detail, list_product_candidates
from store.support_case_store import SupportCase
from store.ticket_store import enqueue_human_ticket

_chat_logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    # 结构化 subject_choice 可以不带自由文本；普通消息仍必须带非空 query。
    query: str = Field(default="", max_length=2000, description="用户消息")
    session_id: str | None = Field(None, description="不传则自动创建新会话")
    replace_from_sequence: int | None = Field(
        None,
        ge=0,
        description="内部兼容字段；客户聊天不允许编辑已发送消息",
    )
    product_category: Literal["laptops", "phones", "components"] | None = None
    product_id: str | None = Field(default=None, min_length=1, max_length=128)
    interaction: SubjectChoiceInteraction | None = None

    @model_validator(mode="after")
    def validate_input(self) -> "ChatRequest":
        has_query = bool(self.query.strip())
        if self.interaction is None and not has_query:
            raise ValueError("query 不能为空")
        if self.interaction is not None and has_query:
            raise ValueError("interaction 不能与 query 同时提交")
        return self

    @property
    def history_content(self) -> str:
        """为结构化点击生成可读的客户历史文本，不把它送回 Router 猜测。"""
        if self.query.strip():
            return self.query
        if self.interaction is not None:
            return f"已选择订单：{self.interaction.subject_id}"
        return ""


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    total_steps: int
    total_tokens: int
    presentation: CustomerPresentation | None = None


chat_router = APIRouter(prefix="/api/v1", tags=["聊天"])

_STREAM_ERROR_MESSAGE = "智能服务暂时不可用，请稍后重试"
_CHAT_RUN_TTL_SECONDS = 300
_CUSTOMER_CHAT_READ_TOOLS = frozenset(
    {
        "search_product",
        "search_knowledge",
        "check_stock",
        "track_order",
        "check_payment_status",
        "query_refund_status",
        "check_refund_eligibility",
        "check_after_sales",
        "compare_products",
        "present_product_candidates",
        "search_component",
    }
)


def _semantic_hint_payload(
    *,
    resolved_query: str,
    entities: dict[str, Any],
    explicit_product: dict[str, str] | None = None,
) -> str:
    """Build non-authoritative hints for the Router without giving it ProductResolver authority."""
    payload: dict[str, Any] = {}
    if resolved_query and resolved_query != entities.get("_raw_query"):
        payload["resolved_reference_hint"] = resolved_query[:2000]
    product_context = product_context_from_entities(entities)
    if product_context:
        payload["ecommerce_context"] = {
            key: product_context.get(key)
            for key in ("category", "min_price_cents", "max_price_cents", "target_price_cents", "brand_keys")
            if product_context.get(key) not in (None, "", [])
        }
        if product_context.get("choice_refs"):
            # Router may know a product choice is pending so a bare ordinal is
            # routed back to Ecommerce Role, but it never sees or selects refs.
            payload["product_choice_pending"] = True
    selected_hint = explicit_product or stored_product_identity(entities)
    if selected_hint is not None:
        payload["selected_product_context"] = {
            key: selected_hint[key]
            for key in ("product", "product_category", "component_category")
            if selected_hint.get(key)
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) if payload else ""


async def _route_intent_with_hints(
    router: IntentRouter,
    raw_query: str,
    *,
    history: list[dict[str, Any]] | None = None,
    case_context: str = "",
    knowledge_context: str = "",
    semantic_hints: str = "",
) -> Intent:
    """Call real and test routers while preserving the raw-query contract."""
    kwargs: dict[str, Any] = {
        "history": history,
        "case_context": case_context,
        "knowledge_context": knowledge_context,
    }
    try:
        signature = inspect.signature(router.route)
        accepts_hints = "semantic_hints" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_hints = False
    if accepts_hints:
        kwargs["semantic_hints"] = semantic_hints
    return await router.route(raw_query, **kwargs)


def _finalize_intent_channels(
    intent: Intent,
    *,
    raw_query: str,
    retrieval_query: str,
    semantic_hints: str,
) -> Intent:
    """Attach explicit query channels to both real and compatibility Intents."""
    intent.raw_query = raw_query
    intent.retrieval_query = retrieval_query or raw_query
    intent.semantic_hints = {"raw": semantic_hints} if semantic_hints else {}
    return intent


def _attach_answer_trace(
    result: LoopResult,
    intent: Intent,
    *,
    raw_query: str,
    retrieval_query: str,
    tool_context: ToolContext,
) -> None:
    """Persist bounded internal provenance for diagnosing semantic authority."""
    catalog_acquired = bool((result.answer_trace or {}).get("catalog_acquired"))
    mode = str((result.response_control or {}).get("mode") or "")
    # The response layer is authoritative for an already validated fallback or
    # controlled response.  This helper only observes/derives a source for the
    # ordinary Operator result; it must never overwrite a stronger source.
    if result.answer_source not in {
        "DETERMINISTIC_FALLBACK",
        "OPERATOR_VALIDATED",
        "CLARIFICATION",
        "CONTROLLED_ACTION",
    }:
        if mode in {"ASK_CHOICE", "ASK_CLARIFICATION", "SELF_SERVICE_HANDOFF", "CONTROLLED_ACTION"}:
            result.answer_source = "CONTROLLED_ACTION" if mode == "CONTROLLED_ACTION" else "CLARIFICATION"
        elif mode in {"FACT", "EXPLANATION", "READ_ONLY", "RESOLVED_WITH_LIMITATION"}:
            result.answer_source = "OPERATOR_VALIDATED"
        else:
            result.answer_source = "OPERATOR"
    actual_tools = [
        str(call.name) for step in result.steps for call in (step.tool_calls or []) if getattr(call, "name", None)
    ]
    progress = result.workflow_progress if isinstance(result.workflow_progress, dict) else {}
    # A bounded Control Plane recovery is executed outside the final AgentLoop
    # result, so the terminal ``steps`` list alone can under-report real reads.
    # Reuse the Workflow evaluator's trusted capability ledger instead of
    # inventing a second tracing mechanism.
    for name in [*progress.get("successful_tools", []), *progress.get("failed_tools", [])]:
        if isinstance(name, str) and name != "generate_refund_entry" and name not in actual_tools:
            actual_tools.append(name)
    selected_subjects = progress.get("selected_subjects", {})
    selected_subject = (
        selected_subjects.get("order_id")
        if isinstance(selected_subjects, dict) and isinstance(selected_subjects.get("order_id"), str)
        else tool_context.selected_order_id
    )
    pending_candidates = [
        {key: choice[key] for key in ("order_id", "product_name", "recency_rank") if key in choice}
        for choice in progress.get("pending_choices", [])[:12]
        if isinstance(choice, dict)
    ]
    decision_facts = progress.get("decision_facts", {})
    refs = list(getattr(intent, "subject_refs", []) or [])
    for request in intent.workflow_requests:
        refs.extend(request.subject_refs)
    result.answer_trace = {
        "raw_query": raw_query[:2000],
        "retrieval_query": retrieval_query[:2000],
        "route_source": intent.route_source,
        "domain": intent.domain,
        "operation": intent.operation,
        "speech_act": intent.speech_act,
        "case_update": intent.case_update,
        "subject_relation": intent.subject_relation,
        "fact_scope": intent.fact_scope,
        "selected_candidate_ref": next(
            (ref for ref in refs if re.fullmatch(r"candidate_[1-9][0-9]*", ref)),
            None,
        ),
        "allowed_tools": sorted(tool_context.allowed_tools or ()),
        "actual_tool_calls": actual_tools[:32],
        "subject_resolution_status": progress.get("subject_resolution_status"),
        "selected_subject": selected_subject,
        "subject_candidates": pending_candidates,
        "required_facts": list(progress.get("required_decision_facts", []))[:32],
        "known_facts": sorted(decision_facts)[:64] if isinstance(decision_facts, dict) else [],
        "missing_facts": list(progress.get("missing_facts", []))[:32],
        "failed_capabilities": list(progress.get("failed_tools", []))[:32],
        "goal_status": progress.get("goal_status"),
        "workflow_reason": progress.get("reason"),
        "next_action": progress.get("next_action"),
        "next_actor": progress.get("next_actor"),
        "automatic_fact_completion": int(progress.get("recovery_count") or 0),
        "response_control_mode": mode or "GENERIC",
        "answer_source": result.answer_source,
    }
    if catalog_acquired:
        result.answer_trace["catalog_acquired"] = True


def _goal_relevant_tools(intent: Intent, *, canonical_product: bool = False) -> frozenset[str] | None:
    """Return a semantic narrowing set; ``None`` means keep policy defaults."""
    requests = intent.support_requests
    keys = {(request.domain, request.operation) for request in requests}
    if not keys and intent.domain and intent.operation:
        keys.add((intent.domain, intent.operation))
    if not keys:
        return None
    if ("product", "purchase") in keys:
        # Purchase is not an inventory lookup.  Once the server has a canonical
        # selected product, no tool is needed to provide the navigation action.
        # Without one, keep only product discovery so “我想买 iPhone16” can bind
        # a real catalog item; check_stock remains unavailable unless inventory
        # is the Router-owned Goal.
        if canonical_product:
            return frozenset()
        return frozenset({"search_product", "search_component", "present_product_candidates"})
    relevant: set[str] = set()
    for domain, operation in keys:
        if domain == "product":
            relevant.update(
                {
                    "search_product",
                    "search_component",
                    "compare_products",
                    "search_knowledge",
                    "present_product_candidates",
                }
            )
        elif domain == "inventory":
            relevant.update({"check_stock", "search_product", "search_component"})
        elif domain in {"order", "delivery"}:
            relevant.add("track_order")
        elif domain == "payment":
            relevant.update({"track_order", "check_payment_status"})
        elif domain == "refund":
            relevant.update({"track_order", "query_refund_status", "check_refund_eligibility", "check_payment_status"})
        elif domain == "after_sales":
            relevant.update({"track_order", "check_after_sales"})
    return frozenset(relevant)


def _operator_tool_context(
    tool_context: ToolContext,
    intent: Intent,
    *,
    canonical_product: bool = False,
) -> ToolContext:
    """Intersect semantic relevance with the already-authorized policy set."""
    # SupportWorkflow already derives its authoritative capability set from
    # PolicyEnvelope.  Applying the generic semantic filter here as well can
    # remove a producer needed for bounded fact completion before the workflow
    # gets its operator turn.
    if intent.use_workflow:
        return tool_context
    relevant = _goal_relevant_tools(intent, canonical_product=canonical_product)
    if relevant is None or tool_context.allowed_tools is None:
        return tool_context
    return replace(tool_context, allowed_tools=frozenset(tool_context.allowed_tools) & relevant)


def _ecommerce_operator_tool_context(
    tool_context: ToolContext,
    intent: Intent,
    *,
    canonical_product: bool,
    catalog_bound: bool,
    candidate_refs: Sequence[str] = (),
) -> ToolContext:
    """Keep catalog acquisition in the Ecommerce Procedure, not the Operator loop.

    The Operator may declare an ordered presentation over opaque candidate refs,
    but cannot search a second catalog once the server has already formed the
    authoritative candidate frame.
    """
    narrowed = _operator_tool_context(tool_context, intent, canonical_product=canonical_product)
    narrowed = replace(
        narrowed,
        product_candidate_refs=frozenset(str(ref) for ref in candidate_refs if str(ref)),
    )
    if not catalog_bound or intent.domain != "product" or narrowed.allowed_tools is None:
        return narrowed
    return replace(
        narrowed,
        allowed_tools=frozenset(narrowed.allowed_tools) - {"search_product", "search_component", "compare_products"},
    )


def _product_entity_from_intent(
    intent: Intent,
    query: str,
    entities: dict[str, Any],
    explicit_product: dict[str, str] | None,
) -> dict[str, str] | None:
    """Return only already-canonical product continuity.

    Candidate selection belongs to ProductResolver + server validation.  The
    Router and Operator prose are not product-subject authorities.
    """
    del query
    if explicit_product is not None:
        return explicit_product
    if intent.domain == "product" and intent.operation == "purchase":
        return stored_product_identity(entities)
    return None


def _compose_prompt_extras(*parts: str) -> str:
    """合并受控的 Agent 提示片段，避免空段落污染模型上下文。"""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _customer_chat_capability_context(
    tool_context: ToolContext,
    *,
    canonical_product: bool,
    recommended_product_actions: bool = False,
) -> str:
    """Expose the real Customer Chat capability boundary to the Operator."""
    if tool_context.role != "customer":
        return ""
    readable = sorted(tool_context.allowed_tools or ())
    snapshot = {
        "read_capabilities": readable,
        "server_actions": {
            "product_detail_navigation": canonical_product or recommended_product_actions,
            "recommended_product_navigation": recommended_product_actions,
            "orders_navigation": True,
            "create_order": False,
            "write_shipping_address": False,
            "select_payment_method": False,
            "submit_checkout": False,
            "pay_on_behalf_of_customer": False,
        },
    }
    return (
        "Customer Chat 当前能力快照（服务端可信；只描述能力，不是业务事实）：\n"
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        + "\n如果 product_detail_navigation=true，服务端一定会在本轮最终回答附上真实商品详情入口；"
        "此时不得声称无法提供/生成商品链接，也不要让客户再去目录里手动搜索。"
        "recommended_product_navigation=true 表示当前候选帧支持推荐详情入口；当你实际推荐商品时，"
        "必须先调用 present_product_candidates(mode=recommend, candidate_refs=[...]) 声明你将展示的候选及顺序，"
        "服务端随后会为这些候选附上真实详情入口；这不等于客户已经选中商品。"
        "orders_navigation=true 表示服务端可以提供当前登录客户的订单页入口。"
        "但不要自己生成 URL。create_order/address/payment/checkout 能力为 false 时，不得索要这些信息，"
        "也不得声称会代客户下单或付款。具体当前在售商品只能来自本轮 catalog/tool 观察或服务端已选商品。"
    )


def _selected_product_operator_context(
    selected_product: dict[str, str] | None,
    entities: dict[str, Any],
) -> str:
    """Describe a server-promoted product without exposing its canonical id."""
    if selected_product is None:
        return ""
    payload: dict[str, Any] = {
        "name": selected_product.get("product", ""),
        "product_category": selected_product.get("product_category", ""),
        "component_category": selected_product.get("component_category", ""),
    }
    candidates = entities.get("product_candidates")
    if isinstance(candidates, list):
        for item in candidates:
            candidate = canonical_product_candidate(item) if isinstance(item, dict) else None
            if candidate is None:
                continue
            if candidate.get("product_id") == selected_product.get("product_id") and candidate.get(
                "product_category"
            ) == selected_product.get("product_category"):
                if isinstance(candidate.get("price_cents"), int):
                    payload["price_yuan"] = candidate["price_cents"] / 100
                break
    return (
        "本轮服务端已完成 canonical product promotion。Operator 只能把下面这件商品当作当前选中商品；"
        "不得改成其他型号，也不要输出内部 product_id：\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def _can_append_generic_customer_action(
    result: LoopResult,
    *,
    trusted_navigation: str = "",
) -> bool:
    """Do not append navigation beneath a controlled response card.

    A server-owned navigation action is also safe for ordinary fact/explanation
    responses.  It is not a reason to add links to clarification, choice,
    handoff, or error responses.
    """
    mode = str((result.response_control or {}).get("mode") or "")
    if mode in {"", "GENERIC"}:
        return True
    return bool(trusted_navigation) and mode in {"FACT", "EXPLANATION", "READ_ONLY"}


def _trusted_navigation_action(
    intent: Intent,
    *,
    canonical_product: bool,
    recommended_product_actions: bool = False,
) -> str:
    """Return a server-owned navigation capability, never a model-generated URL."""
    if canonical_product or recommended_product_actions:
        return "product_detail_navigation"
    if intent.domain in {"order", "delivery", "payment", "refund"}:
        return "orders_navigation"
    if intent.target == "ticket":
        return "ticket_navigation"
    return ""


def _support_case_payloads(intent: Intent) -> list[dict]:
    """把本轮 Control Plane 请求转换为可持久化 Case payload。

    明确 Goal 使用 ``support_requests``；纯陈述中的 mutable customer claim
    只会投影成只读 ``verification_requests``，不会获得任何写操作权限。
    """
    return [support_request.to_case_payload() for support_request in intent.workflow_requests]


def _recent_subject_router_context(case: SupportCase | None) -> str:
    """Expose only a verified recent subject to the Router's relation classifier."""
    if case is None or case.status != "COMPLETED" or _case_has_subject_context_reset(case):
        return ""
    order_id = case.selected_subjects.get("order_id")
    if not isinstance(order_id, str) or not order_id.startswith("SO"):
        return ""
    return json.dumps(
        {
            "case_status": "COMPLETED",
            "recent_verified_subject": {"subject_type": "order", "subject_id": order_id},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _session_decision_facts(messages: list[dict[str, object]] | None) -> dict[str, object]:
    """读取最近一轮由服务端保存的只读事实元数据，不把它暴露给模型。"""
    for message in reversed(messages or []):
        facts = message.get("_decision_facts") if isinstance(message, dict) else None
        if isinstance(facts, dict):
            return dict(facts)
    return {}


def _previous_turn_outcome(messages: list[dict[str, object]] | None) -> dict[str, object]:
    """Return a bounded server-owned summary of the prior customer-support outcome.

    This metadata is observability state, not a transaction fact.  It is useful
    when the Router/Operator must explain why the previous support turn could
    not continue without guessing from the rendered Chinese answer.
    """
    allowed = {
        "domain",
        "operation",
        "subject_relation",
        "selected_subject",
        "subject_resolution_status",
        "required_facts",
        "known_facts",
        "missing_facts",
        "failed_capabilities",
        "goal_status",
        "workflow_reason",
        "next_action",
        "next_actor",
        "response_control_mode",
        "answer_source",
    }
    for message in reversed(messages or []):
        trace = message.get("_answer_trace") if isinstance(message, dict) else None
        if not isinstance(trace, dict):
            continue
        outcome = {key: trace[key] for key in allowed if trace.get(key) not in (None, "", [], {})}
        if outcome:
            return outcome
    return {}


def _router_case_context(case_context: str, messages: list[dict[str, object]] | None) -> str:
    """Compose trusted Case state with the previous support outcome contract."""
    outcome = _previous_turn_outcome(messages)
    if not outcome:
        return case_context
    outcome_block = "previous_turn_outcome=" + json.dumps(outcome, ensure_ascii=False, separators=(",", ":"))
    return f"{case_context}\n{outcome_block}" if case_context else outcome_block


def _previous_outcome_operator_context(intent: Intent, messages: list[dict[str, object]] | None) -> str:
    """Expose prior execution outcome only for a Router-owned explanation turn."""
    if getattr(intent, "fact_scope", "current") != "explain_previous":
        return ""
    outcome = _previous_turn_outcome(messages)
    if not outcome:
        return ""
    return (
        "上一轮客服执行结果（服务端可信；只用于解释上一轮为什么完成/未完成，不代表当前实时交易事实）：\n"
        + json.dumps(outcome, ensure_ascii=False, separators=(",", ":"))
        + "\n请解释这个执行结果本身；除非该结果明确记录 Provider/交易失败，不要把客服编排失败改写成退款 Provider 失败。"
    )


def _session_decision_contexts(messages: list[dict[str, object]] | None) -> list[dict[str, object]]:
    """读取服务端保存的 subject-bound facts，并标记为 historical。"""
    contexts: list[dict[str, object]] = []
    # 按消息时间正序合并；同一订单后一次工具读取应覆盖更早的历史读取。
    # ``_session_decision_facts`` 仍单独从末尾读取 flat 兼容 metadata。
    for message in messages or []:
        raw = message.get("_decision_contexts") if isinstance(message, dict) else None
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            context = dict(item)
            context["provenance"] = "historical"
            contexts.append(context)
    return merge_decision_contexts(contexts, default_provenance="historical")


def _case_refund_requests(case: SupportCase | None) -> list[dict[str, object]]:
    if case is None:
        return []
    return [item for item in case.request_stack if isinstance(item, dict) and str(item.get("domain") or "") == "refund"]


def _case_ticket_id(case: SupportCase | None) -> str | None:
    """Return only the ticket id persisted in an awaiting-staff Case."""
    if case is None:
        return None
    pending = case.pending if isinstance(case.pending, dict) else {}
    summary = pending.get("summary")
    candidate = summary.get("ticket_id") if isinstance(summary, dict) else pending.get("ticket_id")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()[:80]
    return None


def _case_refund_facts(case: SupportCase | None) -> dict[str, object]:
    if case is None:
        return {}
    return {
        str(key): value
        for key, value in case.verified_facts.items()
        if str(key).startswith("refund_") or str(key) in {"order_identified", "shipping_status", "order_status"}
    }


def _case_decision_contexts(case: SupportCase | None) -> list[dict[str, object]]:
    """读取 Case 内按订单隔离的事实组，不把它们展平成客户可见事实。"""
    if case is None:
        return []
    raw = case.verified_facts.get("_decision_contexts")
    if not isinstance(raw, list):
        return []
    contexts = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        contexts.append(item)
    return historicalize_decision_contexts(contexts)


def _merged_refund_facts(
    result: LoopResult,
    *,
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], bool]:
    """按 subject 合并兼容事实，并返回是否全部来自 historical。"""
    current_contexts = merge_decision_contexts(result.decision_contexts)
    if _case_has_subject_context_reset(recent_case):
        # Keep the old contexts in storage for audit, but do not let a new
        # request rediscover the disputed subject as its current transaction.
        historical_contexts: list[dict[str, object]] = []
    else:
        historical_contexts = merge_decision_contexts(
            session_contexts or [],
            _case_decision_contexts(recent_case),
            default_provenance="historical",
        )
    all_contexts = merge_decision_contexts(current_contexts, historical_contexts)
    selected_id = None
    if recent_case is not None:
        selected = recent_case.selected_subjects.get("order_id")
        if isinstance(selected, str) and selected.startswith("SO"):
            selected_id = selected
    subject_ids = {str(item.get("subject_id")) for item in all_contexts if item.get("subject_id")}
    if selected_id is None and len(subject_ids) == 1:
        selected_id = next(iter(subject_ids))
    if selected_id is not None:
        current_facts, _ = context_facts_for_subject(current_contexts, selected_id, provenance="current")
        historical_facts, _ = context_facts_for_subject(historical_contexts, selected_id, provenance="historical")
        merged = dict(historical_facts)
        merged.update(current_facts)
        return merged, bool(merged) and not bool(current_facts)
    if len(subject_ids) > 1:
        return {}, False

    # 只给完全没有 subject context 的旧会话/旧测试保留 flat 兼容路径。只要新格式
    # context 存在，就绝不能用 flat facts 绕过 subject 隔离；这也是迁移期间避免
    # 新旧 metadata 交叉污染的边界。
    # ``session_facts`` 可能仍来自 disputed subject A 的旧 flat metadata。Case reset
    # 是服务端明确的 subject-retirement barrier；本轮只有新 result 的 current facts
    # 可以重新建立一个可信 subject，不能让 session fallback 把 A 重新带回来。
    merged = {} if _case_has_subject_context_reset(recent_case) else dict(session_facts or {})
    progress_facts = result.workflow_progress.get("decision_facts")
    current_sources = (progress_facts, result.verified_facts, result.decision_facts)
    for source in current_sources:
        if isinstance(source, dict):
            merged.update(source)
    has_current_facts = any(isinstance(source, dict) and source for source in current_sources)
    return merged, bool(merged) and not has_current_facts


def _refund_boundary_requests(
    intent: Intent,
    result: LoopResult,
    *,
    query: str = "",
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
    session_messages: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """为普通 AgentLoop 结果恢复最小退款事实边界输入。

    正常 SupportWorkflow 会把请求保存在 Case/Workflow state；兼容的普通 AgentLoop
    路径可能只有 intent 字段或工具事实，因此这里只根据受控请求/事实选择已实现的
    status、eligibility 渲染，不从客户文本猜业务结论。
    """
    requests = _support_case_payloads(intent)
    # Payment status and pending-order cancellation use the same shared
    # customer-response composer as refund.  They must keep their canonical
    # request rather than falling through to a free-form Agent answer.
    controlled_requests = [
        request
        for request in requests
        if (str(request.get("domain") or ""), str(request.get("operation") or ""))
        in {
            ("payment", "check_payment_status"),
            ("order", "cancel"),
        }
    ]
    if controlled_requests:
        return controlled_requests
    refund_requests = [request for request in requests if str(request.get("domain") or "") == "refund"]
    if refund_requests:
        return refund_requests
    facts, _ = _merged_refund_facts(
        result,
        recent_case=recent_case,
        session_facts=session_facts,
        session_contexts=session_contexts,
    )
    # A current read can establish a current refund boundary even if the Router
    # did not emit a canonical request.  Historical facts, however, are never a
    # reason to resurrect the historical refund goal.
    current_fact_keys = _current_refund_fact_keys(result)
    if current_fact_keys:
        if isinstance(facts.get("refund_eligibility"), bool) and (
            intent.domain != "refund" or intent.operation not in {"status", "eligibility"}
        ):
            return [{"domain": "refund", "operation": "eligibility"}]
        if "refund_status" in current_fact_keys:
            return [{"domain": "refund", "operation": "status"}]
        if "refund_eligibility" in current_fact_keys:
            return [{"domain": "refund", "operation": "eligibility"}]

    # Transaction safety remains session-aware, but conversational goal
    # continuation is intentionally narrower.  Only an immediate, explicit
    # refund follow-up may reuse the previous Case request.
    is_follow_up = _is_refund_follow_up(query or intent.query, session_messages=session_messages)
    if is_follow_up:
        recent_requests = _case_refund_requests(recent_case)
        if recent_requests:
            operation = str(recent_requests[0].get("operation") or "")
            if operation in {"request", "eligibility"} and "refund_status" in facts:
                return [{"domain": "refund", "operation": "status"}]
            return recent_requests[:1]
        if "refund_status" in facts:
            return [{"domain": "refund", "operation": "status"}]
        if isinstance(facts.get("refund_eligibility"), bool):
            return [{"domain": "refund", "operation": "eligibility"}]
    if intent.domain == "refund" and intent.operation:
        # 旧兼容字段可能仍把退款状态查询写成泛化 operation（例如 ``refund``）。
        # 只要工具已经返回退款状态，就必须回到 status renderer，不能让这个
        # 未知 operation 绕过事实边界把模型原文直接交给客户。
        if intent.operation in {
            "status",
            "expected_arrival",
            "processing_time",
            "anomaly",
            "delivery_after_refund",
            "destination",
            "request",
            "cancel",
            "amount",
            "eligibility",
            "procedure",
        }:
            return [{"domain": "refund", "operation": intent.operation}]
        if "refund_status" in facts:
            return [{"domain": "refund", "operation": "status"}]
        if isinstance(facts.get("refund_eligibility"), bool):
            return [{"domain": "refund", "operation": "eligibility"}]
    if current_fact_keys and "refund_status" in facts:
        return [{"domain": "refund", "operation": "status"}]
    if current_fact_keys and isinstance(facts.get("refund_eligibility"), bool):
        return [{"domain": "refund", "operation": "eligibility"}]
    return []


def _current_refund_fact_keys(result: LoopResult) -> set[str]:
    """List refund facts produced by this turn, excluding historical contexts."""
    keys = {
        "refund_status",
        "refund_amount",
        "refund_eligibility",
        "refund_entry",
        "expected_arrival_time",
        "refund_processing_sla",
        "refund_destination",
        "refund_failure_reason",
        "warehouse_receipt_status",
    }
    found: set[str] = set()
    for source in (
        result.decision_facts,
        result.verified_facts,
        result.workflow_progress.get("decision_facts"),
    ):
        if isinstance(source, dict):
            found.update(name for name in keys if name in source)
    for context in result.decision_contexts:
        if not isinstance(context, dict) or context.get("provenance") not in {None, "current"}:
            continue
        context_facts = context.get("facts")
        if isinstance(context_facts, dict):
            found.update(name for name in keys if name in context_facts)
    return found


def _is_refund_follow_up(
    query: str,
    *,
    session_messages: list[dict[str, object]] | None = None,
) -> bool:
    """Recognize a narrow transaction follow-up without inheriting a whole goal.

    This is a response-safety gate, not Router logic.  It deliberately accepts
    explicit refund/status language and a small set of pronoun-plus-status forms;
    unrelated acknowledgements, emotion and new topics remain outside refund goal
    continuation.
    """
    normalized = re.sub(r"\s+", "", query).lower()
    if any(marker in normalized for marker in ("退款", "退钱", "返款", "到账", "退回")):
        return True
    if any(marker in normalized for marker in ("这笔", "这单", "刚才那笔")) and any(
        marker in normalized for marker in ("状态", "怎么样", "处理中", "处理到哪", "完成", "进度")
    ):
        return True
    if "页面" in normalized and any(marker in normalized for marker in ("处理中", "处理", "完成")):
        return True
    if normalized in {"为什么", "为什么呢", "怎么回事", "什么意思"}:
        for message in reversed(session_messages or []):
            if message.get("role") != "assistant":
                continue
            previous = re.sub(r"\s+", "", str(message.get("content") or "")).lower()
            return any(marker in previous for marker in ("退款", "退款资格", "退款记录", "尚未发货"))
    return False


def _apply_customer_refund_fact_boundary(
    intent: Intent,
    result: LoopResult,
    *,
    query: str = "",
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
    session_messages: list[dict[str, object]] | None = None,
) -> None:
    """普通/流式出口统一委托共享 customer-response composer。"""
    requests = _refund_boundary_requests(
        intent,
        result,
        query=query,
        recent_case=recent_case,
        session_facts=session_facts,
        session_contexts=session_contexts,
        session_messages=session_messages,
    )
    current_request = any(str(item.get("domain") or "") == "refund" for item in _support_case_payloads(intent))
    allow_historical_facts = current_request or _is_refund_follow_up(
        query or intent.query,
        session_messages=session_messages,
    )
    historical_contexts = (
        merge_decision_contexts(
            session_contexts or [],
            _case_decision_contexts(recent_case),
            default_provenance="historical",
        )
        if allow_historical_facts
        else []
    )
    compose_customer_response(
        result,
        requests,
        current_contexts=result.decision_contexts,
        historical_contexts=historical_contexts,
        legacy_current_facts=result.decision_facts,
        legacy_historical_facts=session_facts,
        case=recent_case,
        enforce_refund_boundary=_refund_response_context(
            intent,
            query,
            active_case=recent_case,
            recent_case=recent_case,
            session_facts=session_facts,
            session_contexts=session_contexts,
        ),
    )


def _case_has_refund_context(case: SupportCase | None) -> bool:
    if case is None:
        return False
    if _case_refund_requests(case) or _case_refund_facts(case) or _case_decision_contexts(case):
        return True
    return any(
        str(item.get("operation") or "") in {"after_sales_transition", "return_logistics"}
        for item in case.request_stack
        if isinstance(item, dict)
    )


def _case_has_subject_context_reset(case: SupportCase | None) -> bool:
    """Return whether a disputed subject awaits a fresh trusted binding."""
    if case is None:
        return False
    return isinstance(case.verified_facts.get(SUBJECT_CONTEXT_RESET_MARKER), dict)


def _refund_response_context(
    intent: Intent,
    query: str,
    *,
    active_case: SupportCase | None = None,
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
) -> bool:
    """在生成客户答案前判断是否必须经过退款事实边界。"""
    if intent.domain == "refund" or any(
        str(item.get("domain") or "") == "refund" for item in _support_case_payloads(intent)
    ):
        return True
    if active_case is not None and _case_has_refund_context(active_case):
        return True
    normalized = re.sub(r"\s+", "", query).lower()
    follow_up_markers = ("退款", "退钱", "返款", "到账", "处理中", "处理", "这笔", "页面", "钱")
    # 当前句直接提到退款/到账时，即使 Router 没有形成 support request，也要在
    # 首个 customer-visible token 发出前进入共享 response boundary。R018 就属于
    # 这种“模型给出多笔交易结论、但本轮没有可信 subject/fact”的情况。
    if any(marker in normalized for marker in follow_up_markers):
        return True
    if recent_case is not None and _case_has_refund_context(recent_case):
        return True
    if session_facts and any(key in session_facts for key in ("refund_status", "refund_eligibility", "refund_amount")):
        return True
    if session_contexts:
        return True
    return False


async def _get_latest_support_case(
    request: Request,
    *,
    session_id: str,
    customer_user_id: int,
) -> SupportCase | None:
    """只读读取最近 Case，供跨轮 customer-output boundary 使用。"""
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return None
    return await service.get_latest(session_id=session_id, customer_user_id=customer_user_id)


async def _open_support_case(
    request: Request,
    *,
    intent: Intent,
    session_id: str,
    customer_user_id: int,
    recent_case: SupportCase | None = None,
    raw_query: str = "",
    tool_context: ToolContext | None = None,
) -> SupportCase | None:
    """为需要跨轮推进的客服请求创建或恢复 Support Case。

    未注册 Service 的测试/兼容应用仍可运行原 Workflow；正式 main 会注册持久化服务。
    """
    payloads = _support_case_payloads(intent)
    if not payloads:
        return None
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return None
    # Natural-language continuation must not pre-bind the previous order from
    # Router ``same`` semantics.  The recent verified subject is passed later as
    # Resolver context only; OrderSubjectResolver owns whether this turn keeps or
    # changes that subject.  Structured UI choices still bind deterministically
    # through the existing interaction path.
    initial_selected_subjects = None
    opened = await service.open_or_resume(
        session_id=session_id,
        customer_user_id=customer_user_id,
        request_stack=payloads,
        initial_selected_subjects=initial_selected_subjects,
    )
    return opened.case


def _explicit_order_reference(query: str) -> str | None:
    match = re.search(r"\bSO[A-Z0-9_-]+\b", query, flags=re.IGNORECASE)
    return match.group(0).upper() if match is not None else None


def _trusted_subject_relation(intent: Intent, *, recent_order_id: str, raw_query: str) -> str:
    """Classify subject relation from Router semantics, never from a generated id.

    ``same`` is only a permission to reuse a server-verified recent subject;
    it does not carry any transaction fact forward.  An explicit different
    order, a Router-provided subject reference, or a correction expression is
    treated as changed/unknown and must resolve again in the normal workflow.
    """
    declared_relation = str(getattr(intent, "subject_relation", "unknown") or "unknown")
    if declared_relation in {"same", "changed"}:
        return declared_relation
    explicit_order_id = _explicit_order_reference(raw_query)
    if explicit_order_id is not None:
        return "same" if explicit_order_id == recent_order_id else "changed"
    requests = intent.support_requests
    if not requests:
        return "unknown"
    if any(request.subject_refs for request in requests):
        # ``current_order`` is a Router semantic anaphora, not a model-supplied
        # identifier.  Other refs may describe a different product/order.
        refs = {ref.strip().lower() for request in requests for ref in request.subject_refs if ref.strip()}
        return "same" if refs and refs <= {"current_order", "current_after_sale"} else "changed"
    if intent.case_update == "continue":
        return "same"
    if intent.case_update == "new_request":
        return "unknown"
    return "unknown"


async def _immediate_trusted_subject_continuation(
    request: Request,
    *,
    intent: Intent,
    recent_case: SupportCase | None,
    raw_query: str,
    tool_context: ToolContext | None,
) -> dict[str, object] | None:
    """Carry a just-verified order into an immediate, semantically same follow-up.

    The Router classifies the current Goal; this function only authorizes reuse
    of the server-bound subject after another ownership check.  Dynamic order,
    payment and refund facts are intentionally never inherited.
    """
    if tool_context is None or tool_context.role != "customer" or recent_case is None:
        return None
    if recent_case.status != "COMPLETED" or _case_has_subject_context_reset(recent_case):
        return None
    selected_order_id = recent_case.selected_subjects.get("order_id")
    if not isinstance(selected_order_id, str) or not selected_order_id.startswith("SO"):
        return None
    if not intent.support_requests:
        return None
    # An order-list request is a new browsing goal, not a singular-subject
    # continuation.  Do not bind the previous order merely because the Router
    # supplied a generic ``same`` relation.
    if all(
        str(item.domain if hasattr(item, "domain") else item.get("domain") or "") == "order"
        and str(item.operation if hasattr(item, "operation") else item.get("operation") or "") == "list"
        for item in intent.support_requests
    ):
        return None
    if _trusted_subject_relation(intent, recent_order_id=selected_order_id, raw_query=raw_query) != "same":
        return None
    # Before carrying a completed Case subject forward, validate it against
    # fresh authenticated checkout candidates using identity-only evidence.
    # This guard does not choose a new order: it only vetoes reuse when the
    # customer's current identity evidence points elsewhere or is ambiguous.
    try:
        current_choices = await _customer_subject_choices(tool_context.user_id)
    except Exception:
        current_choices = []
    if current_choices:
        identity_matches = match_subject_identity_choices(
            raw_query,
            current_choices,
            # This caller is not interpreting exclusion semantics; it is
            # checking positive identity evidence as a contradiction guard.
            trusted_exclusion_applied=True,
        )
        if identity_matches:
            matched_ids = {str(choice.get("order_id") or "") for choice in identity_matches if isinstance(choice, dict)}
            if len(matched_ids) != 1 or selected_order_id not in matched_ids:
                return None
    if not await _verify_customer_order_subject(
        request,
        order_id=selected_order_id,
        tool_context=tool_context,
    ):
        return None
    return {"order_id": selected_order_id}


async def _trusted_previous_subject_for_resolution(
    request: Request,
    *,
    intent: Intent,
    recent_case: SupportCase | None,
    tool_context: ToolContext | None,
) -> dict[str, object] | None:
    """Provide one server-verified prior order as Resolver context, never authority.

    The source may be a completed Case or the currently active Case. An active
    Case selection is still only identity continuity on a natural-language turn:
    the current utterance must be allowed to keep or replace it through the sole
    semantic OrderSubjectResolver. Failed/cancelled or reset Cases provide no
    subject context.
    """
    if tool_context is None or tool_context.role != "customer" or recent_case is None:
        return None
    if recent_case.status not in {"ACTIVE", "AWAITING_CUSTOMER", "COMPLETED"} or _case_has_subject_context_reset(
        recent_case
    ):
        return None
    previous_order_id = recent_case.selected_subjects.get("order_id")
    if not isinstance(previous_order_id, str) or not previous_order_id.startswith("SO"):
        return None
    if not await _verify_customer_order_subject(
        request,
        order_id=previous_order_id,
        tool_context=tool_context,
    ):
        return None
    return {"order_id": previous_order_id}


async def _workflow_subject_context_kwargs(
    request: Request,
    *,
    intent: Intent,
    support_case: SupportCase | None,
    recent_case: SupportCase | None,
    tool_context: ToolContext | None,
    structured_interaction: bool,
) -> dict[str, object]:
    """Build the one subject boundary shared by /chat and /chat/stream.

    A structured server-validated choice is already a current binding.  Every
    natural-language turn treats an existing Case selection only as previous
    identity context so OrderSubjectResolver can either continue or switch it.
    """
    if structured_interaction and support_case is not None and support_case.selected_subjects:
        return {"selected_subjects": support_case.selected_subjects}
    subject_context_case = support_case if support_case is not None and support_case.selected_subjects else recent_case
    previous_subjects = await _trusted_previous_subject_for_resolution(
        request,
        intent=intent,
        recent_case=subject_context_case,
        tool_context=tool_context,
    )
    return {"previous_subjects": previous_subjects} if previous_subjects is not None else {}


async def _allow_historical_subject_explanation(
    request: Request,
    *,
    intent: Intent,
    support_case: SupportCase | None,
    raw_query: str,
    tool_context: ToolContext,
) -> bool:
    """Permit explanation of a prior fact without turning it into a current fact.

    The Router may classify a turn as ``explain_previous`` but cannot select an
    order.  This guard requires the server-owned Case subject and repeats the
    ownership check before the historical context is shown to the operator.
    """
    if getattr(intent, "fact_scope", "current") != "explain_previous" or support_case is None:
        return False
    selected_order_id = support_case.selected_subjects.get("order_id")
    if not isinstance(selected_order_id, str) or not selected_order_id.startswith("SO"):
        return False
    if _trusted_subject_relation(intent, recent_order_id=selected_order_id, raw_query=raw_query) != "same":
        return False
    return await _verify_customer_order_subject(
        request,
        order_id=selected_order_id,
        tool_context=tool_context,
    )


async def _get_active_support_case(
    request: Request,
    *,
    session_id: str,
    customer_user_id: int,
) -> SupportCase | None:
    """读取活动 Case 以便将“第一个/可以”等答复承接回原流程。"""
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return None
    return await service.get_active(session_id=session_id, customer_user_id=customer_user_id)


def _is_pending_case_reply(case: SupportCase | None, intent: Intent, raw_query: str) -> bool:
    """只把明确的短确认绑定回 pending，避免吞掉客户的新问题。"""
    if case is None or case.status != "AWAITING_CUSTOMER":
        return False
    # A correction description is not a general-purpose case confirmation.  A
    # valid bare description is consumed before Router invocation; every other
    # message must stay on the normal Router path and may supersede this frame.
    # This prevents an acknowledgement or a new business request from being
    # fed into the old correction Workflow merely because the Router said
    # ``continue``.
    if case.pending.get("kind") == "subject_correction_description":
        return False
    # Current Goal belongs to the Router.  A pending choice may interpret a
    # continuation, but it must never resurrect an old workflow after the Router
    # has classified this turn as an independent new request.
    if intent.case_update == "new_request":
        return False
    if case.pending.get("kind") == "customer_choice":
        choices = case.pending.get("choices", [])
        # 已展示的候选拥有优先级：可唯一解析的选择，以及明显在回答选择的
        # 未解析表达，都应回到原 Case，而不是被孤立路由成新业务请求。
        if resolve_pending_subject_choice(raw_query, choices) is not None:
            return True
        if looks_like_pending_subject_choice(raw_query, choices):
            return True
    if intent.case_update == "continue":
        return True
    normalized = re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]]+", "", raw_query).lower()
    return normalized in {
        "好",
        "好的",
        "行",
        "可以",
        "嗯",
        "是",
        "不是",
        "没有",
        "没了",
        "第一个",
        "第1个",
        "第二个",
        "第2个",
        "选第一个",
        "选第二个",
        "都不要",
        "已经退了",
        "已退了",
        "还是不行",
    }


async def _supersede_subject_correction_pending(
    request: Request,
    *,
    case: SupportCase | None,
) -> SupportCase | None:
    """清理已被本轮普通 Router 输入取代的 correction 描述 pending。"""
    if case is None or case.pending.get("kind") != "subject_correction_description":
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    updated = await service.supersede_subject_correction_description(case)
    if updated is None:
        # Do not let a stale Case be reattached by open_or_resume after an
        # optimistic-lock race.  The caller will fail closed instead.
        raise HTTPException(status_code=409, detail="当前订单纠正状态已变化，请重试。")
    return updated


def _intent_for_case_reply(case: SupportCase, intent: Intent) -> Intent:
    """用已持久化且重新校验的请求栈恢复 Workflow，而不是信任短回复的孤立路由。"""
    requests = IntentRouter.support_requests_from_case_payloads(case.request_stack)
    if not requests:
        return intent
    primary = requests[0]
    tools = list(dict.fromkeys(tool for item in requests for tool in item.required_tools))
    return Intent(
        target="agent",
        query=intent.query,
        raw_query=intent.raw_query,
        retrieval_query=intent.retrieval_query,
        semantic_hints=dict(intent.semantic_hints),
        confidence=max(intent.confidence, 0.9),
        route_source=intent.route_source,
        speech_act=intent.speech_act,
        domain=primary.domain,
        operation=primary.operation,
        goal_modifier=primary.goal_modifier,
        state="pending",
        next_step=primary.next_step,
        required_tools=tools,
        ambiguities=list(primary.ambiguities),
        requests=requests,
        case_update="continue",
        subject_relation=intent.subject_relation,
        fact_scope=intent.fact_scope,
        subject_refs=list(intent.subject_refs),
        customer_claims=list(intent.customer_claims),
    )


def _is_confirmed_human_handoff(case: SupportCase | None, raw_query: str) -> bool:
    """只在客户明确请求人工时进入真实建单流程。

    对独立的“转人工”消息没有必要先创建一个只会再次询问的 human Case；
    直接复用现有 ``_create_customer_ticket`` 闸门。退款/设备等复合句仍由
    原有的先引导、后确认流程处理。
    """
    normalized = re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]]+", "", raw_query).lower()
    if normalized in {
        "转人工",
        "请转人工",
        "转接人工",
        "找人工",
        "找人工客服",
        "需要人工",
        "需要真人客服",
        "我要人工客服",
        "我要找人工",
    }:
        return True
    operations = (
        {str(item.get("operation") or "") for item in case.request_stack if isinstance(item, dict)}
        if case is not None
        else set()
    )
    if case is None or case.status != "AWAITING_CUSTOMER" or "human_handoff" not in operations:
        return False
    return normalized in {"是", "好的", "好", "确认", "需要", "仍需人工", "还是要人工"}


async def _resume_pending_case(
    request: Request,
    *,
    case: SupportCase | None,
    raw_query: str,
    tool_context: ToolContext | None = None,
) -> SupportCase | None:
    if case is None:
        return None
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    if case.pending.get("kind") == "customer_choice":
        choices = case.pending.get("choices", [])
        if not isinstance(choices, list) or not choices:
            # 兼容在 choice frame 上线前创建的旧 Case。它没有可供服务端解析的
            # 展示快照，不能声称完成了确定性选单；仍按旧恢复路径承接上下文。
            return await service.resume_customer_response(case)
        resolution_status, choice, narrowed_choices, selection_source = await _resolve_pending_order_choice(
            request,
            raw_query=raw_query,
            choices=[item for item in choices if isinstance(item, dict)],
        )
        if resolution_status == "ambiguous":
            pending = dict(case.pending)
            pending["choices"] = narrowed_choices
            updated = await service.await_customer(
                case,
                pending=pending,
                selected_subjects=case.selected_subjects,
            )
            return updated or case
        if resolution_status != "resolved" or choice is None:
            # Keep the task frame alive, but do not expose a stale full list or
            # let an unrecognized reply bind an order by position.
            pending = dict(case.pending)
            pending.update({"kind": "customer_clarification", "choices": []})
            updated = await service.await_customer(
                case,
                pending=pending,
                selected_subjects=case.selected_subjects,
            )
            return updated or case
        if not isinstance(choice, dict):
            return case
        if tool_context is not None:
            return await _select_verified_pending_subject(
                request,
                case=case,
                subject=choice,
                selection_source=selection_source or "choice",
                tool_context=tool_context,
            )
        if case.pending.get("selection_event") == "subject_correction" or case.pending.get("transition_from_order_id"):
            # A correction changes an authoritative subject, so the ownership
            # re-check is mandatory even for internal callers of this helper.
            # Without the request-scoped ToolContext, fail closed instead of
            # transitioning from a natural-language match alone.
            return case
        updated = await service.select_customer_subject(
            case,
            subject=choice,
            selection_source=selection_source or "choice",
        )
        return updated or case
    return await service.resume_customer_response(case)


async def _verify_customer_order_subject(
    request: Request,
    *,
    order_id: str,
    tool_context: ToolContext,
) -> bool:
    """用当前身份和真实只读订单工具重新核验一个 subject。"""
    registry = getattr(request.app.state, "registry", None)
    execute = getattr(registry, "execute", None)
    if not callable(execute):
        raise HTTPException(status_code=503, detail="订单暂时无法核验，请稍后重试。")
    # This is a narrowly scoped ownership re-check.  It must not inherit the
    # currently bound subject, otherwise ToolRegistry would correctly reject
    # the candidate before the transition has been persisted.  The later
    # workflow call still receives the authoritative bound subject.
    verification_context = (
        replace(tool_context, selected_order_id=None) if tool_context.selected_order_id is not None else tool_context
    )
    verification = await execute(
        "track_order",
        tool_context=verification_context,
        order_id=order_id,
    )
    if not getattr(verification, "is_success", False):
        return False
    verified_data = getattr(verification, "data", {})
    return isinstance(verified_data, dict) and verified_data.get("order_id") == order_id


async def _select_verified_pending_subject(
    request: Request,
    *,
    case: SupportCase,
    subject: dict[str, object],
    selection_source: str,
    tool_context: ToolContext,
) -> SupportCase:
    """让结构化点击与自然语言选择汇聚到同一条验证/持久化路径。"""
    order_id = subject.get("order_id")
    if not isinstance(order_id, str) or not await _verify_customer_order_subject(
        request,
        order_id=order_id,
        tool_context=tool_context,
    ):
        raise HTTPException(status_code=409, detail="该订单选择已失效，请重新选择要查询的订单。")
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        raise HTTPException(status_code=503, detail="订单选择暂时无法保存，请稍后重试。")
    pending = case.pending
    if pending.get("selection_event") == "subject_correction" or pending.get("transition_from_order_id"):
        updated = await service.transition_customer_subject(
            case,
            subject=subject,
            selection_source=selection_source,
        )
    else:
        updated = await service.select_customer_subject(
            case,
            subject=subject,
            selection_source=selection_source,
        )
    if updated is None:
        raise HTTPException(status_code=409, detail="该订单选择已失效，请重新选择要查询的订单。")
    return updated


async def _apply_subject_choice_interaction(
    request: Request,
    *,
    case: SupportCase | None,
    interaction: SubjectChoiceInteraction,
    tool_context: ToolContext,
) -> SupportCase:
    """验证并应用客户点击的订单选择。

    浏览器提交的 subject_id 只是 selector，不能直接写入 Case。候选快照先经过
    active Case/pending membership 校验，再用当前客户身份调用真实只读订单工具，
    确认订单仍存在且仍归属当前用户，最后才复用 Case service 的同 Case selection。
    """
    error = "该订单选择已失效，请重新选择要查询的订单。"
    if tool_context.role != "customer":
        raise HTTPException(status_code=403, detail="该交互仅支持客户会话。")
    if case is None or case.status != "AWAITING_CUSTOMER":
        raise HTTPException(status_code=409, detail=error)
    pending = case.pending
    if pending.get("kind") != "customer_choice" or pending.get("subject_type", "order") != "order":
        raise HTTPException(status_code=409, detail=error)
    choices = pending.get("choices")
    if not isinstance(choices, list):
        raise HTTPException(status_code=409, detail=error)
    candidate = next(
        (item for item in choices if isinstance(item, dict) and item.get("order_id") == interaction.subject_id),
        None,
    )
    if candidate is None:
        raise HTTPException(status_code=409, detail=error)

    return await _select_verified_pending_subject(
        request,
        case=case,
        subject=candidate,
        selection_source="structured_interaction",
        tool_context=tool_context,
    )


def _subject_choice_from_checkout_order(order: object) -> dict[str, object] | None:
    """把 checkout store 的本人订单摘要裁成最小候选字段。

    调用方已经是 ownership-scoped ``list_customer_checkout_orders``，因此这里不再
    接受混合 legacy/checkout 的通用订单 payload，也不接受客户端或模型提供的候选。
    """
    order_id = getattr(order, "order_no", None)
    product_name = getattr(order, "product_name", None)
    amount_cents = getattr(order, "total_amount_cents", None)
    if not isinstance(order_id, str) or not order_id.startswith("SO"):
        return None
    choice: dict[str, object] = {"order_id": order_id}
    if isinstance(product_name, str) and product_name.strip():
        choice["product_name"] = product_name.strip()[:160]
    if isinstance(amount_cents, int) and not isinstance(amount_cents, bool) and amount_cents >= 0:
        choice["amount_cents"] = amount_cents
    raw_items = getattr(order, "items", ())
    if isinstance(raw_items, tuple) and raw_items:
        choice["items"] = [
            {
                "product_name": str(getattr(item, "product_name", "")),
                "catalog_category": str(getattr(item, "catalog_category", "")),
                "catalog_product_id": str(getattr(item, "catalog_product_id", "")),
                "component_category": getattr(item, "component_category", None),
            }
            for item in raw_items
        ]
    return choice


async def _customer_subject_choices(customer_user_id: int) -> list[dict[str, object]]:
    """只读取当前客户最近 30 笔 checkout 订单，保持数据库展示顺序。"""
    orders = await list_customer_checkout_orders(customer_user_id, limit=30)
    choices: list[dict[str, object]] = []
    seen: set[str] = set()
    for recency_rank, order in enumerate(orders, start=1):
        choice = _subject_choice_from_checkout_order(order)
        if choice is None:
            continue
        order_id = str(choice["order_id"])
        if order_id in seen:
            continue
        seen.add(order_id)
        if getattr(order, "created_at", None) or getattr(order, "order_date", None):
            choice["recency_rank"] = recency_rank
        choices.append(choice)
    return choices[:MAX_ORDER_CHOICE_OPTIONS]


async def _resolve_pending_order_choice(
    request: Request,
    *,
    raw_query: str,
    choices: list[dict[str, object]],
) -> tuple[str, dict[str, object] | None, list[dict[str, object]], str]:
    """Resolve a pending choice without letting the model choose an order id."""

    explicit = resolve_pending_subject_choice(raw_query, choices)
    if explicit is not None:
        choice = explicit.get("choice")
        return (
            "resolved",
            choice if isinstance(choice, dict) else None,
            [],
            str(explicit.get("selection_source") or "choice"),
        )

    narrowed = match_pending_subject_choices(raw_query, choices)
    pool = narrowed or choices
    if len(pool) <= 1:
        return "unknown", None, [], ""
    intent_router = getattr(request.app.state, "intent_router", None)
    resolution = await resolve_order_subject(
        getattr(intent_router, "llm", None),
        raw_query,
        [item for item in pool if isinstance(item, dict)],
    )
    by_ref = {
        f"order_candidate_{index}": choice for index, choice in enumerate(pool, start=1) if isinstance(choice, dict)
    }
    if resolution.status == "resolved":
        choice = by_ref.get(resolution.selected_ref)
        if choice is not None:
            return "resolved", choice, [], "semantic"
        return "unknown", None, [], ""
    if resolution.status == "ambiguous":
        refs = set(resolution.ambiguous_refs)
        selected = [choice for ref, choice in by_ref.items() if ref in refs]
        return ("ambiguous", None, selected, "") if len(selected) >= 2 else ("unknown", None, [], "")
    return "unknown", None, [], ""


async def _resolve_subject_description_semantically(
    request: Request,
    *,
    description: str,
    choices: list[dict[str, object]],
) -> tuple[str, dict[str, object] | None, list[dict[str, object]]]:
    """Use deterministic exact matching first, then candidate-only semantics."""

    resolution_kind, subject, matches = resolve_subject_description(description, choices)
    if resolution_kind == "unique" or len(choices) <= 1:
        return resolution_kind, subject, matches
    intent_router = getattr(request.app.state, "intent_router", None)
    pool = matches or choices
    resolution = await resolve_order_subject(
        getattr(intent_router, "llm", None),
        description,
        [item for item in pool if isinstance(item, dict)],
    )
    by_ref = {
        f"order_candidate_{index}": choice for index, choice in enumerate(pool, start=1) if isinstance(choice, dict)
    }
    if resolution.status == "resolved":
        selected = by_ref.get(resolution.selected_ref)
        return ("unique", selected, [selected] if selected is not None else [])
    if resolution.status == "ambiguous":
        refs = set(resolution.ambiguous_refs)
        narrowed = [choice for ref, choice in by_ref.items() if ref in refs]
        return ("multiple", None, narrowed) if len(narrowed) >= 2 else ("none", None, [])
    # If deterministic matching already narrowed the description to a
    # non-empty subset, an unavailable semantic resolver must not widen it or
    # discard it.  The safe result remains an ambiguous narrowed choice frame.
    return ("multiple", None, matches) if len(matches) >= 2 else ("none", None, [])


def _bound_subject_correction_case(
    active_case: SupportCase | None,
    recent_case: SupportCase | None,
) -> SupportCase | None:
    """只对当前绑定订单的活动 Case 或最近终态 Case 触发 correction 检测。"""
    if active_case is not None:
        if active_case.status not in {"ACTIVE", "AWAITING_CUSTOMER"}:
            # AWAITING_STAFF is a control-plane handoff.  A customer message
            # must not silently bypass that boundary by changing the subject.
            return None
        order_id = active_case.selected_subjects.get("order_id")
        if (
            isinstance(order_id, str)
            and order_id.startswith("SO")
            and active_case.pending.get("kind") != "customer_choice"
        ):
            return active_case
        # 有其他活动 Case 时不能把终态历史 Case 当成当前 subject。
        return None
    if recent_case is not None and recent_case.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        order_id = recent_case.selected_subjects.get("order_id")
        if isinstance(order_id, str) and order_id.startswith("SO"):
            return recent_case
    return None


async def _detect_customer_subject_correction(
    request: Request,
    *,
    case: SupportCase,
    query: str,
    history: list[dict[str, object]],
) -> SubjectCorrection:
    """调用独立 correction detector；不改动 IntentRouter 的 canonical 结果。"""
    detector = getattr(request.app.state, "subject_correction_detector", None)
    kwargs = {
        "query": query,
        "history": history,
        "current_subject_id": str(case.selected_subjects.get("order_id") or ""),
    }
    if callable(detector):
        try:
            value = detector(**kwargs)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, SubjectCorrection):
                return value
            from agent.subject_correction import parse_subject_correction

            return parse_subject_correction(value)
        except Exception:
            _chat_logger.warning("subject correction detector override failed")
            return SubjectCorrection(False)
    intent_router = getattr(request.app.state, "intent_router", None)
    llm = getattr(intent_router, "llm", None)
    return await detect_subject_correction(llm, **kwargs)


async def _prepare_customer_subject_correction(
    request: Request,
    *,
    case: SupportCase | None,
    query: str,
    history: list[dict[str, object]],
    customer_user_id: int,
    tool_context: ToolContext,
) -> tuple[str, SupportCase | None, dict[str, object] | None, list[dict[str, object]]]:
    """检测并准备 correction；返回 kind/case/unique subject/choice frame。"""
    if case is None or tool_context.role != "customer":
        return "none", case, None, []

    if case.pending.get("kind") == "subject_correction_description":
        continuation = await _continue_subject_correction_description(
            request,
            case=case,
            query=query,
            customer_user_id=customer_user_id,
            tool_context=tool_context,
        )
        if continuation is not None:
            return continuation

    correction = await _detect_customer_subject_correction(
        request,
        case=case,
        query=query,
        history=history,
    )
    if not correction.is_correction:
        return "none", case, None, []
    try:
        all_choices = await _customer_subject_choices(customer_user_id)
    except Exception:
        # 明确 correction 但候选查询不可用时，不让旧 subject 继续回答为新 subject。
        _chat_logger.warning("customer subject correction candidate lookup failed")
        return "unavailable", case, None, []
    old_order_id = case.selected_subjects.get("order_id")
    choices = [item for item in all_choices if item.get("order_id") != old_order_id]
    resolution_kind, subject, matches = await _resolve_subject_description_semantically(
        request,
        description=correction.subject_description,
        choices=choices,
    )
    if resolution_kind == "none":
        service = getattr(request.app.state, "support_case_service", None)
        if not isinstance(service, SupportCaseService):
            return "unavailable", case, None, []
        updated = await service.await_subject_correction_description(case)
        return ("description", updated, None, []) if updated is not None else ("unavailable", case, None, [])
    if resolution_kind == "multiple":
        service = getattr(request.app.state, "support_case_service", None)
        if not isinstance(service, SupportCaseService):
            return "unavailable", case, None, []
        updated = await service.prepare_subject_correction(
            case,
            choices=matches,
        )
        if updated is None:
            return "unavailable", case, None, []
        return "multiple", updated, None, matches
    if subject is None:
        return "unavailable", case, None, []
    order_id = subject.get("order_id")
    if not isinstance(order_id, str):
        return "none_found", case, None, []
    if not await _verify_customer_order_subject(request, order_id=order_id, tool_context=tool_context):
        return "unavailable", case, None, []
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return "unavailable", case, None, []
    if case.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        updated = await service.open_subject_correction_case(
            case,
            subject=subject,
            selection_source="subject_correction",
        )
    else:
        updated = await service.transition_customer_subject(
            case,
            subject=subject,
            selection_source="subject_correction",
        )
    if updated is None:
        return "unavailable", case, None, []
    return "unique", updated, subject, []


async def _continue_subject_correction_description(
    request: Request,
    *,
    case: SupportCase,
    query: str,
    customer_user_id: int,
    tool_context: ToolContext,
) -> tuple[str, SupportCase | None, dict[str, object] | None, list[dict[str, object]]] | None:
    """只把下一轮裸商品描述送入 correction resolver，不重新触发 canonical Router。"""
    if not looks_like_bare_subject_description(query):
        # 完整业务问题（例如“那 Sony 那个退款怎么样？”）继续走原有 Router，
        # 不把新请求误认为是对 pending correction 的补充描述。
        return None
    try:
        all_choices = await _customer_subject_choices(customer_user_id)
    except Exception:
        _chat_logger.warning("customer subject correction continuation lookup failed")
        return "unavailable", case, None, []
    pending = case.pending
    old_order_id = pending.get("transition_from_order_id") or case.selected_subjects.get("order_id")
    choices = [item for item in all_choices if item.get("order_id") != old_order_id]
    resolution_kind, subject, matches = await _resolve_subject_description_semantically(
        request,
        description=query,
        choices=choices,
    )
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return "unavailable", case, None, []
    if resolution_kind == "multiple":
        updated = await service.prepare_subject_correction(case, choices=matches)
        return ("multiple", updated, None, matches) if updated is not None else ("unavailable", case, None, [])
    if resolution_kind == "unique" and subject is not None:
        order_id = subject.get("order_id")
        if not isinstance(order_id, str) or not await _verify_customer_order_subject(
            request,
            order_id=order_id,
            tool_context=tool_context,
        ):
            return "unavailable", case, None, []
        updated = await service.transition_customer_subject(
            case,
            subject=subject,
            selection_source="subject_correction_description",
        )
        return ("unique", updated, subject, []) if updated is not None else ("unavailable", case, None, [])

    updated = await service.await_subject_correction_description(case)
    return ("description", updated, None, []) if updated is not None else ("unavailable", case, None, [])


def _subject_correction_intent(case: SupportCase, query: str) -> Intent:
    """从原 Case request stack 恢复 goal，避免 correction 文本再次触发 Router。"""
    return _intent_for_case_reply(
        case,
        Intent(
            target="agent",
            query=query,
            confidence=1.0,
            route_source="subject_correction",
            case_update="continue",
        ),
    )


def _subject_correction_short_circuit(
    kind: str,
    *,
    case: SupportCase,
    intent: Intent,
    choices: list[dict[str, object]],
) -> tuple[LoopResult, dict | None]:
    """构造 0/N candidate 的安全回答；不调用旧 subject 的 Workflow facts。"""
    if kind == "multiple":
        answer = "我找到多笔符合描述的订单，请选择你指的那一笔。"
        result = LoopResult(
            answer=answer,
            response_control={"mode": "ASK_CHOICE", "subject_id": None},
            workflow_progress={
                "goal_status": "awaiting_customer",
                "control_state": "AWAITING_CUSTOMER",
                "next_action": "ASK_CHOICE",
                "next_actor": "CUSTOMER",
                "pending_choices": choices,
            },
        )
        return result, _customer_presentation(result, intent, case=case)
    if kind == "description":
        answer = "我还没有找到符合描述的订单，请直接告诉我商品品牌、型号或订单号，我再帮你核对。"
    elif kind == "none_found":
        # Keep the legacy branch defensive for old callers; new correction misses
        # are persisted as subject_correction_description before reaching here.
        answer = "我没有在你的订单中找到符合这个描述的订单，请重新描述商品。"
    else:
        answer = "目前无法核验你要找的订单，请稍后重试或重新描述商品。"
    return LoopResult(answer=answer), None


def _customer_presentation(
    result: LoopResult,
    intent: Intent,
    *,
    case: SupportCase | None = None,
    ticket_id: str | None = None,
) -> dict | None:
    """把受控结果投影成 API 可公开的 presentation，并缓存到 LoopResult。"""
    presentation = build_customer_presentation(
        result,
        _support_case_payloads(intent),
        case=case,
        ticket_id=ticket_id,
    )
    result.customer_presentation = presentation or {}
    return presentation


def _awaiting_staff_response(case: SupportCase) -> tuple[LoopResult, dict | None, str | None]:
    """Render an actual ticket handoff; tolerate legacy invalid Cases safely."""
    ticket_id = _case_ticket_id(case)
    if ticket_id:
        answer = (
            f"这项问题已经转人工处理，工单 {ticket_id} 正在处理中。"
            "可以在售后进度中继续补充问题细节。\n\n"
            "[查看售后进度](?page=tickets)"
        )
    else:
        answer = "当前自动流程暂时无法继续处理。如需人工客服协助，请回复“转人工”。"
    result = LoopResult(
        answer=answer,
        response_control={"mode": "STAFF_HANDOFF" if ticket_id else "FACT", "subject_id": None},
        workflow_progress={
            "goal_status": "blocked",
            "control_state": "BLOCKED",
            "next_actor": "STAFF",
            "next_action": "STAFF_HANDOFF",
            "reason": "case_already_handed_off",
        },
    )
    presentation = (
        _customer_presentation(
            result,
            Intent(target="agent", query=answer, confidence=1.0),
            case=case,
            ticket_id=ticket_id,
        )
        if ticket_id
        else None
    )
    return result, presentation, ticket_id


async def _merge_new_support_request(
    request: Request,
    *,
    case: SupportCase | None,
    intent: Intent,
) -> SupportCase | None:
    """Retire an automated task frame before an independent new request."""
    workflow_requests = intent.workflow_requests
    if case is None or not workflow_requests:
        return case
    current_is_order_list = any(
        str(item.domain if hasattr(item, "domain") else item.get("domain") or "") == "order"
        and str(item.operation if hasattr(item, "operation") else item.get("operation") or "") == "list"
        for item in workflow_requests
    )
    case_has_non_list_goal = any(
        not (
            isinstance(item, dict)
            and str(item.get("domain") or "") == "order"
            and str(item.get("operation") or "") == "list"
        )
        for item in case.request_stack
    )
    # ``order.list`` is an independent browse task.  It must not be appended to
    # an old refund/delivery frame even if the Router conservatively says
    # ``continue`` because a recent subject is present.
    independent_order_list = current_is_order_list and case_has_non_list_goal
    if intent.case_update != "new_request" and not independent_order_list:
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    superseded = await service.supersede_for_new_request(case)
    return None if superseded is not None and superseded.status == "CANCELLED" else case


async def _mark_support_case_awaiting_staff(
    request: Request,
    *,
    case: SupportCase | None,
    ticket_id: str,
) -> None:
    """在工单创建成功后更新 Case；失败不否认已创建的工单。"""
    if case is None:
        return
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return
    await service.mark_awaiting_staff(
        case,
        reason="CUSTOMER_CONFIRMED_HUMAN_HANDOFF",
        handoff_summary={"ticket_id": ticket_id, "request_count": len(case.request_stack)},
    )


async def _await_support_case_customer(
    request: Request,
    *,
    case: SupportCase | None,
    intent: Intent,
    workflow_progress: dict[str, object] | None = None,
) -> SupportCase | None:
    """将复杂请求的下一轮语义显式保存，避免短回复退化为新问题。"""
    if case is None or case.status == "AWAITING_STAFF":
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    requests = intent.workflow_requests
    if not requests:
        return case
    primary = requests[0]
    execution_status = str((workflow_progress or {}).get("goal_status") or "")
    next_action = str((workflow_progress or {}).get("next_action") or "")
    request_payloads = _support_case_payloads(intent)
    requires_confirmation = confirmation_required(request_payloads)
    missing_facts = (workflow_progress or {}).get("missing_facts")
    pending = {
        "kind": (
            "execution_blocked"
            if execution_status in {"blocked", "unresolved"}
            else "customer_clarification"
            if next_action == "ASK_CLARIFICATION"
            else "customer_confirmation"
            if requires_confirmation
            else "customer_choice"
        ),
        "operation": primary.operation,
        "next_step": next_action,
        "missing_facts": ([str(item) for item in missing_facts] if isinstance(missing_facts, list) else []),
        "options_limit": MAX_ORDER_CHOICE_OPTIONS,
    }
    if workflow_progress:
        pending["execution"] = workflow_progress
    if pending["kind"] == "customer_choice":
        choices = workflow_progress.get("pending_choices") if workflow_progress else None
        if isinstance(choices, list) and choices:
            # 这个顺序就是 SupportWorkflow 实际展示给客户的顺序，后续序号解析
            # 只能读取这一帧，不能重新查询或排序。
            pending["subject_type"] = "order"
            pending["choices"] = choices[:MAX_ORDER_CHOICE_OPTIONS]
    pending_command: dict[str, object] = {}
    if requires_confirmation and execution_status == "awaiting_confirmation":
        pending_command = {
            "status": "PROPOSED_NOT_EXECUTED",
            "operation": primary.operation,
            "risk": "customer_confirmation",
            "requires_explicit_confirmation": True,
        }
    return await service.await_customer(
        case,
        pending=pending,
        pending_command=pending_command,
        request_stack=request_payloads,
    )


async def _record_support_case_facts(
    request: Request,
    *,
    case: SupportCase | None,
    loop_result: LoopResult,
) -> SupportCase | None:
    """把 Workflow 的受控只读结果持久化为下一轮可复用的事实。"""
    facts = loop_result.decision_facts or loop_result.verified_facts
    if case is None or (not facts and not loop_result.decision_contexts):
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    # 乐观锁竞争时不使用旧 Case 覆盖另一标签页的新选择；本轮仍可完成回答，下一轮
    # 会重新读取最新状态。
    kwargs: dict[str, object] = {"facts": facts}
    selected_subjects = loop_result.workflow_progress.get("selected_subjects")
    if isinstance(selected_subjects, dict):
        selected_order_id = selected_subjects.get("order_id")
        if isinstance(selected_order_id, str) and selected_order_id.startswith("SO"):
            kwargs["selected_subjects"] = {"order_id": selected_order_id}
    if loop_result.decision_contexts:
        kwargs["decision_contexts"] = loop_result.decision_contexts
    return await service.record_verified_facts(case, **kwargs)


def _support_case_needs_customer_turn(
    intent: Intent,
    case: SupportCase | None = None,
    workflow_progress: dict[str, object] | None = None,
) -> bool:
    """判断 Workflow 是否确实还需要客户选择/补充/确认。"""
    next_actor = str((workflow_progress or {}).get("next_actor") or "")
    if next_actor:
        return next_actor == "CUSTOMER"
    progress_status = str((workflow_progress or {}).get("goal_status") or "")
    # 客户恢复 pending 后，Case 仍会暂存旧问题供本轮模型理解；旧 pending 本身
    # 不能覆盖当前执行评估，否则即使事实已核验完成，Case 也会永远停在等待客户。
    if progress_status == "resolved":
        return False
    if progress_status in {"blocked", "unresolved", "awaiting_customer", "awaiting_confirmation"}:
        return True
    if progress_status == "resolved_with_explanation":
        return False
    if case is not None and case.pending:
        # 现有 pending 是案件状态，不会因用户提出另一件事而被覆盖或提前完成。
        return True
    requests = intent.workflow_requests
    if not requests:
        return False
    for item in requests:
        if item.risk != "read_only":
            return True
        if item.missing_facts:
            return True
        if item.next_step in {"ASK_CLARIFICATION", "ASK_CHOICE", "CONFIRM", "ESCALATE"}:
            return True
        if item.operation in {
            "after_sales_transition",
            "return_logistics",
            "delivery_instruction",
            "delivery_exception",
            "refund_request",
            "human_handoff",
        }:
            return True
    return False


async def _persist_support_case_progress(
    request: Request,
    *,
    case: SupportCase | None,
    intent: Intent,
    loop_result: LoopResult,
) -> SupportCase | None:
    """在记录事实后确定性地结束 Case，或保存下一轮待处理状态。"""
    case = await _record_support_case_facts(request, case=case, loop_result=loop_result)
    if case is None:
        return None
    workflow_progress = loop_result.workflow_progress
    next_actor = str(workflow_progress.get("next_actor") or "")
    if next_actor == "STAFF":
        # Workflow capability gaps never create an AWAITING_STAFF case.  The
        # only transition to that state is `_mark_support_case_awaiting_staff`
        # after `_create_customer_ticket` succeeds.  Keep this defensive branch
        # terminal too, so an older Workflow result cannot reintroduce a
        # session-absorbing staff Case.
        workflow_progress["next_actor"] = "NONE"
        workflow_progress["next_action"] = "EXPLAIN_LIMITATION_OR_HANDOFF"
    if next_actor == "SYSTEM" and str(workflow_progress.get("goal_status") or "") == "unresolved":
        # A replan is local to this Workflow invocation.  If it reaches the API
        # boundary unresolved, finish the Case with a safe limitation instead
        # of persisting an artificial staff handoff.
        workflow_progress["next_actor"] = "NONE"
    if _support_case_needs_customer_turn(intent, case, loop_result.workflow_progress):
        updated = await _await_support_case_customer(
            request,
            case=case,
            intent=intent,
            workflow_progress=loop_result.workflow_progress,
        )
        return updated or case
    service = getattr(request.app.state, "support_case_service", None)
    if isinstance(service, SupportCaseService):
        resolution_type = str(loop_result.workflow_progress.get("resolution_type") or "")
        updated = await service.complete(
            case,
            outcome={
                "completion": resolution_type
                or (
                    "safe_partial_answer_returned"
                    if str(loop_result.workflow_progress.get("goal_status") or "") == "resolved_with_limitation"
                    else "read_only_answer_returned"
                ),
                "resolution_type": resolution_type or None,
                "request_count": len(intent.workflow_requests),
                "execution": loop_result.workflow_progress,
            },
        )
        return updated or case
    return case


async def _fail_support_case(
    request: Request,
    *,
    case: SupportCase | None,
    error: BaseException,
) -> None:
    """记录 Workflow 失败，不把异常堆栈或工具原始报文写入 Case。"""
    if case is None:
        return
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return
    try:
        await service.fail(case, reason=f"{type(error).__name__}: workflow execution failed")
    except Exception:
        _chat_logger.exception("support case failure state persistence failed")


def _chat_run_key(session_id: str) -> str:
    """返回单个会话的最新流式运行标识键。"""
    return f"chat:active-run:{session_id}"


async def _claim_chat_run(session_id: str) -> str | None:
    """将本次请求标记为会话最新运行，供旧流主动让位。

    测试中未初始化 Redis 时返回 None，保留现有内存测试的纯 HTTP 边界；实际应用
    已在启动时初始化 Redis，运行令牌会在五分钟后自动过期。
    """
    try:
        redis = get_redis()
    except RuntimeError:
        return None

    run_id = uuid.uuid4().hex
    try:
        await redis.set(_chat_run_key(session_id), run_id, ex=_CHAT_RUN_TTL_SECONDS)
    except Exception as exc:
        raise DependencyUnavailableError("会话协调服务暂时不可用") from exc
    return run_id


async def _is_current_chat_run(session_id: str, run_id: str | None) -> bool:
    """确认当前流仍是该会话最后一次发送的请求。"""
    if run_id is None:
        return True
    try:
        value = await get_redis().get(_chat_run_key(session_id))
    except Exception as exc:
        raise DependencyUnavailableError("会话协调服务暂时不可用") from exc
    if isinstance(value, bytes):
        value = value.decode()
    return value == run_id


def _superseded_stream_event(request: Request) -> dict[str, str]:
    """通知旧标签页：同一会话已有更新请求，当前流不再落库。"""
    request_id = getattr(request.state, "request_id", None) or get_request_id()
    return {"event": "superseded", "message": "此会话已在另一窗口继续生成", "request_id": request_id}


def _stream_error_event(
    request: Request,
    *,
    message: str = _STREAM_ERROR_MESSAGE,
) -> dict[str, str]:
    request_id = getattr(request.state, "request_id", None) or get_request_id()
    return {
        "event": "error",
        "code": "DEPENDENCY_UNAVAILABLE",
        "message": message,
        "request_id": request_id,
    }


def _build_ticket_issue(
    current_query: str,
    history: list[dict] | None = None,
) -> str:
    """为工单保留最近客户原话，避免只把触发升级的最后一句当成问题。"""
    customer_messages = [
        str(message.get("content", "")).strip()
        for message in (history or [])[-12:]
        if message.get("role") == "user" and str(message.get("content", "")).strip()
    ]
    current = current_query.strip()
    if current:
        customer_messages.append(current)

    # 仅保留最近几条，防止很长的历史把不相关的商品咨询带进售后工单。
    customer_messages = customer_messages[-4:]
    if not customer_messages:
        return current
    if len(customer_messages) == 1:
        return customer_messages[0]
    return "客户售后诉求（按时间）：\n" + "\n".join(
        f"{index}. {message[:800]}" for index, message in enumerate(customer_messages, start=1)
    )


async def _create_customer_ticket(
    request: Request,
    *,
    issue: str,
    tool_context: ToolContext,
    history: list[dict] | None = None,
) -> tuple[str, str]:
    """以当前登录客户身份创建售后工单并生成可直接展示的结果。

    Args:
        request: 当前 HTTP 请求，用于取得服务端注册的受控工具。
        issue: 已完成会话消解的客户原始诉求。
        tool_context: 由服务端构造的当前客户身份，不接受客户端传入的身份。

    Returns:
        新工单编号和展示给客户的确认消息。

    Raises:
        DependencyUnavailableError: 工单工具不可用或未返回有效工单编号。
    """
    registry = request.app.state.registry
    ticket_issue = _build_ticket_issue(issue, history)
    # 仅这个函数会在售后策略已经返回 CREATE_TICKET 后被调用。普通 Agent Loop
    # 使用的 context 会阻止 create_ticket，避免模型绕过聊天入口的动作闸门。
    authorized_tool_context = ToolContext(
        user_id=tool_context.user_id,
        role=tool_context.role,
        ticket_queue_status="待人工处理",
    )
    result = await registry.execute(
        "create_ticket",
        tool_context=authorized_tool_context,
        issue=ticket_issue,
        urgency="medium",
    )
    ticket_id = str(result.data.get("ticket_id") or "") if result.is_success else ""
    if not ticket_id:
        raise DependencyUnavailableError("工单服务暂时不可用")

    # 这里的工单来自聊天入口的人工边界决策，不能再落入 AI worker 队列。
    # 只有正式应用注册了 SupportCaseService 时才执行队列迁移；保留没有完整
    # lifespan 的 HTTP 单元测试和兼容调用的原有行为。
    if isinstance(getattr(request.app.state, "support_case_service", None), SupportCaseService):
        escalation_reason = classify_ticket_escalation(ticket_issue) or TicketEscalationReason.MODEL_ESCALATION
        queued = await enqueue_human_ticket(ticket_id, escalation_reason=escalation_reason)
        if not queued:
            raise DependencyUnavailableError("人工客服队列暂时不可用")

    return ticket_id, f"已为您创建售后工单 {ticket_id}，已转人工客服处理。可以在售后进度中继续补充问题细节。"


def _build_context(docs: list[dict], *, customer_view: bool) -> str:
    """把检索结果拼成上下文字符串"""
    if not docs:
        return "（未找到相关内容）"
    lines = []
    for doc in docs[: settings.rerank_top_k]:
        title = doc.get("title", "?")
        content = str(doc.get("content", ""))
        if customer_view:
            content = re.sub(r"(?:库存|仓库|[\u4e00-\u9fa5]+仓)[^。；\n]*[。；]?", "", content)
        content = content[:300]
        lines.append(f"[来源: {title} {content}]")
    return "\n-----\n".join(lines)


async def _pre_route_knowledge_context(query: str) -> str:
    """轻量 Pre-RAG：只辅助 Router 理解术语，不作为最终回答依据。"""
    try:
        docs = await pre_retrieve_knowledge(query, top_k=3)
    except Exception as exc:  # 知识不可用时 fail closed：不给 Router 任何未审计来源。
        _chat_logger.warning("pre-rag unavailable error_type=%s", type(exc).__name__)
        return ""
    return format_knowledge_context(docs, max_docs=3)


async def _deep_knowledge_context(query: str, *, required: bool) -> str:
    """按 EvidencePlan 获取供解答使用的知识证据。"""
    if not required:
        return ""
    try:
        docs = await hybrid_search(query, table="knowledge_chunks", top_k=5, use_rerank=True)
    except Exception as exc:
        _chat_logger.warning("deep-rag unavailable error_type=%s", type(exc).__name__)
        return ""
    return format_knowledge_context(docs, max_docs=5)


_CATALOG_TABLES = ("laptop_products", "phone_products", "component_products")
_CATALOG_TABLE_BY_CATEGORY = {
    "laptops": "laptop_products",
    "phones": "phone_products",
    "components": "component_products",
}


def _catalog_tables_for_evidence(intent: Intent, entities: dict[str, Any]) -> tuple[str, ...]:
    """Choose catalog sources from server state, never from raw-query keywords."""
    category_table = _CATALOG_TABLE_BY_CATEGORY.get(getattr(intent, "product_category", ""))
    if category_table:
        return (category_table,)
    if intent.table in _CATALOG_TABLES:
        return (intent.table,)
    candidates = entities.get("product_candidates")
    if isinstance(candidates, list):
        categories = {
            str(candidate.get("product_category") or "") for candidate in candidates if isinstance(candidate, dict)
        }
        categories.discard("")
        if len(categories) == 1:
            table = _CATALOG_TABLE_BY_CATEGORY.get(next(iter(categories)))
            if table:
                return (table,)
    return _CATALOG_TABLES


async def _acquire_catalog_evidence(
    *,
    required: bool,
    intent: Intent,
    query: str,
    entities: dict[str, Any],
    canonical_product: bool,
    product_context: dict[str, Any] | None = None,
    reuse_existing_frame: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Acquire one authoritative ecommerce candidate pool.

    Hard eligibility lives in Product Context and is applied before semantic
    recommendation.  Natural ecommerce turns acquire a fresh bounded frame so
    new preference/model evidence can change ranking; only an ordinal choice
    reuses the exact frame the customer previously saw.  Semantic search may
    rank eligible products but can never add an ineligible product.
    """
    if not required or canonical_product:
        return [], False
    candidates = entities.get("product_candidates")
    if (
        reuse_existing_frame
        and isinstance(candidates, list)
        and dedupe_product_candidates([candidate for candidate in candidates if isinstance(candidate, dict)])
    ):
        # Ordinal/structured follow-ups must resolve against exactly the frame
        # the customer saw.  Natural recommendation/refinement turns acquire a
        # fresh frame so new semantic evidence cannot be trapped in stale top-N.
        return [], True

    context = product_context or {}
    category = str(context.get("category") or "")
    has_structured_budget = any(
        isinstance(context.get(key), int) for key in ("min_price_cents", "max_price_cents", "target_price_cents")
    )
    has_structured_filter = has_structured_budget or bool(context.get("brand_keys"))
    if category in {"laptops", "phones", "components"} and has_structured_filter:
        try:
            rows = await list_product_candidates(
                category,
                min_price_cents=context.get("min_price_cents")
                if isinstance(context.get("min_price_cents"), int)
                else None,
                max_price_cents=context.get("max_price_cents")
                if isinstance(context.get("max_price_cents"), int)
                else None,
                target_price_cents=context.get("target_price_cents")
                if isinstance(context.get("target_price_cents"), int)
                else None,
                # This is the authoritative eligibility pool, not the prompt
                # frame.  Keep it broad enough that semantic pre-ranking can
                # still surface an eligible model such as a mid-range variant.
                limit=100,
            )
        except Exception as exc:
            _chat_logger.warning(
                "catalog candidate acquisition unavailable category=%s error_type=%s",
                category,
                type(exc).__name__,
            )
            rows = []
        filtered = filter_candidates_by_context([row for row in rows if isinstance(row, dict)], context)
        if filtered:
            # Hard constraints decide eligibility.  Within that authoritative
            # pool, reuse the existing Catalog retrieval only as a semantic
            # pre-ranker so a bounded ProductResolver frame does not accidentally
            # omit a clearly relevant eligible model.  Retrieval can reorder but
            # can never add an ineligible product.
            ranked: list[dict[str, Any]] = []
            if len(filtered) > 12:
                preference_turns = [
                    str(item).strip()
                    for item in context.get("preference_turns", [])[-4:]
                    if isinstance(item, str) and item.strip()
                ]
                semantic_query = "；".join(preference_turns)[-1800:] or query
                table = _CATALOG_TABLE_BY_CATEGORY.get(category, "")
                if table and semantic_query.strip():
                    try:
                        evidence_rows = await hybrid_search(
                            semantic_query,
                            table=table,
                            top_k=max(24, min(settings.retrieval_top_k * 2, 50)),
                            use_rerank=_should_rerank(semantic_query, table),
                        )
                    except Exception as exc:
                        _chat_logger.warning(
                            "catalog candidate ranking unavailable table=%s error_type=%s",
                            table,
                            type(exc).__name__,
                        )
                        evidence_rows = []
                    eligible_by_key = {
                        (
                            str(candidate.get("product_category") or ""),
                            str(candidate.get("product_id") or ""),
                        ): candidate
                        for candidate in filtered
                    }
                    seen_ranked: set[tuple[str, str]] = set()
                    for evidence in evidence_rows:
                        candidate = canonical_product_candidate(evidence) if isinstance(evidence, dict) else None
                        if candidate is None:
                            continue
                        key = (str(candidate.get("product_category") or ""), str(candidate.get("product_id") or ""))
                        eligible = eligible_by_key.get(key)
                        if eligible is None or key in seen_ranked:
                            continue
                        seen_ranked.add(key)
                        ranked.append(eligible)
                    ranked.extend(
                        candidate
                        for candidate in filtered
                        if (str(candidate.get("product_category") or ""), str(candidate.get("product_id") or ""))
                        not in seen_ranked
                    )
            # Candidate order is server-owned and becomes the stable ordinal
            # frame used by the resolver and later customer choice.
            return (ranked or filtered)[:12], True
        # Explicit hard constraints that produce no products are themselves an
        # authoritative empty result.  Do not silently broaden the search.
        if any(context.get(key) not in (None, "", []) for key in ("min_price_cents", "max_price_cents", "brand_keys")):
            return [], True

    docs: list[dict[str, Any]] = []
    for table in _catalog_tables_for_evidence(intent, entities):
        try:
            rows = await hybrid_search(
                query,
                table=table,
                top_k=settings.retrieval_top_k,
                use_rerank=_should_rerank(query, table),
            )
        except Exception as exc:
            _chat_logger.warning(
                "catalog evidence unavailable table=%s error_type=%s",
                table,
                type(exc).__name__,
            )
            continue
        docs.extend(row for row in rows if isinstance(row, dict))

    if not docs:
        return [], False

    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    # hybrid_search already owns ranking (vector/BM25/RRF/rerank).  Preserve
    # that order rather than re-sorting by one component score.
    for doc in docs:
        candidate = canonical_product_candidate(doc)
        if candidate is None:
            continue
        key = (str(candidate["product_category"]), str(candidate["product_id"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(doc)
        if len(unique) >= 12:
            break
    return unique, bool(unique)


def _candidate_refs(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"candidate_{index}": candidate for index, candidate in enumerate(candidates, start=1)}


def _visible_product_refs(
    candidates: list[dict[str, Any]],
    resolution: ProductResolution,
) -> list[str]:
    """Return the only opaque refs the Operator may act on this turn."""
    by_ref = _candidate_refs(candidates)
    if resolution.status == "selected" and resolution.selected_ref in by_ref:
        return [resolution.selected_ref]
    if resolution.status == "ambiguous" and resolution.ambiguous_refs:
        return [ref for ref in resolution.ambiguous_refs if ref in by_ref]
    return list(by_ref.keys())


def _product_procedure_context(
    *,
    product_context: dict[str, Any],
    candidates: list[dict[str, Any]],
    resolution: ProductResolution,
) -> str:
    """Expose one authoritative catalog frame to the Ecommerce Operator.

    ProductResolver owns only subject resolution.  When no product subject is
    selected, the Operator may recommend freely *inside* this server-owned
    frame.  Any ordered list it presents must be declared through the
    ``present_product_candidates`` tool so the server can validate refs, persist
    ordinal continuity, and generate detail actions without parsing prose.
    """
    by_ref = _candidate_refs(candidates)
    visible_refs = _visible_product_refs(candidates, resolution)

    public_candidates: list[dict[str, Any]] = []
    for source_index, ref in enumerate(visible_refs[:12], start=1):
        raw = by_ref.get(ref)
        candidate = canonical_product_candidate(raw) if raw is not None else None
        if candidate is None:
            continue
        item: dict[str, Any] = {
            "source_index": source_index,
            "ref": ref,
            "name": candidate["product"],
        }
        if isinstance(candidate.get("price_cents"), int):
            item["price_yuan"] = candidate["price_cents"] / 100
        attributes = candidate.get("public_attributes")
        if isinstance(attributes, Mapping) and attributes:
            item["attributes"] = {
                str(key)[:80]: str(value)[:240]
                for key, value in list(attributes.items())[:48]
                if str(key).strip() and str(value).strip()
            }
        summary = candidate.get("summary")
        if isinstance(summary, str) and summary.strip():
            item["summary"] = summary[:700]
        public_candidates.append(item)

    payload = {
        "constraints": {
            **({"category": product_context.get("category")} if product_context.get("category") else {}),
            **(
                {"min_price_yuan": product_context["min_price_cents"] / 100}
                if isinstance(product_context.get("min_price_cents"), int)
                else {}
            ),
            **(
                {"max_price_yuan": product_context["max_price_cents"] / 100}
                if isinstance(product_context.get("max_price_cents"), int)
                else {}
            ),
            **(
                {"target_price_yuan": product_context["target_price_cents"] / 100}
                if isinstance(product_context.get("target_price_cents"), int)
                else {}
            ),
            **({"brand_keys": product_context.get("brand_keys")} if product_context.get("brand_keys") else {}),
        },
        "preference_turns": product_context.get("preference_turns", [])[-6:],
        "subject_resolution": {
            "status": resolution.status,
            "selected_ref": resolution.selected_ref or None,
            "ambiguous_refs": resolution.ambiguous_refs,
        },
        "eligible_candidate_count": len(candidates),
        "candidates": public_candidates,
    }
    return (
        "Ecommerce Role 当前商品候选帧（服务端可信）。price_yuan/attributes/summary 来自当前 Catalog，"
        "不得编造缺失的价格、库存或规格，也不得推荐 candidates 之外的商品。"
        "ProductResolver 这里只负责商品指代，不负责推荐。"
        "subject_resolution.status=ambiguous 时，只展示给出的 candidates 并让客户选择；"
        "status=selected 时只能把唯一 candidate 当作当前选中商品；"
        "status=unknown 时表示尚未选中具体商品，不代表不能推荐。"
        "开放式导购（例如‘性能最好’、‘性价比高’、‘随便推荐几款’）由你根据客户原话和候选真实属性自行判断，"
        "不要求存在唯一客观最优，也不要仅因为存在多个合理选项就反复追问。"
        "当你准备向客户展示任何有顺序的商品列表时，必须先调用 present_product_candidates："
        "推荐列表用 mode=recommend，需要客户在多个配置/选项中继续选择时用 mode=choice；"
        "candidate_refs 必须严格按你随后向客户展示的顺序提交。工具成功后按同一顺序回答。"
        "这个工具只声明展示顺序，不代表客户已经选择商品。"
        "如果客户明确要求列出全部候选，可以展示全部；如果某个主观维度缺少可靠属性，可说明比较依据有限，"
        "但不要因此把正常导购变成无限澄清。eligible_candidate_count=0 才能说明当前目录没有满足硬约束的商品。\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


async def _resolve_product_turn(
    *,
    resolver: ProductResolver | None,
    query: str,
    candidates: list[dict[str, Any]],
    product_context: dict[str, Any],
    history: list[dict[str, Any]],
    previous_selected: dict[str, str] | None = None,
    purpose: str = "auto",
) -> tuple[ProductResolution, dict[str, str] | None, dict[str, Any]]:
    """Resolve product semantics inside one authoritative candidate frame."""
    if not candidates:
        return ProductResolution(), None, set_choice_refs(product_context, [])
    ordinal_ref = ordinal_choice_ref(query, product_context)
    if ordinal_ref:
        selected = candidate_for_ref(ordinal_ref, candidates)
        if selected is not None:
            identity = canonical_product_identity(selected)
            return (
                ProductResolution(status="selected", selected_ref=ordinal_ref),
                identity,
                set_choice_refs(product_context, []),
            )

    matched = match_product_candidate(query, candidates)
    if matched is not None:
        for ref, raw in _candidate_refs(candidates).items():
            identity = canonical_product_identity(raw)
            if identity and (identity["product_category"], identity["product_id"]) == (
                matched["product_category"],
                matched["product_id"],
            ):
                return (
                    ProductResolution(status="selected", selected_ref=ref),
                    matched,
                    set_choice_refs(product_context, []),
                )

    if resolver is None:
        return ProductResolution(), None, set_choice_refs(product_context, [])
    resolution = await resolver.resolve(
        query=query,
        candidates=candidates,
        product_context=product_context,
        history=history,
        previous_selected=previous_selected,
        purpose=purpose,
    )
    selected: dict[str, str] | None = None
    choice_refs: list[str] = []
    if resolution.status == "selected":
        candidate = candidate_for_ref(resolution.selected_ref, candidates)
        selected = canonical_product_identity(candidate) if candidate is not None else None
    elif resolution.status == "ambiguous":
        choice_refs = resolution.ambiguous_refs
    return resolution, selected, set_choice_refs(product_context, choice_refs)


async def _prepare_ecommerce_role(
    *,
    intent: Intent,
    raw_query: str,
    retrieval_query: str,
    entities: dict[str, Any],
    history: list[dict[str, Any]],
    explicit_product: dict[str, str] | None,
    catalog_required: bool,
    resolver: ProductResolver | None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any], ProductResolution, dict[str, str] | None, dict[str, Any]]:
    """Run the bounded Ecommerce Role before Operator generation."""
    existing_context = product_context_from_entities(entities)
    pending_ordinal_choice = ordinal_choice_ref(raw_query, existing_context) is not None
    is_product_turn = (
        intent.domain == "product"
        or intent.table in _CATALOG_TABLES
        or getattr(intent, "product_category", "") in {"phones", "laptops", "components"}
        or pending_ordinal_choice
    )
    if not is_product_turn:
        # Service/other roles may keep ecommerce history in the shared session,
        # but this turn must not mutate ProductContext, refresh candidates, or
        # create a product-navigation authority.
        return [], False, existing_context, ProductResolution(), None, {}

    product_context, hard_changed = update_product_context(
        existing_context,
        query=raw_query,
        table=intent.table,
        category=getattr(intent, "product_category", ""),
        is_product_turn=True,
    )

    if explicit_product is not None:
        product_context["category"] = explicit_product.get("product_category", product_context.get("category", ""))
        return (
            [],
            False,
            product_context,
            ProductResolution(status="selected"),
            explicit_product,
            {
                **product_context_entity(product_context),
                **explicit_product,
            },
        )

    # A previously selected product is trusted continuity for a product purchase/navigation
    # turn.  It is retired automatically below when hard constraints refresh the candidate frame.
    stored = stored_product_identity(entities)
    if stored is not None and not hard_changed and intent.domain == "product" and intent.operation == "purchase":
        return (
            [],
            False,
            product_context,
            ProductResolution(status="selected"),
            stored,
            {
                **product_context_entity(product_context),
                **stored,
            },
        )

    catalog_docs, catalog_acquired = await _acquire_catalog_evidence(
        required=catalog_required and is_product_turn,
        intent=intent,
        query=retrieval_query,
        entities=entities,
        canonical_product=False,
        product_context=product_context,
        reuse_existing_frame=pending_ordinal_choice and not hard_changed,
    )
    if catalog_docs:
        candidates = dedupe_product_candidates(catalog_docs)[:12]
    else:
        candidates = dedupe_product_candidates(
            [item for item in entities.get("product_candidates", []) if isinstance(item, dict)]
        )[:12]
        if hard_changed:
            # Hard constraints changed but acquisition returned an authoritative
            # empty set; stale candidates must not survive into this turn.
            candidates = []
    # Keep a previously server-validated selected product inside the semantic
    # frame while hard eligibility is unchanged.  This gives ProductResolver a
    # bounded previous_selected_ref for pronouns/navigation without making the
    # old selection authoritative over a new recommendation request.
    if stored is not None and not hard_changed and candidates:
        stored_key = (stored.get("product_category"), stored.get("product_id"))
        candidate_keys = {(candidate.get("product_category"), candidate.get("product_id")) for candidate in candidates}
        if stored_key not in candidate_keys:
            previous_candidates = dedupe_product_candidates(
                [item for item in entities.get("product_candidates", []) if isinstance(item, dict)]
            )
            previous_candidate = next(
                (
                    candidate
                    for candidate in previous_candidates
                    if (candidate.get("product_category"), candidate.get("product_id")) == stored_key
                ),
                None,
            )
            if previous_candidate is not None and filter_candidates_by_context([previous_candidate], product_context):
                candidates = [previous_candidate, *candidates[:11]]

    product_context = attach_candidate_frame(product_context, candidates)

    resolution = ProductResolution()
    selected: dict[str, str] | None = None
    if is_product_turn and candidates:
        resolution, selected, product_context = await _resolve_product_turn(
            resolver=resolver,
            query=raw_query,
            candidates=candidates,
            product_context=product_context,
            history=history,
            previous_selected=stored,
            purpose={
                "answer": "inspect",
                "purchase": "purchase",
            }.get(intent.operation, "auto"),
        )

    entity_projection: dict[str, Any] = product_context_entity(product_context)
    if candidates:
        entity_projection["product_candidates"] = candidates
    elif hard_changed:
        # An empty authoritative frame must clear prior product candidates.
        entity_projection["product_candidates"] = []
    if selected is not None:
        entity_projection.update(selected)
    return catalog_docs, catalog_acquired, product_context, resolution, selected, entity_projection


def _merge_evidence_context(*contexts: str) -> str:
    """保留来源边界地合并产品上下文和知识上下文。"""
    return "\n\n".join(context.strip() for context in contexts if context and context.strip())


def _presented_product_refs(
    verified_facts: Mapping[str, Any] | None,
    candidates: list[dict[str, Any]],
    *,
    allowed_refs: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """Read the Operator's structured presentation declaration and revalidate refs.

    The declaration tool is only a structured output channel.  This function is
    still the final server membership check against the current candidate frame.
    """
    if not isinstance(verified_facts, Mapping):
        return "", []
    raw = verified_facts.get("present_product_candidates")
    if not isinstance(raw, Mapping) or raw.get("status") != "success":
        return "", []
    data = raw.get("data")
    if not isinstance(data, Mapping):
        return "", []
    mode = str(data.get("mode") or "")
    if mode not in {"recommend", "choice"}:
        return "", []
    refs = data.get("candidate_refs")
    if not isinstance(refs, list):
        return "", []
    allowed = set(allowed_refs) if allowed_refs is not None else set(_candidate_refs(candidates))
    validated: list[str] = []
    for ref in refs[:12]:
        ref = str(ref)
        if ref in allowed and ref not in validated:
            validated.append(ref)
    return (mode, validated) if validated else ("", [])


def _recommended_products_from_refs(
    refs: Sequence[str],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve validated recommendation refs back to canonical catalog candidates."""
    products: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ref in list(refs)[:12]:
        candidate = candidate_for_ref(str(ref), candidates)
        if candidate is None:
            continue
        key = (str(candidate.get("product_category") or ""), str(candidate.get("product_id") or ""))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        products.append(candidate)
    return products


def _recommended_product_action_suffix(products: list[dict[str, Any]]) -> str:
    actions: list[str] = []
    for product in products[:12]:
        candidate = canonical_product_candidate(product)
        if candidate is None:
            continue
        category = str(candidate.get("product_category") or "")
        product_id = str(candidate.get("product_id") or "")
        if category not in {"laptops", "phones", "components"} or not product_id:
            continue
        name = str(candidate.get("product") or candidate.get("product_name") or "这款商品").strip()
        # Catalog text is trusted business data, but escape Markdown link-label
        # delimiters so unusual product names cannot break presentation syntax.
        label = name.replace("[", "［").replace("]", "］")[:120]
        href = f"?page=product&category={quote(category)}&product={quote(product_id)}"
        actions.append(f"[查看 {label}]({href})")
    if not actions:
        return ""
    if len(actions) == 1:
        return "\n\n" + actions[0]
    return "\n\n" + "\n".join(f"- {action}" for action in actions)


def _customer_action_suffix(
    intent_target: str,
    table: str,
    query: str,
    product_name: str = "",
    product_id: str = "",
    product_category: str = "",
    component_category: str = "",
    trusted_refund_entry: str = "",
    recommended_products: list[dict[str, Any]] | None = None,
    *,
    intent_domain: str = "",
    intent_operation: str = "",
) -> str:
    """为客户的下一步操作附加确定性站内链接，而不是让模型临时编造 URL。"""
    del query, intent_operation
    if intent_target == "ticket":
        return "\n\n[查看售后进度](?page=tickets)"
    # 退款入口只能由 SupportWorkflow 在资格核验后通过可信后端生成。不能再根据
    # 原始 query 中的“退款”一词拼通用链接，否则 STATEMENT/状态查询也会被误导。
    if trusted_refund_entry.startswith("?page=orders&refund_order=SO"):
        return f"\n\n[前往我的订单申请退款]({trusted_refund_entry})"
    # A trusted catalog observation may come from the Customer Operator rather
    # than the legacy RAG route, so ``table`` is not always populated here.
    # Once the server has projected a canonical product id/category, link to
    # the detail page directly; never fall back to the default laptop search.
    if product_id and product_category in {"laptops", "phones", "components"}:
        return f"\n\n[查看该商品](?page=product&category={quote(product_category)}&product={quote(product_id)})"
    recommended_suffix = _recommended_product_action_suffix(recommended_products or [])
    if recommended_suffix:
        return recommended_suffix
    if intent_domain in {"order", "delivery", "payment", "refund"}:
        return "\n\n[查看我的订单](?page=orders)"
    if table in {"laptop_products", "phone_products", "component_products"} or intent_domain == "product":
        category = product_category if product_category in {"laptops", "phones", "components"} else ""
        if product_id and category:
            return f"\n\n[查看该商品](?page=product&category={quote(category)}&product={quote(product_id)})"
        search = product_name.strip()
        if search:
            category_suffix = f"&category={quote(category)}" if category else ""
            component_suffix = (
                f"&component_category={quote(component_category.strip())}"
                if category == "components" and component_category.strip()
                else ""
            )
            return f"\n\n[去商品目录查看](?page=catalog{category_suffix}{component_suffix}&q={quote(search)})"
        return "\n\n[去商品目录查看](?page=catalog)"
    return ""


def _append_customer_action_suffix(
    answer: str,
    intent_target: str,
    table: str,
    query: str,
    product_name: str = "",
    product_id: str = "",
    product_category: str = "",
    component_category: str = "",
    recommended_products: list[dict[str, Any]] | None = None,
    *,
    intent_domain: str = "",
    intent_operation: str = "",
) -> str:
    """追加稳定的站内链接，但不重复模型已经生成的同一链接。"""
    if (product_id and product_category in {"laptops", "phones", "components"}) or recommended_products:
        # A model may still emit the legacy generic catalog link even after a
        # server-owned product was selected.  Keeping both links makes the
        # stale/default laptop route look equally authoritative.  The direct
        # detail link below is the only safe action once canonical identity is
        # available; remove only that exact relative fallback link.
        answer = re.sub(
            r"\s*\[去商品目录查看\]\(\?page=catalog(?:[^)]*)\)",
            "",
            answer,
        ).rstrip()
    suffix = _customer_action_suffix(
        intent_target,
        table,
        query,
        product_name,
        product_id,
        product_category,
        component_category,
        recommended_products=recommended_products,
        intent_domain=intent_domain,
        intent_operation=intent_operation,
    )
    if suffix and suffix.strip() not in answer:
        return answer + suffix
    return answer


def _entities_from_catalog_docs(docs: list[dict], answer: str = "") -> dict[str, Any]:
    """Project only server-owned candidates from catalog observations.

    ``answer`` is intentionally ignored: natural-language output is not a
    business protocol and cannot promote a canonical product.
    """
    del answer
    unique = dedupe_product_candidates([doc for doc in docs if isinstance(doc, dict)])
    return {"product_candidates": unique[:12]} if unique else {}


def _entities_from_retrieval(table: str, docs: list[dict], answer: str = "") -> dict[str, Any]:
    """Compatibility wrapper for product-table retrieval projections."""
    if table not in _CATALOG_TABLES:
        return {}
    return _entities_from_catalog_docs(docs, answer)


def _merge_last_entities(target: dict[str, Any], incoming: object) -> None:
    """Merge only the small server-owned entity projection into session state."""
    if not isinstance(incoming, dict):
        return
    product_keys = {"product", "product_id", "product_name", "product_category", "component_category"}
    selected = canonical_product_identity(incoming)
    candidates = incoming.get("product_candidates")
    if isinstance(candidates, list):
        normalized_candidates = dedupe_product_candidates([item for item in candidates if isinstance(item, dict)])
        for key in product_keys:
            target.pop(key, None)
        target["product_candidates"] = normalized_candidates[:12]
    if selected is not None:
        # A server-validated candidate may become the current canonical product
        # while the same trusted candidate set remains useful for later
        # preference refinement.  A subsequent fresh candidate observation
        # still retires the selected product in the branch above.
        for key in product_keys:
            target.pop(key, None)
        target.update({key: value for key, value in incoming.items() if key in product_keys})
    for key, value in incoming.items():
        if not isinstance(key, str):
            continue
        if key in product_keys or key == "product_candidates":
            continue
        if key == "product_context" and isinstance(value, dict):
            target[key] = dict(value)
            continue
        if isinstance(value, str) and value.strip():
            target[key] = value.strip()


def _product_entity_for_turn(query: str, entities: dict[str, Any]) -> dict[str, str] | None:
    """Perform identity-only matching against prior server-owned candidates."""
    candidates = entities.get("product_candidates")
    if isinstance(candidates, list):
        return match_product_candidate(query, [item for item in candidates if isinstance(item, dict)])
    return None


def _should_rerank(query: str, table: str) -> bool:
    """仅在精排确实能改善答案时承担额外 CPU 延迟。

    商品单品咨询、参数查询和预算推荐首先要求快速出首字；向量检索与 BM25 融合
    已足够作为候选。明确比较多个商品时，才为笔记本/手机启用交叉编码精排。
    政策和组件类问题则保留精排，以降低把不相关依据带入回答的概率。
    """
    if table in {"laptop_products", "phone_products"}:
        comparison_markers = ("对比", "区别", "哪个好", "哪款", " versus ", " vs ", "和")
        normalized_query = f" {query.lower()} "
        return any(re.search(re.escape(marker), normalized_query) for marker in comparison_markers)
    return True


async def _selected_product_context(chat_req: ChatRequest) -> tuple[str, str, str]:
    """读取详情页明确选中的商品，避免模型只凭长标题猜参数。

    返回的内容只来自用户刚刚浏览的公开详情；不会把库存、仓库或任意客户端字段
    放进模型上下文。退款等明确售后意图仍由正常路由处理，不会被商品介绍覆盖。
    """
    if (chat_req.product_category is None) != (chat_req.product_id is None):
        raise HTTPException(status_code=400, detail="商品咨询上下文不完整")
    if not chat_req.product_category or not chat_req.product_id:
        return "", "", ""
    product = await get_product_detail(chat_req.product_category, chat_req.product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="商品不可用或无法核验")
    table = {
        "laptops": "laptop_products",
        "phones": "phone_products",
        "components": "component_products",
    }[chat_req.product_category]
    return build_public_product_context(product), table, str(product.get("product_name") or "")


@chat_router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
async def chat(chat_req: ChatRequest, request: Request):
    try:
        agent = request.app.state.agent
        session = request.app.state.session
        intent_router = request.app.state.intent_router
        user_id = request.state.user["id"]
        role = request.state.user["role"]
        tool_context = ToolContext(
            user_id=user_id,
            role=role,
            blocked_tools=frozenset({"create_ticket"}) if role == "customer" else frozenset(),
            allowed_tools=_CUSTOMER_CHAT_READ_TOOLS if role == "customer" else None,
        )
        if role == "customer" and chat_req.replace_from_sequence is not None:
            raise HTTPException(status_code=400, detail="客户消息发送后不可编辑，请重新发送更正内容")
        selected_product_context, selected_product_table, selected_product_name = await _selected_product_context(
            chat_req
        )

        if chat_req.replace_from_sequence is not None:
            if chat_req.session_id is None or not await session.truncate_from(
                chat_req.session_id,
                user_id,
                chat_req.replace_from_sequence,
            ):
                raise HTTPException(status_code=404, detail="会话消息不可用")

        # 获取历史会话或创建新会话
        ctx = await session.get_or_create(chat_req.session_id, user_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        explicit_product_entity: dict[str, str] | None = None
        if selected_product_name and chat_req.product_id and chat_req.product_category:
            explicit_product_entity = {
                "product": selected_product_name,
                "product_id": chat_req.product_id,
                "product_category": chat_req.product_category,
            }
        turn_product_entity: dict[str, str] | None = explicit_product_entity

        # 判断指代词对应的实体。结构化订单选择已经由服务端验证并写入 Case，
        # 不能再交给 Router 根据一段自然语言重新猜测。
        active_support_case = await _get_active_support_case(
            request,
            session_id=ctx.session_id,
            customer_user_id=user_id,
        )
        recent_support_case = await _get_latest_support_case(
            request,
            session_id=ctx.session_id,
            customer_user_id=user_id,
        )
        session_facts = _session_decision_facts(ctx.messages)
        session_contexts = _session_decision_contexts(ctx.messages)
        confirmed_human_handoff = False
        resuming_support_case = False
        if chat_req.interaction is not None:
            active_support_case = await _apply_subject_choice_interaction(
                request,
                case=active_support_case,
                interaction=chat_req.interaction,
                tool_context=tool_context,
            )
            recent_support_case = active_support_case
            resolved_query = chat_req.history_content
            intent = _intent_for_case_reply(
                active_support_case,
                Intent(
                    target="agent",
                    query=resolved_query,
                    confidence=1.0,
                    route_source="structured_interaction",
                    case_update="continue",
                ),
            )
            confirmed_human_handoff = False
            resuming_support_case = True
        else:
            resolved_query = await session.resolve(chat_req.query, ctx.session_id, user_id)
            resolved_query = resolve_stock_follow_up(
                resolved_query,
                ctx.last_entities,
                ctx.history,
            )
            # Phase A authority cutover: natural-language order corrections are
            # resolved by OrderSubjectResolver inside SupportWorkflow.  Keep the
            # legacy correction helpers for rollback/Phase C deletion, but do
            # not let them pre-empt Router + Resolver on the customer main path.
            correction_kind = "none"
            if correction_kind == "none":
                active_case_context = (
                    SupportCaseService.to_prompt_context(active_support_case)
                    if active_support_case is not None
                    else _recent_subject_router_context(recent_support_case)
                )
                active_case_context = _router_case_context(active_case_context, ctx.messages)
                # Pre-RAG 只给 Router 解释项目术语；业务事实和最终回答证据仍由后续层获取。
                semantic_hints = _semantic_hint_payload(
                    resolved_query=resolved_query,
                    entities={**ctx.last_entities, "_raw_query": chat_req.query},
                    explicit_product=explicit_product_entity,
                )
                pre_knowledge_context = await _pre_route_knowledge_context(resolved_query)
                if active_case_context:
                    intent = await _route_intent_with_hints(
                        intent_router,
                        chat_req.query,
                        history=ctx.history,
                        case_context=active_case_context,
                        knowledge_context=pre_knowledge_context,
                        semantic_hints=semantic_hints,
                    )
                else:
                    intent = await _route_intent_with_hints(
                        intent_router,
                        chat_req.query,
                        history=ctx.history,
                        knowledge_context=pre_knowledge_context,
                        semantic_hints=semantic_hints,
                    )
                # A non-description turn has now been adjudicated by the
                # normal Router.  Retire the correction-description frame
                # before any workflow/request-stack resume can occur.  This is
                # intentionally independent of case_update: a misclassified
                # ``continue`` must not revive the stale correction either.
                if (
                    active_support_case is not None
                    and active_support_case.pending.get("kind") == "subject_correction_description"
                    and not looks_like_bare_subject_description(chat_req.query)
                ):
                    active_support_case = await _supersede_subject_correction_pending(
                        request,
                        case=active_support_case,
                    )
                    recent_support_case = active_support_case
                confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
                resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
                if resuming_support_case and active_support_case is not None:
                    intent = _intent_for_case_reply(active_support_case, intent)
                    active_support_case = await _resume_pending_case(
                        request,
                        case=active_support_case,
                        raw_query=chat_req.query,
                        tool_context=tool_context,
                    )
        retrieval_query = getattr(intent, "retrieval_query", "") or getattr(intent, "query", "") or resolved_query
        semantic_hints = _semantic_hint_payload(
            resolved_query=resolved_query,
            entities={**ctx.last_entities, "_raw_query": chat_req.query},
            explicit_product=explicit_product_entity,
        )
        _finalize_intent_channels(
            intent,
            raw_query=chat_req.query,
            retrieval_query=retrieval_query,
            semantic_hints=semantic_hints,
        )
        evidence_plan = resolve_evidence(
            domain=intent.domain,
            operation=intent.operation,
            target=intent.target,
            table=intent.table,
        )
        resolver_llm = getattr(intent_router, "llm", None) or getattr(agent, "llm", None)
        product_resolver = ProductResolver(resolver_llm) if resolver_llm is not None else None
        (
            catalog_docs,
            catalog_acquired,
            product_context,
            product_resolution,
            turn_product_entity,
            product_entity_projection,
        ) = await _prepare_ecommerce_role(
            intent=intent,
            raw_query=chat_req.query,
            retrieval_query=retrieval_query,
            entities=ctx.last_entities,
            history=ctx.history,
            explicit_product=explicit_product_entity,
            catalog_required=evidence_plan.catalog,
            resolver=product_resolver,
        )
        product_candidates_for_turn = dedupe_product_candidates(
            [item for item in product_entity_projection.get("product_candidates", []) if isinstance(item, dict)]
        )
        operator_tool_context = _ecommerce_operator_tool_context(
            tool_context,
            intent,
            canonical_product=turn_product_entity is not None,
            catalog_bound=catalog_acquired,
            candidate_refs=_visible_product_refs(product_candidates_for_turn, product_resolution),
        )
        recommended_products_for_turn: list[dict[str, Any]] = []
        catalog_context = (
            _product_procedure_context(
                product_context=product_context,
                candidates=product_candidates_for_turn,
                resolution=product_resolution,
            )
            if (
                intent.domain == "product"
                or intent.table in _CATALOG_TABLES
                or intent.product_category
                or product_resolution.status != "unknown"
            )
            else ""
        )
        # `rag/knowledge_chunks` 兼容路径会在下方复用原有检索；其他路径可同时携带
        # 受控知识和实时 Workflow/Tool 事实，而不再二选一。
        knowledge_context = await _deep_knowledge_context(
            retrieval_query,
            required=evidence_plan.needs_deep_knowledge
            and not (intent.target == "rag" and intent.table == "knowledge_chunks"),
        )
        sentiment = detect_sentiment(chat_req.query, history=ctx.history)
        sentiment_ctx = build_escalation_prompt(sentiment)
        route_ctx = build_route_instruction(intent)
        capability_ctx = _customer_chat_capability_context(
            operator_tool_context,
            canonical_product=turn_product_entity is not None,
            recommended_product_actions=bool(product_candidates_for_turn and intent.domain == "product"),
        )
        selected_product_ctx = _selected_product_operator_context(
            turn_product_entity,
            {**ctx.last_entities, **product_entity_projection},
        )
        agent_prompt_extra = _compose_prompt_extras(
            sentiment_ctx,
            route_ctx,
            capability_ctx,
            selected_product_ctx,
            _previous_outcome_operator_context(intent, ctx.messages),
        )

        support_decision = decide_customer_support_action(
            intent_target=intent.target,
            role=tool_context.role,
            # 建单许可只能依据客户本轮原话和服务端历史，不能依据模型改写后的
            # effective_query，避免模型补全/幻觉被当成可执行业务事实。
            query=chat_req.query,
            history=ctx.messages,
        )
        _chat_logger.info(
            "customer support decision action=%s reason=%s proposed_target=%s",
            support_decision.action,
            support_decision.reason,
            intent.target,
        )
        if confirmed_human_handoff:
            ticket_id, answer = await _create_customer_ticket(
                request,
                issue=chat_req.query,
                tool_context=tool_context,
                history=ctx.messages,
            )
            await _mark_support_case_awaiting_staff(
                request,
                case=active_support_case,
                ticket_id=ticket_id,
            )
            answer += "\n\n[查看售后进度](?page=tickets)"
            handoff_result = LoopResult(
                answer=answer,
                response_control={"mode": "STAFF_HANDOFF", "subject_id": None},
            )
            presentation = _customer_presentation(
                handoff_result,
                intent,
                case=active_support_case,
                ticket_id=ticket_id,
            )
            await session.add_turn_simple(
                ctx.session_id,
                user_id,
                chat_req.history_content,
                answer,
                presentation=presentation,
            )
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=1,
                total_tokens=0,
                presentation=presentation,
            )
        if support_decision.action in {
            CustomerSupportAction.SHOW_REFUND_PROGRESS,
            CustomerSupportAction.OFFER_REFUND_SELF_SERVICE,
            CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING,
        }:
            answer = support_decision.answer
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.history_content, answer)
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=0,
                total_tokens=0,
            )

        if support_decision.action == CustomerSupportAction.ASK_FOR_CLARIFICATION:
            agent_prompt_extra = _compose_prompt_extras(
                agent_prompt_extra,
                "当前需要先做意图确认。请先复述你对用户诉求的理解，列出缺失或冲突的信息，"
                "然后只提出一个最关键的问题；不要创建工单，也不要执行任何写操作。",
            )

        if support_decision.action == CustomerSupportAction.CREATE_TICKET:
            ticket_id, answer = await _create_customer_ticket(
                request,
                issue=chat_req.query,
                tool_context=tool_context,
                history=ctx.messages,
            )
            answer = _append_customer_action_suffix(
                answer,
                intent.target,
                intent.table,
                chat_req.query,
                intent_domain=intent.domain,
                intent_operation=intent.operation,
            )
            handoff_result = LoopResult(
                answer=answer,
                response_control={"mode": "STAFF_HANDOFF", "subject_id": None},
            )
            presentation = _customer_presentation(handoff_result, intent, ticket_id=ticket_id)
            await session.add_turn_simple(
                ctx.session_id,
                user_id,
                chat_req.history_content,
                answer,
                presentation=presentation,
            )
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=1,
                total_tokens=0,
                presentation=presentation,
            )

        response_case = recent_support_case
        if intent.target == "plan_execute":
            plan_agent = request.app.state.plan_execute_agent
            plan_state = await plan_agent.run(
                chat_req.query,
                history=ctx.history,
                scenario=intent.scenario,
                tool_context=operator_tool_context,
            )
            # plan_execute 不走 AgentLoop，手动记录到 session
            await session.add_turn_simple(
                ctx.session_id,
                user_id,
                chat_req.history_content,
                plan_state.get("answer", ""),
            )
            return ChatResponse(
                answer=plan_state.get("answer", ""),
                session_id=ctx.session_id,
                total_steps=len(plan_state.get("plan", [])),
                total_tokens=plan_state.get("total_tokens", 0),
            )
        elif intent.use_workflow:
            support_workflow = request.app.state.support_workflow_agent
            support_case = (
                active_support_case
                if resuming_support_case
                else await _open_support_case(
                    request,
                    intent=intent,
                    session_id=ctx.session_id,
                    customer_user_id=user_id,
                    recent_case=recent_support_case,
                    raw_query=chat_req.query,
                    tool_context=tool_context,
                )
            )
            support_case = await _merge_new_support_request(request, case=support_case, intent=intent)
            if support_case is None:
                support_case = await _open_support_case(
                    request,
                    intent=intent,
                    session_id=ctx.session_id,
                    customer_user_id=user_id,
                    recent_case=None,
                    raw_query=chat_req.query,
                    tool_context=tool_context,
                )
            case_context = SupportCaseService.to_prompt_context(support_case) if support_case is not None else ""
            workflow_kwargs = await _workflow_subject_context_kwargs(
                request,
                intent=intent,
                support_case=support_case,
                recent_case=recent_support_case,
                tool_context=tool_context,
                structured_interaction=chat_req.interaction is not None,
            )
            # Compatibility/trace field only; OrderSubjectResolver does not
            # consume Router same/changed semantics.
            workflow_kwargs["subject_relation"] = getattr(intent, "subject_relation", "unknown")
            if getattr(intent, "fact_scope", "current") == "explain_previous":
                allow_historical_explanation = await _allow_historical_subject_explanation(
                    request,
                    intent=intent,
                    support_case=support_case,
                    raw_query=chat_req.query,
                    tool_context=tool_context,
                )
                if allow_historical_explanation:
                    workflow_kwargs["historical_contexts"] = session_contexts
                    workflow_kwargs["allow_historical_explanation"] = True
            try:
                loop_result = await support_workflow.run(
                    chat_req.query,
                    context=_merge_evidence_context(selected_product_context, catalog_context, knowledge_context),
                    history=ctx.history,
                    system_prompt_extra=agent_prompt_extra,
                    case_context=case_context,
                    support_requests=_support_case_payloads(intent),
                    tool_context=operator_tool_context,
                    **workflow_kwargs,
                )
            except Exception as exc:
                await _fail_support_case(request, case=support_case, error=exc)
                raise
            response_case = await _persist_support_case_progress(
                request,
                case=support_case,
                intent=intent,
                loop_result=loop_result,
            )
        elif intent.target == "rag":
            if catalog_acquired and (intent.domain == "product" or intent.product_category):
                docs = catalog_docs
            else:
                docs = await hybrid_search(
                    retrieval_query,
                    table=intent.table,
                    use_rerank=_should_rerank(retrieval_query, intent.table),
                )
            context = _merge_evidence_context(
                selected_product_context,
                catalog_context
                if (intent.domain == "product" or intent.product_category)
                else _build_context(docs, customer_view=tool_context.role == "customer"),
                knowledge_context,
            )
            loop_result = await agent.run(
                chat_req.query,
                context=context,
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                tool_context=operator_tool_context,
            )
            retrieved_entities = _entities_from_retrieval(intent.table, docs, loop_result.answer)
            if selected_product_name:
                retrieved_entities["product"] = selected_product_name
                if chat_req.product_id and chat_req.product_category:
                    retrieved_entities["product_id"] = chat_req.product_id
                    retrieved_entities["product_category"] = chat_req.product_category
            loop_result.last_entities = {**retrieved_entities, **loop_result.last_entities}
        else:
            loop_result = await agent.run(
                chat_req.query,
                context=_merge_evidence_context(selected_product_context, catalog_context, knowledge_context),
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                tool_context=operator_tool_context,
            )
            if catalog_docs:
                catalog_entities = _entities_from_catalog_docs(catalog_docs, loop_result.answer)
                loop_result.last_entities = {**catalog_entities, **loop_result.last_entities}

        presentation_mode, presented_refs = _presented_product_refs(
            loop_result.verified_facts,
            product_candidates_for_turn,
            allowed_refs=_visible_product_refs(product_candidates_for_turn, product_resolution),
        )
        if presented_refs:
            product_context = set_choice_refs(product_context, presented_refs)
            product_entity_projection.update(product_context_entity(product_context))
            if presentation_mode == "recommend":
                recommended_products_for_turn = _recommended_products_from_refs(
                    presented_refs, product_candidates_for_turn
                )

        if product_entity_projection:
            _merge_last_entities(loop_result.last_entities, product_entity_projection)

        if catalog_acquired:
            loop_result.answer_trace.setdefault("catalog_acquired", True)

        if tool_context.role == "customer":
            _apply_customer_refund_fact_boundary(
                intent,
                loop_result,
                query=chat_req.query,
                recent_case=response_case,
                session_facts=session_facts,
                session_contexts=session_contexts,
                session_messages=ctx.messages,
            )

        if (
            tool_context.role == "customer"
            and loop_result.workflow_progress.get("resolution_type")
            not in {
                "SELF_SERVICE_HANDOFF",
                "SELF_SERVICE_ORDER_CANCEL",
            }
            # A server-owned product selected in an earlier turn remains a
            # safe navigation target even when the operator classified the
            # current read-only answer as FACT/EXPLANATION rather than the
            # generic mode.  Controlled/error responses are still excluded
            # above and by the generic guard when no canonical identity is
            # available.
            and (
                _can_append_generic_customer_action(
                    loop_result,
                    trusted_navigation=_trusted_navigation_action(
                        intent,
                        canonical_product=canonical_product_identity(loop_result.last_entities) is not None,
                        recommended_product_actions=bool(recommended_products_for_turn),
                    ),
                )
            )
        ):
            loop_result.answer = _append_customer_action_suffix(
                loop_result.answer,
                intent.target,
                intent.table,
                chat_req.query,
                loop_result.last_entities.get("product", ""),
                loop_result.last_entities.get("product_id", ""),
                loop_result.last_entities.get("product_category", ""),
                loop_result.last_entities.get("component_category", ""),
                recommended_products=recommended_products_for_turn,
                intent_domain=intent.domain,
                intent_operation=intent.operation,
            )

        presentation = _customer_presentation(loop_result, intent, case=response_case)
        _attach_answer_trace(
            loop_result,
            intent,
            raw_query=chat_req.query,
            retrieval_query=retrieval_query,
            tool_context=operator_tool_context,
        )
        # 当前对话放入上下文ctx
        await session.add_turn(ctx.session_id, user_id, chat_req.history_content, loop_result)
        return ChatResponse(
            answer=loop_result.answer,
            session_id=ctx.session_id,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
            presentation=presentation,
        )
    except DependencyUnavailableError:
        raise
    except LLMError as e:
        _chat_logger.error(
            "LLM 调用失败: retry=%d status=%s reason=%s",
            e.retry_count,
            e.status_code,
            e.last_response,
        )
        raise DependencyUnavailableError("智能服务暂时不可用") from e
    except Exception:
        _chat_logger.error("chat 端点异常")
        raise


@chat_router.post("/chat/stream")
async def chat_stream(chat_req: ChatRequest, request: Request):
    last_entities: dict[str, Any] = {}
    try:
        agent = request.app.state.agent
        session = request.app.state.session
        intent_router = request.app.state.intent_router
        user_id = request.state.user["id"]
        role = request.state.user["role"]
        tool_context = ToolContext(
            user_id=user_id,
            role=role,
            blocked_tools=frozenset({"create_ticket"}) if role == "customer" else frozenset(),
            allowed_tools=_CUSTOMER_CHAT_READ_TOOLS if role == "customer" else None,
        )
        if role == "customer" and chat_req.replace_from_sequence is not None:
            raise HTTPException(status_code=400, detail="客户消息发送后不可编辑，请重新发送更正内容")
        selected_product_context, selected_product_table, selected_product_name = await _selected_product_context(
            chat_req
        )

        # 这些步骤发生在 StreamingResponse 创建前，失败时可以正常返回 HTTP 503。
        if chat_req.replace_from_sequence is not None:
            if chat_req.session_id is None or not await session.truncate_from(
                chat_req.session_id,
                user_id,
                chat_req.replace_from_sequence,
            ):
                raise HTTPException(status_code=404, detail="会话消息不可用")

        session_ctx = await session.get_or_create(chat_req.session_id, user_id)
        if session_ctx is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        explicit_product_entity: dict[str, str] | None = None
        if selected_product_name and chat_req.product_id and chat_req.product_category:
            explicit_product_entity = {
                "product": selected_product_name,
                "product_id": chat_req.product_id,
                "product_category": chat_req.product_category,
            }
        turn_product_entity: dict[str, str] | None = explicit_product_entity

        history = session_ctx.history
        session_id = session_ctx.session_id
        chat_run_id = await _claim_chat_run(session_id)
        active_support_case = await _get_active_support_case(
            request,
            session_id=session_id,
            customer_user_id=user_id,
        )
        recent_support_case = await _get_latest_support_case(
            request,
            session_id=session_id,
            customer_user_id=user_id,
        )
        session_facts = _session_decision_facts(session_ctx.messages)
        session_contexts = _session_decision_contexts(session_ctx.messages)
        # Subject-correction paths can rejoin the common stream control flow
        # without resuming a Case.  These flags must therefore exist before
        # entering either interaction or correction handling.
        confirmed_human_handoff = False
        resuming_support_case = False
        subject_correction_short_circuit: tuple[LoopResult, dict | None] | None = None
        if chat_req.interaction is not None:
            active_support_case = await _apply_subject_choice_interaction(
                request,
                case=active_support_case,
                interaction=chat_req.interaction,
                tool_context=tool_context,
            )
            recent_support_case = active_support_case
            resolve_query = chat_req.history_content
            intent = _intent_for_case_reply(
                active_support_case,
                Intent(
                    target="agent",
                    query=resolve_query,
                    confidence=1.0,
                    route_source="structured_interaction",
                    case_update="continue",
                ),
            )
            confirmed_human_handoff = False
            resuming_support_case = True
        else:
            resolve_query = await session.resolve(chat_req.query, session_id, user_id)
            resolve_query = resolve_stock_follow_up(
                resolve_query,
                session_ctx.last_entities,
                history,
            )
            # Same authority cutover as /chat: legacy subject-correction code is
            # intentionally non-authoritative on the streaming main path.
            correction_kind = "none"
            if correction_kind == "none":
                active_case_context = (
                    SupportCaseService.to_prompt_context(active_support_case)
                    if active_support_case is not None
                    else _recent_subject_router_context(recent_support_case)
                )
                active_case_context = _router_case_context(active_case_context, session_ctx.messages)
                semantic_hints = _semantic_hint_payload(
                    resolved_query=resolve_query,
                    entities={**session_ctx.last_entities, "_raw_query": chat_req.query},
                    explicit_product=explicit_product_entity,
                )
                pre_knowledge_context = await _pre_route_knowledge_context(resolve_query)
                if active_case_context:
                    intent = await _route_intent_with_hints(
                        intent_router,
                        chat_req.query,
                        history=history,
                        case_context=active_case_context,
                        knowledge_context=pre_knowledge_context,
                        semantic_hints=semantic_hints,
                    )
                else:
                    intent = await _route_intent_with_hints(
                        intent_router,
                        chat_req.query,
                        history=history,
                        knowledge_context=pre_knowledge_context,
                        semantic_hints=semantic_hints,
                    )
                if (
                    active_support_case is not None
                    and active_support_case.pending.get("kind") == "subject_correction_description"
                    and not looks_like_bare_subject_description(chat_req.query)
                ):
                    active_support_case = await _supersede_subject_correction_pending(
                        request,
                        case=active_support_case,
                    )
                    recent_support_case = active_support_case
                confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
                resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
                if resuming_support_case and active_support_case is not None:
                    intent = _intent_for_case_reply(active_support_case, intent)
                    active_support_case = await _resume_pending_case(
                        request,
                        case=active_support_case,
                        raw_query=chat_req.query,
                        tool_context=tool_context,
                    )
        retrieval_query = getattr(intent, "retrieval_query", "") or getattr(intent, "query", "") or resolve_query
        semantic_hints = _semantic_hint_payload(
            resolved_query=resolve_query,
            entities={**session_ctx.last_entities, "_raw_query": chat_req.query},
            explicit_product=explicit_product_entity,
        )
        _finalize_intent_channels(
            intent,
            raw_query=chat_req.query,
            retrieval_query=retrieval_query,
            semantic_hints=semantic_hints,
        )
        evidence_plan = resolve_evidence(
            domain=intent.domain,
            operation=intent.operation,
            target=intent.target,
            table=intent.table,
        )
        resolver_llm = getattr(intent_router, "llm", None) or getattr(agent, "llm", None)
        product_resolver = ProductResolver(resolver_llm) if resolver_llm is not None else None
        (
            catalog_docs,
            catalog_acquired,
            product_context,
            product_resolution,
            turn_product_entity,
            product_entity_projection,
        ) = await _prepare_ecommerce_role(
            intent=intent,
            raw_query=chat_req.query,
            retrieval_query=retrieval_query,
            entities=session_ctx.last_entities,
            history=history,
            explicit_product=explicit_product_entity,
            catalog_required=evidence_plan.catalog,
            resolver=product_resolver,
        )
        product_candidates_for_turn = dedupe_product_candidates(
            [item for item in product_entity_projection.get("product_candidates", []) if isinstance(item, dict)]
        )
        operator_tool_context = _ecommerce_operator_tool_context(
            tool_context,
            intent,
            canonical_product=turn_product_entity is not None,
            catalog_bound=catalog_acquired,
            candidate_refs=_visible_product_refs(product_candidates_for_turn, product_resolution),
        )
        recommended_products_for_turn: list[dict[str, Any]] = []
        catalog_context = (
            _product_procedure_context(
                product_context=product_context,
                candidates=product_candidates_for_turn,
                resolution=product_resolution,
            )
            if (
                intent.domain == "product"
                or intent.table in _CATALOG_TABLES
                or intent.product_category
                or product_resolution.status != "unknown"
            )
            else ""
        )
        knowledge_context = await _deep_knowledge_context(
            retrieval_query,
            required=evidence_plan.needs_deep_knowledge
            and not (intent.target == "rag" and intent.table == "knowledge_chunks"),
        )
        sentiment = detect_sentiment(chat_req.query, history=history)
        extra_prompt = _compose_prompt_extras(
            build_escalation_prompt(sentiment),
            build_route_instruction(intent),
            _customer_chat_capability_context(
                operator_tool_context,
                canonical_product=turn_product_entity is not None,
                recommended_product_actions=bool(product_candidates_for_turn and intent.domain == "product"),
            ),
            _selected_product_operator_context(
                turn_product_entity,
                {**session_ctx.last_entities, **product_entity_projection},
            ),
            _previous_outcome_operator_context(intent, session_ctx.messages),
        )
        context = _merge_evidence_context(selected_product_context, catalog_context, knowledge_context)
        if intent.target == "rag":
            if catalog_acquired and (intent.domain == "product" or intent.product_category):
                docs = catalog_docs
            else:
                docs = await hybrid_search(
                    retrieval_query,
                    table=intent.table,
                    use_rerank=_should_rerank(retrieval_query, intent.table),
                )
            context = _merge_evidence_context(
                selected_product_context,
                catalog_context
                if (intent.domain == "product" or intent.product_category)
                else _build_context(docs, customer_view=tool_context.role == "customer"),
                knowledge_context,
            )
            # 检索排序本身不是商品选择；在 done 时结合 Operator 的最终回答再投影实体。
            last_entities = {}
            if selected_product_name:
                last_entities["product"] = selected_product_name
                if chat_req.product_id and chat_req.product_category:
                    last_entities["product_id"] = chat_req.product_id
                    last_entities["product_category"] = chat_req.product_category
        if product_entity_projection:
            _merge_last_entities(last_entities, product_entity_projection)
    except DependencyUnavailableError:
        raise
    except LLMError as exc:
        raise DependencyUnavailableError("智能服务暂时不可用") from exc

    stream_res = {
        "answer": "",
        "total_steps": 0,
        "total_tokens": 0,
        "decision_facts": {},
        "decision_contexts": [],
        "presentation": None,
    }
    start_t = time.perf_counter()
    support_decision = decide_customer_support_action(
        intent_target=intent.target,
        role=tool_context.role,
        query=chat_req.history_content,
        history=session_ctx.messages,
    )
    _chat_logger.info(
        "customer support decision action=%s reason=%s proposed_target=%s",
        support_decision.action,
        support_decision.reason,
        intent.target,
    )
    create_customer_ticket = support_decision.action == CustomerSupportAction.CREATE_TICKET or confirmed_human_handoff
    if support_decision.action == CustomerSupportAction.ASK_FOR_CLARIFICATION:
        extra_prompt = _compose_prompt_extras(
            extra_prompt,
            "当前需要先做意图确认。请先复述你对用户诉求的理解，列出缺失或冲突的信息，"
            "然后只提出一个最关键的问题；不要创建工单，也不要执行任何写操作。",
        )

    async def generate():
        stream_completed = False
        phase = "start"
        pending_done_event: dict[str, object] | None = None
        buffered_response_tokens: list[str] = []
        response_fact_sensitive = tool_context.role == "customer" and _refund_response_context(
            intent,
            chat_req.query,
            active_case=active_support_case,
            recent_case=recent_support_case,
            session_facts=session_facts,
            session_contexts=session_contexts,
        )
        nonlocal last_entities
        try:
            # 先推一个 start 事件给前端，带 session_id
            yield f"data: {json.dumps({'event': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            if subject_correction_short_circuit is not None:
                correction_result, correction_presentation = subject_correction_short_circuit
                answer = correction_result.answer
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                phase = "persist"
                await session.add_turn_simple(
                    session_id,
                    user_id,
                    chat_req.history_content,
                    answer,
                    presentation=correction_presentation,
                )
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "session_id": session_id,
                    "total_steps": 0,
                    "total_tokens": 0,
                    "presentation": correction_presentation,
                }
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                return

            if support_decision.action in {
                CustomerSupportAction.SHOW_REFUND_PROGRESS,
                CustomerSupportAction.OFFER_REFUND_SELF_SERVICE,
                CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING,
            }:
                answer = support_decision.answer
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                try:
                    await session.add_turn_simple(session_id, user_id, chat_req.history_content, answer)
                except Exception as exc:
                    _chat_logger.error(
                        "customer support guidance persistence failed action=%s error_type=%s",
                        support_decision.action,
                        type(exc).__name__,
                    )
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "session_id": session_id,
                    "total_steps": 0,
                    "total_tokens": 0,
                    "presentation": None,
                }
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                return

            if create_customer_ticket:
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                phase = "ticket_create"
                ticket_id, answer = await _create_customer_ticket(
                    request,
                    issue=chat_req.history_content,
                    tool_context=tool_context,
                    history=session_ctx.messages,
                )
                if confirmed_human_handoff:
                    await _mark_support_case_awaiting_staff(
                        request,
                        case=active_support_case,
                        ticket_id=ticket_id,
                    )
                phase = "ticket_link"
                if confirmed_human_handoff:
                    answer += "\n\n[查看售后进度](?page=tickets)"
                else:
                    answer = _append_customer_action_suffix(
                        answer,
                        intent.target,
                        intent.table,
                        chat_req.query,
                        intent_domain=intent.domain,
                        intent_operation=intent.operation,
                    )
                handoff_result = LoopResult(
                    answer=answer,
                    response_control={"mode": "STAFF_HANDOFF", "subject_id": None},
                )
                presentation = _customer_presentation(
                    handoff_result,
                    intent,
                    case=active_support_case,
                    ticket_id=ticket_id,
                )
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                phase = "ticket_events"
                yield f"data: {json.dumps({'event': 'tool_call', 'name': 'create_ticket'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "session_id": session_id,
                    "total_steps": 1,
                    "total_tokens": 0,
                    "ticket_id": ticket_id,
                    "presentation": presentation,
                }
                phase = "persist"
                try:
                    await session.add_turn_simple(
                        session_id,
                        user_id,
                        chat_req.history_content,
                        answer,
                        presentation=presentation,
                    )
                except Exception as exc:
                    # 工单已在独立事务中成功创建；会话存档失败不能伪装成工单失败。
                    _chat_logger.error(
                        "ticket created but chat history persistence failed error_type=%s",
                        type(exc).__name__,
                    )
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                return

            if intent.use_workflow:
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                phase = "support_workflow"
                support_workflow = request.app.state.support_workflow_agent
                support_case = (
                    active_support_case
                    if resuming_support_case
                    else await _open_support_case(
                        request,
                        intent=intent,
                        session_id=session_id,
                        customer_user_id=user_id,
                        recent_case=recent_support_case,
                        raw_query=chat_req.query,
                        tool_context=tool_context,
                    )
                )
                support_case = await _merge_new_support_request(request, case=support_case, intent=intent)
                if support_case is None:
                    support_case = await _open_support_case(
                        request,
                        intent=intent,
                        session_id=session_id,
                        customer_user_id=user_id,
                        recent_case=None,
                        raw_query=chat_req.query,
                        tool_context=tool_context,
                    )
                case_context = SupportCaseService.to_prompt_context(support_case) if support_case is not None else ""
                workflow_kwargs = await _workflow_subject_context_kwargs(
                    request,
                    intent=intent,
                    support_case=support_case,
                    recent_case=recent_support_case,
                    tool_context=tool_context,
                    structured_interaction=chat_req.interaction is not None,
                )
                # Compatibility/trace field only; OrderSubjectResolver does not
                # consume Router same/changed semantics.
                workflow_kwargs["subject_relation"] = getattr(intent, "subject_relation", "unknown")
                if getattr(intent, "fact_scope", "current") == "explain_previous":
                    allow_historical_explanation = await _allow_historical_subject_explanation(
                        request,
                        intent=intent,
                        support_case=support_case,
                        raw_query=chat_req.query,
                        tool_context=tool_context,
                    )
                    if allow_historical_explanation:
                        workflow_kwargs["historical_contexts"] = session_contexts
                        workflow_kwargs["allow_historical_explanation"] = True
                try:
                    workflow_result = await support_workflow.run(
                        chat_req.query,
                        context=context,
                        history=history,
                        system_prompt_extra=extra_prompt,
                        case_context=case_context,
                        support_requests=_support_case_payloads(intent),
                        tool_context=operator_tool_context,
                        **workflow_kwargs,
                    )
                except Exception as exc:
                    await _fail_support_case(request, case=support_case, error=exc)
                    raise
                # Persist the control outcome before composing the customer answer.  The
                # updated Case carries the exact pending choice frame (or staff handoff),
                # so the same shared composer sees the same state as the normal endpoint.
                phase = "persist"
                response_case = await _persist_support_case_progress(
                    request,
                    case=support_case,
                    intent=intent,
                    loop_result=workflow_result,
                )
                answer = workflow_result.answer
                if tool_context.role == "customer":
                    _apply_customer_refund_fact_boundary(
                        intent,
                        workflow_result,
                        query=chat_req.query,
                        recent_case=response_case or support_case,
                        session_facts=session_facts,
                        session_contexts=session_contexts,
                        session_messages=session_ctx.messages,
                    )
                    answer = workflow_result.answer
                presentation = _customer_presentation(
                    workflow_result,
                    intent,
                    case=response_case or support_case,
                )
                _attach_answer_trace(
                    workflow_result,
                    intent,
                    raw_query=chat_req.query,
                    retrieval_query=retrieval_query,
                    tool_context=operator_tool_context,
                )
                if (
                    tool_context.role == "customer"
                    and workflow_result.workflow_progress.get("resolution_type")
                    not in {
                        "SELF_SERVICE_HANDOFF",
                        "SELF_SERVICE_ORDER_CANCEL",
                    }
                    and "generate_refund_entry"
                    not in workflow_result.workflow_progress.get("unavailable_capabilities", [])
                    and (
                        _can_append_generic_customer_action(
                            workflow_result,
                            trusted_navigation=_trusted_navigation_action(
                                intent,
                                canonical_product=turn_product_entity is not None,
                            ),
                        )
                    )
                ):
                    answer = _append_customer_action_suffix(
                        answer,
                        intent.target,
                        intent.table,
                        chat_req.query,
                        workflow_result.last_entities.get("product", ""),
                        workflow_result.last_entities.get("product_id", ""),
                        workflow_result.last_entities.get("product_category", ""),
                        workflow_result.last_entities.get("component_category", ""),
                        intent_domain=intent.domain,
                        intent_operation=intent.operation,
                    )
                for step in workflow_result.steps:
                    for tool_call in step.tool_calls or []:
                        event = {"event": "tool_call", "name": tool_call.name}
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                stream_res["answer"] = answer
                stream_res["total_steps"] = workflow_result.total_steps
                stream_res["total_tokens"] = workflow_result.total_tokens
                stream_res["decision_facts"] = workflow_result.decision_facts
                stream_res["decision_contexts"] = workflow_result.decision_contexts
                await session.add_turn(
                    session_id,
                    user_id,
                    chat_req.history_content,
                    LoopResult(
                        answer=answer,
                        steps=workflow_result.steps,
                        total_steps=workflow_result.total_steps,
                        total_tokens=workflow_result.total_tokens,
                        total_latency_ms=(time.perf_counter() - start_t) * 1000,
                        last_entities=workflow_result.last_entities,
                        decision_facts=workflow_result.decision_facts,
                        decision_contexts=workflow_result.decision_contexts,
                        workflow_progress=workflow_result.workflow_progress,
                        response_control=workflow_result.response_control,
                        customer_presentation=presentation or {},
                        answer_source=workflow_result.answer_source,
                        answer_trace=workflow_result.answer_trace,
                    ),
                )
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "session_id": session_id,
                    "total_steps": workflow_result.total_steps,
                    "total_tokens": workflow_result.total_tokens,
                    "presentation": presentation,
                }
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                return

            if intent.target == "plan_execute":
                plan_agent = request.app.state.plan_execute_agent
                async for chunk in plan_agent.run_stream(
                    chat_req.query,
                    history=history,
                    scenario=intent.scenario,
                    tool_context=operator_tool_context,
                ):
                    if not await _is_current_chat_run(session_id, chat_run_id):
                        yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                        return
                    if chunk.get("event") == "error":
                        yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
                        return

                    if chunk.get("event") == "done":
                        data = chunk.get("data", {})
                        if not isinstance(data, dict):
                            data = {}
                        stream_res["answer"] = data.get("answer", "")
                        stream_res["total_steps"] = len(data.get("plan", []))
                        stream_res["total_tokens"] = data.get("total_tokens", 0)
                        stream_completed = True
                        # plan_execute 的内部 plan 也不是客户 Chat Contract 的一部分；
                        # 只发布与普通/Workflow 流一致的受控 done payload。
                        pending_done_event = {
                            "event": "done",
                            "answer": stream_res["answer"],
                            "session_id": session_id,
                            "total_steps": stream_res["total_steps"],
                            "total_tokens": stream_res["total_tokens"],
                            "presentation": None,
                        }
                    else:
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

                if stream_completed:
                    if not await _is_current_chat_run(session_id, chat_run_id):
                        yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                        return
                    phase = "persist"
                    await session.add_turn(
                        session_id,
                        user_id,
                        chat_req.history_content,
                        LoopResult(
                            answer=stream_res["answer"],
                            total_steps=stream_res["total_steps"],
                            total_latency_ms=(time.perf_counter() - start_t) * 1000,
                            last_entities=last_entities,
                        ),
                    )
                    if pending_done_event is not None:
                        yield f"data: {json.dumps(pending_done_event, ensure_ascii=False)}\n\n"
                return

            # 消费 agent 的消息流，逐个处理事件
            async for event in agent.run_stream(
                chat_req.query,
                context=context,
                history=history,
                system_prompt_extra=extra_prompt,
                tool_context=operator_tool_context,
            ):
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return
                if event.get("event") == "error":
                    yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
                    return

                if event.get("event") == "done":
                    answer = str(event.get("answer", ""))
                    event_entities = event.get("last_entities")
                    _merge_last_entities(last_entities, event_entities)
                    retrieved_entities = {}
                    if catalog_docs:
                        retrieved_entities = _entities_from_catalog_docs(catalog_docs, answer)
                    elif intent.target == "rag":
                        retrieved_entities = _entities_from_retrieval(intent.table, docs, answer)
                    if retrieved_entities:
                        _merge_last_entities(last_entities, retrieved_entities)

                    verified_facts = event.get("verified_facts", {})
                    presentation_mode, presented_refs = _presented_product_refs(
                        verified_facts if isinstance(verified_facts, Mapping) else {},
                        product_candidates_for_turn,
                        allowed_refs=_visible_product_refs(product_candidates_for_turn, product_resolution),
                    )
                    if presented_refs:
                        updated_product_context = set_choice_refs(product_context, presented_refs)
                        product_entity_projection.update(product_context_entity(updated_product_context))
                        if presentation_mode == "recommend":
                            recommended_products_for_turn.clear()
                            recommended_products_for_turn.extend(
                                _recommended_products_from_refs(presented_refs, product_candidates_for_turn)
                            )

                    # Match the synchronous path: retrieval/tool observations
                    # are evidence, while the Ecommerce Procedure's validated
                    # candidate-frame/selection projection is authoritative and
                    # must be applied last.  Otherwise a stream done event can
                    # accidentally retire a product selected earlier this turn.
                    if product_entity_projection:
                        _merge_last_entities(last_entities, product_entity_projection)
                    decision_facts = event.get("decision_facts", {})
                    if not isinstance(decision_facts, dict):
                        decision_facts = {}
                    decision_contexts = event.get("decision_contexts", [])
                    if not isinstance(decision_contexts, list):
                        decision_contexts = []
                    guarded_result: LoopResult | None = None
                    if response_fact_sensitive:
                        guarded_result = LoopResult(
                            answer=answer or "".join(buffered_response_tokens),
                            decision_facts=decision_facts,
                            decision_contexts=decision_contexts,
                        )
                        _apply_customer_refund_fact_boundary(
                            intent,
                            guarded_result,
                            query=chat_req.query,
                            recent_case=recent_support_case,
                            session_facts=session_facts,
                            session_contexts=session_contexts,
                            session_messages=session_ctx.messages,
                        )
                        answer = guarded_result.answer
                        if answer:
                            yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                    presentation_result = guarded_result or LoopResult(
                        answer=answer,
                        decision_facts=decision_facts,
                        decision_contexts=decision_contexts,
                    )
                    final_answer = answer
                    if tool_context.role == "customer" and _can_append_generic_customer_action(
                        presentation_result,
                        trusted_navigation=_trusted_navigation_action(
                            intent,
                            canonical_product=canonical_product_identity(last_entities) is not None,
                            recommended_product_actions=bool(recommended_products_for_turn),
                        ),
                    ):
                        final_answer = _append_customer_action_suffix(
                            answer,
                            intent.target,
                            intent.table,
                            chat_req.query,
                            last_entities.get("product", ""),
                            last_entities.get("product_id", ""),
                            last_entities.get("product_category", ""),
                            last_entities.get("component_category", ""),
                            recommended_products=recommended_products_for_turn,
                            intent_domain=intent.domain,
                            intent_operation=intent.operation,
                        )
                    if final_answer != answer:
                        # Streaming may already have emitted the model prose.  Emit
                        # only a pure suffix incrementally; if stale generic prose
                        # had to be removed, the authoritative done event below
                        # replaces the visible final answer atomically.
                        if final_answer.startswith(answer):
                            suffix = final_answer[len(answer) :]
                            if suffix:
                                yield f"data: {json.dumps({'event': 'token', 'content': suffix}, ensure_ascii=False)}\n\n"
                        answer = final_answer
                    presentation_result.answer = answer
                    presentation = _customer_presentation(
                        presentation_result,
                        intent,
                        case=recent_support_case,
                    )
                    _attach_answer_trace(
                        presentation_result,
                        intent,
                        raw_query=chat_req.query,
                        retrieval_query=retrieval_query,
                        tool_context=operator_tool_context,
                    )
                    stream_res["answer"] = answer
                    stream_res["total_steps"] = event.get("total_steps", 0)
                    stream_res["total_tokens"] = event.get("total_tokens", 0)
                    stream_res["decision_facts"] = decision_facts
                    stream_res["decision_contexts"] = decision_contexts
                    stream_res["presentation"] = presentation
                    stream_completed = True
                    pending_done_event = {
                        "event": "done",
                        "answer": answer,
                        "session_id": session_id,
                        "total_steps": stream_res["total_steps"],
                        "total_tokens": stream_res["total_tokens"],
                        "presentation": presentation,
                    }
                    continue

                if event.get("event") == "token" and response_fact_sensitive:
                    buffered_response_tokens.append(str(event.get("content") or ""))
                    continue

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if stream_completed:
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return
                phase = "persist"
                await session.add_turn(
                    session_id,
                    user_id,
                    chat_req.history_content,
                    LoopResult(
                        answer=stream_res["answer"],
                        total_steps=stream_res["total_steps"],
                        total_latency_ms=(time.perf_counter() - start_t) * 1000,
                        last_entities=last_entities,
                        decision_facts=stream_res["decision_facts"],
                        decision_contexts=stream_res["decision_contexts"],
                        response_control=(guarded_result.response_control if guarded_result is not None else {}),
                        customer_presentation=stream_res["presentation"] or {},
                        answer_source=presentation_result.answer_source,
                        answer_trace=presentation_result.answer_trace,
                    ),
                )
                if pending_done_event is not None:
                    yield f"data: {json.dumps(pending_done_event, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            raise
        except (DependencyUnavailableError, LLMError):
            yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
        except Exception as exc:
            _chat_logger.error(
                "chat stream generation failed phase=%s error_type=%s",
                phase,
                type(exc).__name__,
            )
            if phase == "persist":
                persistence_error = _stream_error_event(
                    request,
                    message="会话暂时无法保存，请稍后重试",
                )
                yield f"data: {json.dumps(persistence_error, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"

    return StreamingResponse(content=generate(), media_type="text/event-stream")
