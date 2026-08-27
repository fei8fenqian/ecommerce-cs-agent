import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent.rag.retrieve import hybrid_search
from log_config import redact_text
from service.support_case_service import SupportCaseService
from store.ticket_message_store import (
    create_agent_ticket_message,
    create_customer_ticket_message,
    list_agent_ticket_messages,
    list_customer_ticket_messages,
)
from store.ticket_store import (
    claim_ticket,
    close_agent_ticket,
    close_customer_ticket,
    create_ticket,
    get_agent_ticket,
    get_agent_ticket_escalation,
    get_customer_ticket,
    list_agent_tickets,
    list_customer_tickets,
    update_agent_ticket,
    update_customer_ticket,
)


class CustomerTicketItem(BaseModel):
    ticket_id: str
    customer_name: str
    urgency: str
    status: str
    created_at: str


class AgentTicketItem(BaseModel):
    ticket_id: str
    urgency: str
    status: str
    created_at: str
    issue_summary: str


class CustomerTicketListResponse(BaseModel):
    tickets: list[CustomerTicketItem]
    total: int


class AgentTicketListResponse(BaseModel):
    tickets: list[AgentTicketItem]
    total: int


class CustomerTicketDetailResponse(BaseModel):
    ticket_id: str
    customer_name: str
    phone: str
    issue: str
    urgency: str
    status: str
    created_at: str


class AgentTicketSummaryResponse(BaseModel):
    ticket_id: str
    urgency: str
    status: str
    created_at: str
    issue_summary: str


class AgentTicketEscalationResponse(BaseModel):
    """当前客服可见的最近一次人工升级通知投递状态。"""

    status: str | None = None
    attempts: int = 0
    next_attempt_at: str | None = None
    delivered_at: str | None = None
    last_error_code: str | None = None


class AgentTicketDetailResponse(BaseModel):
    ticket_id: str
    customer_name: str
    phone: str
    issue: str
    urgency: str
    status: str
    created_at: str
    assigned_agent_id: int


class TicketUpdateRequest(BaseModel):
    status: str | None = None
    urgency: str | None = None


class ClaimResponse(BaseModel):
    ticket_id: str
    assigned_agent_id: int
    status: str
    created_at: str


class TicketCloseResponse(BaseModel):
    """工单由有权限的一方明确关闭后的结果。"""

    ok: bool = True
    status: str = "已关闭"


class KnowledgeReference(BaseModel):
    """客服草稿所依据的一条知识资料。"""

    title: str
    reference: str


class SupportReplyDraftResponse(BaseModel):
    """只返回给已认领工单客服的回复草稿，不会写回工单。"""

    ticket_id: str
    draft: str
    knowledge_references: list[KnowledgeReference]
    needs_human_follow_up: bool


class TicketMessageItem(BaseModel):
    """工单对话中的一条客户可见消息。"""

    message_id: int
    author_role: str
    content: str
    ai_assisted: bool = Field(description="客服提交时标记的 AI 辅助来源，不代表可验证的模型调用审计。")
    created_at: str


class TicketMessageListResponse(BaseModel):
    """按时间顺序返回的工单消息记录。"""

    messages: list[TicketMessageItem]


class TicketMessageCreateRequest(BaseModel):
    """客服提交给客户的工单回复。"""

    content: str = Field(max_length=4000)
    ai_assisted: bool = Field(
        default=False,
        description="客服自行标记是否采用 AI 草稿；MVP 不验证草稿来源。",
    )


class CustomerTicketMessageCreateRequest(BaseModel):
    """客户向自己工单补充的后续问题。"""

    content: str = Field(min_length=1, max_length=4000)


class CustomerTicketCreateRequest(BaseModel):
    """客户直接发起售后工单；身份和紧急度不由浏览器决定。"""

    issue: str = Field(min_length=1, max_length=4000)


ticket_router = APIRouter(prefix="/api/v1", tags=["工单"])


def _knowledge_context(documents: list[dict[str, Any]]) -> tuple[str, list[KnowledgeReference]]:
    """将检索结果缩短为可安全传给模型的知识上下文和公开引用。"""
    references: list[KnowledgeReference] = []
    excerpts: list[str] = []
    for index, document in enumerate(documents[:3], start=1):
        title = str(document.get("title") or "知识资料")
        reference = str(document.get("source") or document.get("id") or f"knowledge-{index}")
        content = str(document.get("content") or "").strip()
        if not content:
            continue
        references.append(KnowledgeReference(title=title, reference=reference))
        excerpts.append(f"资料 {len(references)}（{title}）：\n{content[:1200]}")
    return "\n\n".join(excerpts), references


