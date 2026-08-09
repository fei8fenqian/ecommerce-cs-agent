from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from store.ticket_store import get_ticket, list_tickets, update_ticket


class TicketItem(BaseModel):
    ticket_id: str
    customer_name: str
    urgency: str  # "low" | "medium" | "high" | "urgent"
    status: str  # "open" | "processing" | "closed"
    created_at: str


class TicketListResponse(BaseModel):
    tickets: list[TicketItem]
    total: int


class TicketDetailResponse(BaseModel):
    ticket_id: str
    customer_name: str
    phone: str
    issue: str
    urgency: str
    status: str
    created_at: str


class TicketUpdateRequest(BaseModel):
    status: str | None = None
    urgency: str | None = None


ticket_router = APIRouter(prefix="/api/v1", tags=["工单"])


@ticket_router.get("/tickets", response_model=TicketListResponse)
async def tickets(status: str | None = None):
    tickets = await list_tickets(status)
    ticket_list: list[TicketItem] = []
    for ticket in tickets:
        ticket_list.append(
            TicketItem(
                ticket_id=ticket.get("ticket_id"),
                customer_name=ticket.get("customer_name"),
                urgency=ticket.get("urgency"),
                status=ticket.get("status"),
                created_at=ticket.get("created_at"),
            )
        )
    return TicketListResponse(tickets=ticket_list, total=len(ticket_list))


@ticket_router.get("/tickets/{ticket_id}", response_model=TicketDetailResponse)
async def ticket(ticket_id: str):
    ticket_data = await get_ticket(ticket_id)
    if ticket_data is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return TicketDetailResponse(
        ticket_id=ticket_data.get("ticket_id"),
        customer_name=ticket_data.get("customer_name"),
        phone=ticket_data.get("phone"),
        issue=ticket_data.get("issue"),
        urgency=ticket_data.get("urgency"),
        status=ticket_data.get("status"),
        created_at=ticket_data.get("created_at"),
    )


@ticket_router.patch("/tickets/{ticket_id}")
async def update(ticket_id: str, update_req: TicketUpdateRequest):
    """修改工单字段"""
    update_dict: dict = {"status": update_req.status, "urgency": update_req.urgency}
    success: bool = await update_ticket(ticket_id, **update_dict)
    if not success:
        raise HTTPException(status_code=404, detail="工单不存在")
    return {"ok": True}
