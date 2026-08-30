import asyncio
import json
import logging
import re
import time
import uuid
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.customer_response import compose_customer_response
from agent.decision_context import (
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
from agent.support_control import confirmation_required
from agent.support_subjects import looks_like_pending_subject_choice, resolve_pending_subject_choice
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
from store.product_catalog_store import build_public_product_context, get_product_detail
from store.support_case_store import SupportCase
from store.ticket_store import enqueue_human_ticket

_chat_logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    session_id: str | None = Field(None, description="不传则自动创建新会话")
    replace_from_sequence: int | None = Field(
        None,
        ge=0,
        description="编辑已有用户消息时，删除该条及之后的历史后再发送本消息",
    )
    product_category: Literal["laptops", "phones", "components"] | None = None
    product_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    total_steps: int
    total_tokens: int


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


def _support_case_payloads(intent: Intent) -> list[dict]:
    """把路由器的受控多请求结构转换为可持久化 Case payload。"""
    return [support_request.to_case_payload() for support_request in intent.support_requests]


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
    merged = dict(session_facts or {})
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
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """为普通 AgentLoop 结果恢复最小退款事实边界输入。

    正常 SupportWorkflow 会把请求保存在 Case/Workflow state；兼容的普通 AgentLoop
    路径可能只有 intent 字段或工具事实，因此这里只根据受控请求/事实选择已实现的
    status、eligibility 渲染，不从客户文本猜业务结论。
    """
    requests = _support_case_payloads(intent)
    refund_requests = [request for request in requests if str(request.get("domain") or "") == "refund"]
    if refund_requests:
        return refund_requests
    recent_requests = _case_refund_requests(recent_case)
    facts, _ = _merged_refund_facts(
        result,
        recent_case=recent_case,
        session_facts=session_facts,
        session_contexts=session_contexts,
    )
    # 非退款路由也可能在本轮读到了退款资格事实（例如订单售后流程）。优先渲染
    # 资格结论，避免模型把 true 扩写成“全额退款资格”。
    if isinstance(facts.get("refund_eligibility"), bool) and (
        intent.domain != "refund" or intent.operation not in {"status", "eligibility"}
    ):
        return [{"domain": "refund", "operation": "eligibility"}]
    if recent_requests:
        return recent_requests[:1]
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
    if "refund_status" in facts:
        return [{"domain": "refund", "operation": "status"}]
    if isinstance(facts.get("refund_eligibility"), bool):
        return [{"domain": "refund", "operation": "eligibility"}]
    return []


def _apply_customer_refund_fact_boundary(
    intent: Intent,
    result: LoopResult,
    *,
    query: str = "",
    recent_case: SupportCase | None = None,
    session_facts: dict[str, object] | None = None,
    session_contexts: list[dict[str, object]] | None = None,
) -> None:
    """普通/流式出口统一委托共享 customer-response composer。"""
    requests = _refund_boundary_requests(
        intent,
        result,
        recent_case=recent_case,
        session_facts=session_facts,
        session_contexts=session_contexts,
    )
    historical_contexts = merge_decision_contexts(
        session_contexts or [],
        _case_decision_contexts(recent_case),
        default_provenance="historical",
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
    opened = await service.open_or_resume(
        session_id=session_id,
        customer_user_id=customer_user_id,
        request_stack=payloads,
    )
    return opened.case


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
    """仅在 Agent 已经进行过一次澄清后接受客户的人工交接确认。"""
    if case is None or case.status != "AWAITING_CUSTOMER":
        return False
    operations = {str(item.get("operation") or "") for item in case.request_stack if isinstance(item, dict)}
    if "human_handoff" not in operations:
        return False
    normalized = re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]]+", "", raw_query).lower()
    return normalized in {"是", "好的", "好", "确认", "需要", "仍需人工", "还是要人工", "转人工", "人工客服"}