@ticket_router.post("/tickets", response_model=CustomerTicketDetailResponse, status_code=201)
async def create_customer_ticket(
    request: Request,
    payload: CustomerTicketCreateRequest,
) -> CustomerTicketDetailResponse:
    """由客户创建工单，并优先交给自主售后 Agent 处理。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以创建工单")
    issue = payload.issue.strip()
    if not issue:
        raise HTTPException(status_code=400, detail="工单内容不能为空")

    ticket_id = f"TK{uuid4().hex[:14].upper()}"
    await create_ticket(
        ticket_id=ticket_id,
        issue=issue,
        urgency="medium",
        customer_user_id=user["id"],
        # 客户主动发起的售后与聊天工具创建的售后走同一条 Agent 队列。
        # 只有 Agent 无法可靠处理时才会转成“待人工处理”。
        status="AI待处理",
    )
    ticket_data = await get_customer_ticket(ticket_id, user["id"])
    if ticket_data is None:
        raise HTTPException(status_code=500, detail="工单创建后无法读取")
    return CustomerTicketDetailResponse(**ticket_data)


@ticket_router.post(
    "/agent/support-reply-drafts/{ticket_id}",
    response_model=SupportReplyDraftResponse,
)
async def create_support_reply_draft(ticket_id: str, request: Request) -> SupportReplyDraftResponse:
    """为当前客服已认领工单生成仅供内部查看的知识依据回复草稿。"""
    user = request.state.user
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="只有客服可以生成回复草稿")

    ticket_data = await get_agent_ticket(ticket_id, user["id"])
    if ticket_data is None or ticket_data.get("assigned_agent_id") != user["id"]:
        # 未认领、他人认领和不存在都统一为 404，避免泄露工单存在或归属。
        raise HTTPException(status_code=404, detail="工单不存在")

    issue = str(ticket_data.get("issue") or "").strip()
    if not issue:
        raise HTTPException(status_code=404, detail="工单不存在")
    # issue 是客户自由输入；仅让脱敏后的副本离开工单边界进入检索和模型。
    safe_issue = redact_text(issue)

    documents = await hybrid_search(safe_issue, table="knowledge_chunks")
    knowledge_context, references = _knowledge_context(documents)
    if not knowledge_context:
        return SupportReplyDraftResponse(
            ticket_id=ticket_id,
            draft="当前知识库中没有足够依据，需要人工补充后再回复客户。",
            knowledge_references=[],
            needs_human_follow_up=True,
        )

    llm_response = await request.app.state.llm_client.chat(
        [
            {
                "role": "system",
                "content": (
                    "你是内部客服助手。仅根据给定知识资料起草一段可供客服人工审核的回复。"
                    "不得编造政策、订单、物流、支付或退款事实；资料不足时必须写“需要人工补充”。"
                    "不要复述、推断或索要客户姓名、手机号等个人信息。只输出回复草稿正文。"
                ),
            },
            {
                "role": "user",
                "content": f"工单问题：\n{safe_issue}\n\n可用知识资料：\n{knowledge_context}",
            },
        ],
        temperature=0.0,
        max_tokens=500,
    )
    draft = (llm_response.content or "").strip()
    if not draft:
        return SupportReplyDraftResponse(
            ticket_id=ticket_id,
            draft="当前知识库不足以生成可靠回复，需要人工补充后再回复客户。",
            knowledge_references=references,
            needs_human_follow_up=True,
        )

    return SupportReplyDraftResponse(
        ticket_id=ticket_id,
        draft=draft,
        knowledge_references=references,
        needs_human_follow_up="需要人工补充" in draft,
    )


@ticket_router.get("/tickets/{ticket_id}/messages", response_model=TicketMessageListResponse)
async def ticket_messages(ticket_id: str, request: Request) -> TicketMessageListResponse:
    """返回当前客户自己的、或当前客服已认领工单的消息历史。"""
    user = request.state.user
    if user["role"] == "customer":
        messages = await list_customer_ticket_messages(ticket_id, user["id"])
    elif user["role"] == "agent":
        messages = await list_agent_ticket_messages(ticket_id, user["id"])
    else:
        raise HTTPException(status_code=403, detail="当前帐号无权查询工单消息")

    if messages is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return TicketMessageListResponse(messages=[TicketMessageItem(**message) for message in messages])


@ticket_router.post("/tickets/{ticket_id}/messages", response_model=TicketMessageItem)
async def send_ticket_message(
    ticket_id: str,
    request: Request,
    message: TicketMessageCreateRequest,
) -> TicketMessageItem:
    """由已认领客服发送客户可见回复；不会修改工单状态。"""
    user = request.state.user
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="只有客服可以发送工单回复")

    content = message.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="回复内容不能为空")

    created = await create_agent_ticket_message(ticket_id, user["id"], content, message.ai_assisted)
    if created is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return TicketMessageItem(**created)


@ticket_router.post("/tickets/{ticket_id}/customer-messages", response_model=TicketMessageItem)
async def send_customer_ticket_message(
    ticket_id: str,
    request: Request,
    message: CustomerTicketMessageCreateRequest,
) -> TicketMessageItem:
    """客户补充自己工单的问题；未认领工单会重新进入 AI 队列。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以补充工单消息")

    created = await create_customer_ticket_message(ticket_id, user["id"], message.content.strip())
    if created is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return TicketMessageItem(**created)


