import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Literal
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
from agent.rag.knowledge_context import format_knowledge_context
from agent.rag.retrieve import hybrid_search, pre_retrieve_knowledge
from agent.subject_correction import (
    SubjectCorrection,
    detect_subject_correction,
    resolve_subject_description,
)
from agent.support_control import confirmation_required
from agent.support_subjects import (
    looks_like_bare_subject_description,
    looks_like_pending_subject_choice,
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
from store.product_catalog_store import build_public_product_context, get_product_detail
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
        "search_component",
    }
)


def _compose_prompt_extras(*parts: str) -> str:
    """合并受控的 Agent 提示片段，避免空段落污染模型上下文。"""
    return "\n\n".join(part.strip() for part in parts if part and part.strip())


def _can_append_generic_customer_action(result: LoopResult) -> bool:
    """Do not append unrelated navigation beneath a controlled response card."""
    mode = str((result.response_control or {}).get("mode") or "")
    return mode in {"", "GENERIC"}


def _support_case_payloads(intent: Intent) -> list[dict]:
    """把路由器的受控多请求结构转换为可持久化 Case payload。"""
    return [support_request.to_case_payload() for support_request in intent.support_requests]


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
            "recent_goal": [
                {"domain": item.get("domain"), "operation": item.get("operation")}
                for item in case.request_stack[:3]
                if isinstance(item, dict)
            ],
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
    initial_selected_subjects = await _immediate_trusted_subject_continuation(
        request,
        intent=intent,
        recent_case=recent_case,
        raw_query=raw_query,
        tool_context=tool_context,
    )
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
    if _trusted_subject_relation(intent, recent_order_id=selected_order_id, raw_query=raw_query) != "same":
        return None
    if not await _verify_customer_order_subject(
        request,
        order_id=selected_order_id,
        tool_context=tool_context,
    ):
        return None
    return {"order_id": selected_order_id}


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
    if intent.case_update == "new_request":
        return False
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
        confidence=max(intent.confidence, 0.9),
        domain=primary.domain,
        operation=primary.operation,
        state="pending",
        next_step=primary.next_step,
        required_tools=tools,
        requests=requests,
        case_update="continue",
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
        selected = resolve_pending_subject_choice(raw_query, choices)
        if selected is None:
            # 选择不唯一时保持 AWAITING_CUSTOMER；后续 Workflow 会再次使用同一
            # 候选帧提问，不能依赖模型猜测或重新按数据库顺序选单。
            return case
        choice = selected.get("choice")
        if not isinstance(choice, dict):
            return case
        if tool_context is not None:
            return await _select_verified_pending_subject(
                request,
                case=case,
                subject=choice,
                selection_source=str(selected.get("selection_source") or "choice"),
                tool_context=tool_context,
            )
        if case.pending.get("selection_event") == "subject_correction" or case.pending.get("transition_from_order_id"):
            # A correction changes an authoritative subject, so the ownership
            # re-check is mandatory even for internal callers of this helper.
            # Without the request-scoped ToolContext, fail closed instead of
            # transitioning from a natural-language match alone.
            return case
        selection_source = str(selected.get("selection_source") or "choice")
        updated = await service.select_customer_subject(
            case,
            subject=choice,
            selection_source=selection_source,
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
    return choice


async def _customer_subject_choices(customer_user_id: int) -> list[dict[str, object]]:
    """只读取当前客户最近 30 笔 checkout 订单，保持数据库展示顺序。"""
    orders = await list_customer_checkout_orders(customer_user_id, limit=30)
    choices: list[dict[str, object]] = []
    seen: set[str] = set()
    for order in orders:
        choice = _subject_choice_from_checkout_order(order)
        if choice is None:
            continue
        order_id = str(choice["order_id"])
        if order_id in seen:
            continue
        seen.add(order_id)
        choices.append(choice)
    return choices[:30]


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
    resolution_kind, subject, matches = resolve_subject_description(correction.subject_description, choices)
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
    resolution_kind, subject, matches = resolve_subject_description(query, choices)
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
    """把已判断为新诉求的内容加入现有案件，避免覆盖未完成的 pending。"""
    if case is None or intent.case_update != "new_request" or not intent.support_requests:
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    existing = IntentRouter.support_requests_from_case_payloads(case.request_stack)
    merged_payloads = [item.to_case_payload() for item in existing]
    for item in intent.support_requests:
        payload = item.to_case_payload()
        if payload not in merged_payloads:
            merged_payloads.append(payload)
    merged_payloads = merged_payloads[:3]
    updated = await service.record_requests(
        case,
        request_stack=merged_payloads,
        event_payload={"request_count": len(merged_payloads), "reason": "NEW_REQUEST_DURING_ACTIVE_CASE"},
    )
    return updated or case


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
    requests = intent.support_requests
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
        "options_limit": 3,
    }
    if workflow_progress:
        pending["execution"] = workflow_progress
    if pending["kind"] == "customer_choice":
        choices = workflow_progress.get("pending_choices") if workflow_progress else None
        if isinstance(choices, list) and choices:
            # 这个顺序就是 SupportWorkflow 实际展示给客户的顺序，后续序号解析
            # 只能读取这一帧，不能重新查询或排序。
            pending["subject_type"] = "order"
            pending["choices"] = choices[:3]
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
    requests = intent.support_requests
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
                "request_count": len(intent.support_requests),
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


