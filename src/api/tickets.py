from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from store.ticket_store import (
    claim_ticket,
    get_agent_ticket,
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


ticket_router = APIRouter(prefix="/api/v1", tags=["工单"])


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
        )

    raise HTTPException(status_code=403, detail="当前帐号无权查询工单")


@ticket_router.post("/tickets/{ticket_id}/claim", response_model=ClaimResponse)
async def claim(ticket_id: str, request: Request):
    user = request.state.user
    if user["role"] != "agent":
        raise HTTPException(status_code=403, detail="只有客服可以认领工单")

    result = await claim_ticket(ticket_id, user["id"])
    if result is None:
        raise HTTPException(status_code=409, detail="工单不存在或已被其他客服认领")
    return ClaimResponse(**result)


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