async def _resume_pending_case(
    request: Request,
    *,
    case: SupportCase | None,
    raw_query: str,
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
        updated = await service.select_customer_subject(
            case,
            subject=choice,
            selection_source=str(selected.get("selection_source") or "choice"),
        )
        return updated or case
    return await service.resume_customer_response(case)


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
        "missing_facts": list((workflow_progress or {}).get("missing_facts") or []),
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
    if next_actor in {"STAFF", "SYSTEM"}:
        service = getattr(request.app.state, "support_case_service", None)
        if isinstance(service, SupportCaseService):
            updated = await service.mark_awaiting_staff(
                case,
                reason=str(workflow_progress.get("reason") or "CONTROL_PLANE_BLOCKED"),
                handoff_summary={
                    "goal": str(workflow_progress.get("goal") or "customer_support"),
                    "next_actor": next_actor,
                    "next_action": str(workflow_progress.get("next_action") or ""),
                    "unsupported_workflows": list(workflow_progress.get("unsupported_workflows") or []),
                    "unavailable_capabilities": list(workflow_progress.get("unavailable_capabilities") or []),
                },
            )
            return updated or case
        return case
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
                "completion": resolution_type or "read_only_answer_returned",
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

    return ticket_id, f"已为您创建售后工单 {ticket_id}，已转人工客服处理。您可以在当前会话补充问题细节。"


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


@chat_router.post("/chat", response_model=ChatResponse)
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

        # 判断指代词对应的实体
        resolved_query = await session.resolve(chat_req.query, ctx.session_id, user_id)
        resolved_query = resolve_stock_follow_up(
            resolved_query,
            ctx.last_entities,
            ctx.history,
        )
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
        active_case_context = (
            SupportCaseService.to_prompt_context(active_support_case) if active_support_case is not None else ""
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
        confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
        resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
        if resuming_support_case and active_support_case is not None:
            intent = _intent_for_case_reply(active_support_case, intent)
            active_support_case = await _resume_pending_case(
                request,
                case=active_support_case,
                raw_query=chat_req.query,
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
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, answer)
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=1,
                total_tokens=0,
            )
        if support_decision.action in {
            CustomerSupportAction.SHOW_REFUND_PROGRESS,
            CustomerSupportAction.OFFER_REFUND_SELF_SERVICE,
            CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING,
        }:
            answer = support_decision.answer
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, answer)
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
            _, answer = await _create_customer_ticket(
                request,
                issue=chat_req.query,
                tool_context=tool_context,
                history=ctx.messages,
            )
            answer = _append_customer_action_suffix(answer, intent.target, intent.table, effective_query)
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, answer)
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=1,
                total_tokens=0,
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
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, plan_state.get("answer", ""))
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
                    request, intent=intent, session_id=ctx.session_id, customer_user_id=user_id
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
            )

        if (
            tool_context.role == "customer"
            and loop_result.workflow_progress.get("resolution_type") != "SELF_SERVICE_HANDOFF"
        ):
            loop_result.answer = _append_customer_action_suffix(
                loop_result.answer,
                intent.target,
                intent.table,
                effective_query,
                loop_result.last_entities.get("product", ""),
            )

        # 当前对话放入上下文ctx
        await session.add_turn(ctx.session_id, user_id, chat_req.query, loop_result)
        return ChatResponse(
            answer=loop_result.answer,
            session_id=ctx.session_id,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
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
        resolve_query = await session.resolve(chat_req.query, session_id, user_id)
        resolve_query = resolve_stock_follow_up(
            resolve_query,
            session_ctx.last_entities,
            history,
        )
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
        active_case_context = (
            SupportCaseService.to_prompt_context(active_support_case) if active_support_case is not None else ""
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
        confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
        resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
        if resuming_support_case and active_support_case is not None:
            intent = _intent_for_case_reply(active_support_case, intent)
            active_support_case = await _resume_pending_case(
                request,
                case=active_support_case,
                raw_query=chat_req.query,
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
    }
    start_t = time.perf_counter()
    support_decision = decide_customer_support_action(
        intent_target=intent.target,
        role=tool_context.role,
        query=chat_req.query,
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

            if support_decision.action in {
                CustomerSupportAction.SHOW_REFUND_PROGRESS,
                CustomerSupportAction.OFFER_REFUND_SELF_SERVICE,
                CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING,
            }:
                answer = support_decision.answer
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                try:
                    await session.add_turn_simple(session_id, user_id, chat_req.query, answer)
                except Exception as exc:
                    _chat_logger.error(
                        "customer support guidance persistence failed action=%s error_type=%s",
                        support_decision.action,
                        type(exc).__name__,
                    )
                done_event = {"event": "done", "answer": answer, "total_steps": 0}
                yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
                return

            if create_customer_ticket:
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                phase = "ticket_create"
                ticket_id, answer = await _create_customer_ticket(
                    request,
                    issue=chat_req.query,
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
                if not await _is_current_chat_run(session_id, chat_run_id):
                    yield f"data: {json.dumps(_superseded_stream_event(request), ensure_ascii=False)}\n\n"
                    return

                phase = "ticket_events"
                yield f"data: {json.dumps({'event': 'tool_call', 'name': 'create_ticket'}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'event': 'token', 'content': answer}, ensure_ascii=False)}\n\n"
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "total_steps": 1,
                    "ticket_id": ticket_id,
                }
                phase = "persist"
                try:
                    await session.add_turn_simple(session_id, user_id, chat_req.query, answer)
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
                        request, intent=intent, session_id=session_id, customer_user_id=user_id
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
                    )
                    answer = workflow_result.answer
                if (
                    tool_context.role == "customer"
                    and workflow_result.workflow_progress.get("resolution_type") != "SELF_SERVICE_HANDOFF"
                    and "generate_refund_entry"
                    not in workflow_result.workflow_progress.get("unavailable_capabilities", [])
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
                    chat_req.query,
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
                    ),
                )
                done_event = {
                    "event": "done",
                    "answer": answer,
                    "total_steps": workflow_result.total_steps,
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
                        stream_res["answer"] = data.get("answer", "")
                        stream_res["total_steps"] = len(data.get("plan", []))
                        stream_res["total_tokens"] = data.get("total_tokens", 0)
                        stream_completed = True
                        pending_done_event = chunk
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
                        chat_req.query,
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
                    event = {**event, "answer": answer}
                    stream_res["answer"] = answer
                    stream_res["total_steps"] = event.get("total_steps", 0)
                    stream_res["decision_facts"] = decision_facts
                    stream_res["decision_contexts"] = decision_contexts
                    stream_completed = True
                    pending_done_event = event
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
                    chat_req.query,
                    LoopResult(
                        answer=stream_res["answer"],
                        total_steps=stream_res["total_steps"],
                        total_latency_ms=(time.perf_counter() - start_t) * 1000,
                        last_entities=last_entities,
                        decision_facts=stream_res["decision_facts"],
                        decision_contexts=stream_res["decision_contexts"],
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