def _merge_evidence_context(*contexts: str) -> str:
    """保留来源边界地合并产品上下文和知识上下文。"""
    return "\n\n".join(context.strip() for context in contexts if context and context.strip())


def _customer_action_suffix(
    intent_target: str,
    table: str,
    query: str,
    product_name: str = "",
    trusted_refund_entry: str = "",
) -> str:
    """为客户的下一步操作附加确定性站内链接，而不是让模型临时编造 URL。"""
    normalized = "".join(query.split()).lower()
    if intent_target == "ticket":
        return "\n\n[查看售后进度](?page=tickets)"
    # 退款入口只能由 SupportWorkflow 在资格核验后通过可信后端生成。不能再根据
    # 原始 query 中的“退款”一词拼通用链接，否则 STATEMENT/状态查询也会被误导。
    if trusted_refund_entry.startswith("?page=orders&refund_order=SO"):
        return f"\n\n[前往我的订单申请退款]({trusted_refund_entry})"
    if any(marker in normalized for marker in ("订单", "物流", "发货", "签收")):
        return "\n\n[查看我的订单](?page=orders)"
    if table in {"laptop_products", "phone_products"} or any(
        marker in normalized for marker in ("购买", "买", "下单", "商品", "笔记本", "手机")
    ):
        search = product_name.strip()
        if search:
            return f"\n\n[去商品目录查看](?page=catalog&q={quote(search)})"
        return "\n\n[去商品目录查看](?page=catalog)"
    return ""


def _append_customer_action_suffix(
    answer: str,
    intent_target: str,
    table: str,
    query: str,
    product_name: str = "",
) -> str:
    """追加稳定的站内链接，但不重复模型已经生成的同一链接。"""
    suffix = _customer_action_suffix(intent_target, table, query, product_name)
    if suffix and suffix.strip() not in answer:
        return answer + suffix
    return answer


