"""客户订单只读接口；历史未归属订单不会从这里暴露。"""

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from store.order_store import find_orders, list_customer_orders

order_router = APIRouter(prefix="/api/v1/orders", tags=["我的订单"])


class OrderItem(BaseModel):
    product_name: str
    brand: str | None
    price: float
    quantity: int | None


class TrackingInfo(BaseModel):
    company: str | None
    number: str | None


class CustomerOrder(BaseModel):
    order_id: str
    status: str | None
    tracking: TrackingInfo
    total_amount: float
    paid_amount: float
    payment_method: str | None
    order_date: str
    delivered_at: str | None
    items: list[OrderItem]


class CustomerOrderListResponse(BaseModel):
    orders: list[CustomerOrder]
    total: int


@order_router.get("/my", response_model=CustomerOrderListResponse)
async def my_orders(request: Request, limit: int = Query(default=30, ge=1, le=100)) -> CustomerOrderListResponse:
    """列出当前登录客户已归属的订单，永不读取历史 UNMATCHED 订单。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查看我的订单")
    orders = await list_customer_orders(user["id"], limit)
    return CustomerOrderListResponse(orders=[CustomerOrder(**order) for order in orders], total=len(orders))


@order_router.get("/my/{order_id}", response_model=CustomerOrder)
async def my_order(order_id: str, request: Request) -> CustomerOrder:
    """读取一张属于当前客户的订单详情；越权和不存在统一为 404。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查看我的订单")
    orders = await find_orders(user["id"], order_id=order_id)
    if not orders:
        raise HTTPException(status_code=404, detail="订单不存在")
    return CustomerOrder(**orders[0])