@ticket_router.get(
    "/tickets",
    response_model=CustomerTicketListResponse | AgentTicketListResponse,
)
async def tickets(request: Request, status: str | None = None):
    user_id = request.state.user["id"]
    role = request.state.user["role"]

    if role == "customer":
        ticket_rows = await list_customer_tickets(user_id, status)
        return CustomerTicketListResponse(
            tickets=[CustomerTicketItem(**ticket) for ticket in ticket_rows],
            total=len(ticket_rows),
        )

    if role == "agent":
        ticket_rows = await list_agent_tickets(user_id, status)
        return AgentTicketListResponse(
            tickets=[AgentTicketItem(**ticket) for ticket in ticket_rows],
            total=len(ticket_rows),
        )

    raise HTTPException(status_code=403, detail="当前帐号无权查询工单")


@ticket_router.get(
    "/tickets/{ticket_id}",
    response_model=(AgentTicketDetailResponse | CustomerTicketDetailResponse | AgentTicketSummaryResponse),
)
async def ticket(ticket_id: str, request: Request):
    user_id = request.state.user["id"]
    role = request.state.user["role"]

    if role == "customer":
        ticket_data = await get_customer_ticket(ticket_id, user_id)
        if ticket_data is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        return CustomerTicketDetailResponse(**ticket_data)

    if role == "agent":
        ticket_data = await get_agent_ticket(ticket_id, user_id)
        if ticket_data is None:
            raise HTTPException(status_code=404, detail="工单不存在")

        if ticket_data.get("assigned_agent_id") == user_id:
            return AgentTicketDetailResponse(**ticket_data)
        return AgentTicketSummaryResponse(
            ticket_id=ticket_data["ticket_id"],
            urgency=ticket_data["urgency"],
            status=ticket_data["status"],
            created_at=ticket_data["created_at"],
            issue_summary=ticket_data.get("issue_summary", ""),
        )

    raise HTTPException(status_code=403, detail="当前帐号无权查询工单")


@ticket_router.get(
    "/tickets/{ticket_id}/escalation",
    response_model=AgentTicketEscalationResponse,
)
async def ticket_escalation(ticket_id: str, request: Request) -> AgentTicketEscalationResponse:
    """返回客服队列中工单的通知投递状态，不返回供应商响应正文。"""
    user = request.state.user
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="只有客服可以查询通知状态")

    escalation = await get_agent_ticket_escalation(ticket_id, user["id"])
    if escalation is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return AgentTicketEscalationResponse(**escalation)


@ticket_router.post("/tickets/{ticket_id}/claim", response_model=ClaimResponse)
async def claim(ticket_id: str, request: Request):
    user = request.state.user
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="只有客服可以认领工单")

    result = await claim_ticket(ticket_id, user["id"])
    if result is None:
        raise HTTPException(status_code=409, detail="工单不存在或已被其他客服认领")
    return ClaimResponse(**result)


@ticket_router.post("/tickets/{ticket_id}/close", response_model=TicketCloseResponse)
async def close_ticket(ticket_id: str, request: Request) -> TicketCloseResponse:
    """客户确认 AI 已解决，或已认领客服完成处理后，明确关闭工单。"""
    user = request.state.user
    if user["role"] == "customer":
        closed = await close_customer_ticket(ticket_id, user["id"])
    elif user["role"] == "agent":
        closed = await close_agent_ticket(ticket_id, user["id"])
    else:
        raise HTTPException(status_code=403, detail="当前帐号无权关闭工单")

    if not closed:
        # 统一不暴露不存在、越权或不处于可关闭状态的差异。
        raise HTTPException(status_code=404, detail="工单不存在")
    support_case_service = getattr(request.app.state, "support_case_service", None)
    if isinstance(support_case_service, SupportCaseService):
        try:
            await support_case_service.complete_for_ticket(
                ticket_id,
                outcome={"completion": "ticket_closed", "ticket_id": ticket_id},
            )
        except Exception:
            # 工单已经成功关闭；Case 同步失败不能伪装成关闭失败，交给后续巡检修复。
            logging.getLogger(__name__).exception("support case completion sync failed after ticket close")
    return TicketCloseResponse()


@ticket_router.patch("/tickets/{ticket_id}")
async def update(
    ticket_id: str,
    request: Request,
    update_req: TicketUpdateRequest,
):
    user_id = request.state.user["id"]
    role = request.state.user["role"]
    update_dict = {"status": update_req.status, "urgency": update_req.urgency}

    if role == "customer":
        success = await update_customer_ticket(ticket_id, user_id, **update_dict)
    elif role == "agent":
        success = await update_agent_ticket(ticket_id, user_id, **update_dict)
    else:
        raise HTTPException(status_code=403, detail="当前帐号无权修改工单")

    if not success:
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"ok": True}
