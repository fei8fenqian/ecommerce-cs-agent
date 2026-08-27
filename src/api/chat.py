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

from agent.engines.loop import LoopResult
from agent.llm.intent_router import Intent, IntentRouter, build_route_instruction
from agent.llm.resolve import resolve_stock_follow_up
from agent.llm.sentiment import build_escalation_prompt, detect_sentiment
from agent.rag.retrieve import hybrid_search
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
from store.product_catalog_store import build_public_product_context, get_product_detail
from store.support_case_store import SupportCase

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
        "check_stock",
        "track_order",
        "check_payment_status",
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
) -> SupportCase | None:
    if case is None:
        return None
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
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
) -> None:
    """将复杂请求的下一轮语义显式保存，避免短回复退化为新问题。"""
    if case is None or case.status == "AWAITING_STAFF":
        return
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return
    requests = intent.support_requests
    if not requests:
        return
    primary = requests[0]
    pending = {
        "kind": "customer_confirmation" if primary.risk != "read_only" else "customer_choice",
        "operation": primary.operation,
        "next_step": primary.next_step,
        "missing_facts": primary.missing_facts,
        "options_limit": 3,
    }
    pending_command = {
        "status": "PROPOSED_NOT_EXECUTED",
        "operation": primary.operation,
        "risk": primary.risk,
        "requires_explicit_confirmation": primary.risk != "read_only",
    }
    await service.await_customer(
        case,
        pending=pending,
        pending_command=pending_command,
        request_stack=_support_case_payloads(intent),
    )


async def _record_support_case_facts(
    request: Request,
    *,
    case: SupportCase | None,
    loop_result: LoopResult,
) -> SupportCase | None:
    """把 Workflow 的受控只读结果持久化为下一轮可复用的事实。"""
    if case is None or not loop_result.verified_facts:
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if not isinstance(service, SupportCaseService):
        return case
    # 乐观锁竞争时不使用旧 Case 覆盖另一标签页的新选择；本轮仍可完成回答，下一轮
    # 会重新读取最新状态。
    return await service.record_verified_facts(case, facts=loop_result.verified_facts)


def _support_case_needs_customer_turn(intent: Intent, case: SupportCase | None = None) -> bool:
    """判断 Workflow 是否确实还需要客户选择/补充/确认。"""
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
    if _support_case_needs_customer_turn(intent, case):
        await _await_support_case_customer(request, case=case, intent=intent)
        return case
    service = getattr(request.app.state, "support_case_service", None)
    if isinstance(service, SupportCaseService):
        await service.complete(
            case,
            outcome={"completion": "read_only_answer_returned", "request_count": len(intent.support_requests)},
        )
    return case


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
    authorized_tool_context = ToolContext(user_id=tool_context.user_id, role=tool_context.role)
    result = await registry.execute(
        "create_ticket",
        tool_context=authorized_tool_context,
        issue=ticket_issue,
        urgency="medium",
    )
    ticket_id = str(result.data.get("ticket_id") or "") if result.is_success else ""
    if not ticket_id:
        raise DependencyUnavailableError("工单服务暂时不可用")

    return ticket_id, f"已为您创建售后工单 {ticket_id}。智能客服正在处理中，您也可以在当前会话补充问题细节。"


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