def _entities_from_retrieval(table: str, docs: list[dict]) -> dict[str, str]:
    """把商品检索首选结果保存为下一轮可解析的会话事实。"""
    if table not in {"laptop_products", "phone_products"} or not docs:
        return {}
    title = str(docs[0].get("title") or "").strip()
    return {"product": title} if title else {}


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
            correction_case = _bound_subject_correction_case(active_support_case, recent_support_case)
            correction_kind = "none"
            if correction_case is not None:
                correction_kind, corrected_case, _, correction_choices = await _prepare_customer_subject_correction(
                    request,
                    case=correction_case,
                    query=chat_req.query,
                    history=ctx.history,
                    customer_user_id=user_id,
                    tool_context=tool_context,
                )
                if correction_kind == "unique" and corrected_case is not None:
                    active_support_case = corrected_case
                    recent_support_case = corrected_case
                    intent = _subject_correction_intent(corrected_case, resolved_query)
                    confirmed_human_handoff = False
                    resuming_support_case = True
                elif (
                    correction_kind in {"multiple", "description", "none_found", "unavailable"}
                    and corrected_case is not None
                ):
                    intent = _subject_correction_intent(corrected_case, resolved_query)
                    correction_result, correction_presentation = _subject_correction_short_circuit(
                        correction_kind,
                        case=corrected_case,
                        intent=intent,
                        choices=correction_choices,
                    )
                    await session.add_turn_simple(
                        ctx.session_id,
                        user_id,
                        chat_req.history_content,
                        correction_result.answer,
                        presentation=correction_presentation,
                    )
                    return ChatResponse(
                        answer=correction_result.answer,
                        session_id=ctx.session_id,
                        total_steps=0,
                        total_tokens=0,
                        presentation=correction_presentation,
                    )
            if correction_kind == "none":
                active_case_context = (
                    SupportCaseService.to_prompt_context(active_support_case)
                    if active_support_case is not None
                    else _recent_subject_router_context(recent_support_case)
                )
                # Pre-RAG 只给 Router 解释项目术语；业务事实和最终回答证据仍由后续层获取。
                pre_knowledge_context = await _pre_route_knowledge_context(resolved_query)
                if active_case_context:
                    intent = await intent_router.route(
                        resolved_query,
                        history=ctx.history,
                        case_context=active_case_context,
                        knowledge_context=pre_knowledge_context,
                    )
                else:
                    intent = await intent_router.route(
                        resolved_query,
                        history=ctx.history,
                        knowledge_context=pre_knowledge_context,
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
        effective_query = intent.query or resolved_query
        evidence_plan = resolve_evidence(
            domain=intent.domain,
            operation=intent.operation,
            target=intent.target,
            table=intent.table,
        )
        # `rag/knowledge_chunks` 兼容路径会在下方复用原有检索；其他路径可同时携带
        # 受控知识和实时 Workflow/Tool 事实，而不再二选一。
        knowledge_context = await _deep_knowledge_context(
            effective_query,
            required=evidence_plan.needs_deep_knowledge
            and not (intent.target == "rag" and intent.table == "knowledge_chunks"),
        )
        sentiment = detect_sentiment(effective_query, history=ctx.history)
        sentiment_ctx = build_escalation_prompt(sentiment)
        route_ctx = build_route_instruction(intent)
        agent_prompt_extra = _compose_prompt_extras(sentiment_ctx, route_ctx)

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
            answer = _append_customer_action_suffix(answer, intent.target, intent.table, effective_query)
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
                effective_query,
                history=ctx.history,
                scenario=intent.scenario,
                tool_context=tool_context,
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
            case_context = SupportCaseService.to_prompt_context(support_case) if support_case is not None else ""
            workflow_kwargs: dict[str, object] = {}
            if support_case is not None and support_case.selected_subjects:
                workflow_kwargs["selected_subjects"] = support_case.selected_subjects
            try:
                loop_result = await support_workflow.run(
                    effective_query,
                    context=_merge_evidence_context(selected_product_context, knowledge_context),
                    history=ctx.history,
                    system_prompt_extra=agent_prompt_extra,
                    case_context=case_context,
                    support_requests=_support_case_payloads(intent),
                    tool_context=tool_context,
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
            docs = await hybrid_search(
                effective_query,
                table=intent.table,
                use_rerank=_should_rerank(effective_query, intent.table),
            )
            context = _merge_evidence_context(
                selected_product_context,
                _build_context(docs, customer_view=tool_context.role == "customer"),
                knowledge_context,
            )
            retrieved_entities = _entities_from_retrieval(intent.table, docs)
            if selected_product_name:
                retrieved_entities["product"] = selected_product_name
            loop_result = await agent.run(
                effective_query,
                context=context,
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                tool_context=tool_context,
            )
            loop_result.last_entities = {**retrieved_entities, **loop_result.last_entities}
        else:
            loop_result = await agent.run(
                effective_query,
                context=_merge_evidence_context(selected_product_context, knowledge_context),
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                tool_context=tool_context,
            )

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
            and _can_append_generic_customer_action(loop_result)
        ):
            loop_result.answer = _append_customer_action_suffix(
                loop_result.answer,
                intent.target,
                intent.table,
                effective_query,
                loop_result.last_entities.get("product", ""),
            )

        presentation = _customer_presentation(loop_result, intent, case=response_case)
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
    last_entities: dict[str, str] = {}
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
            correction_case = _bound_subject_correction_case(active_support_case, recent_support_case)
            correction_kind = "none"
            if correction_case is not None:
                correction_kind, corrected_case, _, correction_choices = await _prepare_customer_subject_correction(
                    request,
                    case=correction_case,
                    query=chat_req.query,
                    history=history,
                    customer_user_id=user_id,
                    tool_context=tool_context,
                )
                if correction_kind == "unique" and corrected_case is not None:
                    active_support_case = corrected_case
                    recent_support_case = corrected_case
                    intent = _subject_correction_intent(corrected_case, resolve_query)
                    confirmed_human_handoff = False
                    resuming_support_case = True
                elif (
                    correction_kind in {"multiple", "description", "none_found", "unavailable"}
                    and corrected_case is not None
                ):
                    intent = _subject_correction_intent(corrected_case, resolve_query)
                    subject_correction_short_circuit = _subject_correction_short_circuit(
                        correction_kind,
                        case=corrected_case,
                        intent=intent,
                        choices=correction_choices,
                    )
            if correction_kind == "none":
                active_case_context = (
                    SupportCaseService.to_prompt_context(active_support_case)
                    if active_support_case is not None
                    else _recent_subject_router_context(recent_support_case)
                )
                pre_knowledge_context = await _pre_route_knowledge_context(resolve_query)
                if active_case_context:
                    intent = await intent_router.route(
                        resolve_query,
                        history=history,
                        case_context=active_case_context,
                        knowledge_context=pre_knowledge_context,
                    )
                else:
                    intent = await intent_router.route(
                        resolve_query,
                        history=history,
                        knowledge_context=pre_knowledge_context,
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
        effective_query = intent.query or resolve_query
        evidence_plan = resolve_evidence(
            domain=intent.domain,
            operation=intent.operation,
            target=intent.target,
            table=intent.table,
        )
        knowledge_context = await _deep_knowledge_context(
            effective_query,
            required=evidence_plan.needs_deep_knowledge
            and not (intent.target == "rag" and intent.table == "knowledge_chunks"),
        )
        sentiment = detect_sentiment(effective_query, history=history)
        extra_prompt = _compose_prompt_extras(
            build_escalation_prompt(sentiment),
            build_route_instruction(intent),
        )
        context = _merge_evidence_context(selected_product_context, knowledge_context)
        if intent.target == "rag":
            docs = await hybrid_search(
                effective_query,
                table=intent.table,
                use_rerank=_should_rerank(effective_query, intent.table),
            )
            context = _merge_evidence_context(
                selected_product_context,
                _build_context(docs, customer_view=tool_context.role == "customer"),
                knowledge_context,
            )
            last_entities = _entities_from_retrieval(intent.table, docs)
            if selected_product_name:
                last_entities["product"] = selected_product_name
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
                    answer = _append_customer_action_suffix(answer, intent.target, intent.table, effective_query)
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
                case_context = SupportCaseService.to_prompt_context(support_case) if support_case is not None else ""
                workflow_kwargs: dict[str, object] = {}
                if support_case is not None and support_case.selected_subjects:
                    workflow_kwargs["selected_subjects"] = support_case.selected_subjects
                try:
                    workflow_result = await support_workflow.run(
                        effective_query,
                        context=context,
                        history=history,
                        system_prompt_extra=extra_prompt,
                        case_context=case_context,
                        support_requests=_support_case_payloads(intent),
                        tool_context=tool_context,
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
                if (
                    tool_context.role == "customer"
                    and workflow_result.workflow_progress.get("resolution_type")
                    not in {
                        "SELF_SERVICE_HANDOFF",
                        "SELF_SERVICE_ORDER_CANCEL",
                    }
                    and "generate_refund_entry"
                    not in workflow_result.workflow_progress.get("unavailable_capabilities", [])
                    and _can_append_generic_customer_action(workflow_result)
                ):
                    answer = _append_customer_action_suffix(
                        answer,
                        intent.target,
                        intent.table,
                        effective_query,
                        workflow_result.last_entities.get("product", ""),
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
                    effective_query,
                    history=history,
                    scenario=intent.scenario,
                    tool_context=tool_context,
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
                effective_query,
                context=context,
                history=history,
                system_prompt_extra=extra_prompt,
                tool_context=tool_context,
            ):
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return
                if event.get("event") == "error":
                    yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
                    return

                if event.get("event") == "tool_call":
                    args = event.get("args", {})
                    if "product_name" in args:
                        last_entities["product"] = str(args["product_name"])
                    if "order_id" in args:
                        last_entities["order"] = str(args["order_id"])

                if event.get("event") == "done":
                    answer = str(event.get("answer", ""))
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
                    suffix = ""
                    if tool_context.role == "customer":
                        suffix = _customer_action_suffix(
                            intent.target,
                            intent.table,
                            effective_query,
                            last_entities.get("product", ""),
                        )
                        if suffix and suffix.strip() in answer:
                            suffix = ""
                    if suffix:
                        answer += suffix
                        yield f"data: {json.dumps({'event': 'token', 'content': suffix}, ensure_ascii=False)}\n\n"
                    presentation_result = guarded_result or LoopResult(
                        answer=answer,
                        decision_facts=decision_facts,
                        decision_contexts=decision_contexts,
                    )
                    presentation = _customer_presentation(
                        presentation_result,
                        intent,
                        case=recent_support_case,
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