def _customer_action_suffix(intent_target: str, table: str, query: str, product_name: str = "") -> str:
    """为客户的下一步操作附加确定性站内链接，而不是让模型临时编造 URL。"""
    normalized = "".join(query.split()).lower()
    if intent_target == "ticket":
        return "\n\n[查看售后进度](?page=tickets)"
    if any(marker in normalized for marker in ("退款", "退货", "退钱", "想退", "不想要")):
        return "\n\n[前往我的订单申请退款](?page=orders)"
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
        active_case_context = (
            SupportCaseService.to_prompt_context(active_support_case) if active_support_case is not None else ""
        )
        # 单次轻量调用同时完成上下文 query 重写和意图路由，不增加额外模型往返。
        if active_case_context:
            intent = await intent_router.route(
                resolved_query,
                history=ctx.history,
                case_context=active_case_context,
            )
        else:
            intent = await intent_router.route(resolved_query, history=ctx.history)
        confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
        resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
        if resuming_support_case and active_support_case is not None:
            intent = _intent_for_case_reply(active_support_case, intent)
            active_support_case = await _resume_pending_case(request, case=active_support_case)
        if selected_product_context and intent.target != "ticket":
            intent.target = "rag"
            intent.table = selected_product_table
            intent.scenario = ""
        effective_query = intent.query or resolved_query
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
            answer += _customer_action_suffix(intent.target, intent.table, effective_query)
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, answer)
            return ChatResponse(
                answer=answer,
                session_id=ctx.session_id,
                total_steps=1,
                total_tokens=0,
            )

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
            loop_result = await support_workflow.run(
                effective_query,
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                case_context=case_context,
                support_requests=_support_case_payloads(intent),
                tool_context=tool_context,
            )
            await _persist_support_case_progress(
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
            context = _build_context(docs, customer_view=tool_context.role == "customer")
            if selected_product_context:
                context = f"{selected_product_context}\n\n相关资料：\n{context}"
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
                history=ctx.history,
                system_prompt_extra=agent_prompt_extra,
                tool_context=tool_context,
            )

        if tool_context.role == "customer":
            loop_result.answer += _customer_action_suffix(
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
        active_case_context = (
            SupportCaseService.to_prompt_context(active_support_case) if active_support_case is not None else ""
        )
        if active_case_context:
            intent = await intent_router.route(resolve_query, history=history, case_context=active_case_context)
        else:
            intent = await intent_router.route(resolve_query, history=history)
        confirmed_human_handoff = _is_confirmed_human_handoff(active_support_case, chat_req.query)
        resuming_support_case = _is_pending_case_reply(active_support_case, intent, chat_req.query)
        if resuming_support_case and active_support_case is not None:
            intent = _intent_for_case_reply(active_support_case, intent)
            active_support_case = await _resume_pending_case(request, case=active_support_case)
        if selected_product_context and intent.target != "ticket":
            intent.target = "rag"
            intent.table = selected_product_table
            intent.scenario = ""
        effective_query = intent.query or resolve_query
        sentiment = detect_sentiment(effective_query, history=history)
        extra_prompt = _compose_prompt_extras(
            build_escalation_prompt(sentiment),
            build_route_instruction(intent),
        )
        context = ""
        if intent.target == "rag":
            docs = await hybrid_search(
                effective_query,
                table=intent.table,
                use_rerank=_should_rerank(effective_query, intent.table),
            )
            context = _build_context(docs, customer_view=tool_context.role == "customer")
            if selected_product_context:
                context = f"{selected_product_context}\n\n相关资料：\n{context}"
            last_entities = _entities_from_retrieval(intent.table, docs)
            if selected_product_name:
                last_entities["product"] = selected_product_name
    except DependencyUnavailableError:
        raise
    except LLMError as exc:
        raise DependencyUnavailableError("智能服务暂时不可用") from exc

    stream_res = {"answer": "", "total_steps": 0, "total_tokens": 0}
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
                    answer += _customer_action_suffix(intent.target, intent.table, effective_query)
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
                workflow_result = await support_workflow.run(
                    effective_query,
                    history=history,
                    system_prompt_extra=extra_prompt,
                    case_context=case_context,
                    support_requests=_support_case_payloads(intent),
                    tool_context=tool_context,
                )
                answer = workflow_result.answer
                if tool_context.role == "customer":
                    answer += _customer_action_suffix(
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
                phase = "persist"
                await _persist_support_case_progress(
                    request,
                    case=support_case,
                    intent=intent,
                    loop_result=workflow_result,
                )
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
                    suffix = ""
                    if tool_context.role == "customer":
                        suffix = _customer_action_suffix(
                            intent.target,
                            intent.table,
                            effective_query,
                            last_entities.get("product", ""),
                        )
                    if suffix:
                        answer += suffix
                        yield f"data: {json.dumps({'event': 'token', 'content': suffix}, ensure_ascii=False)}\n\n"
                    event = {**event, "answer": answer}
                    stream_res["answer"] = answer
                    stream_res["total_steps"] = event.get("total_steps", 0)
                    stream_completed = True
                    pending_done_event = event
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
