"""客户发起支付宝沙箱 checkout 的 API 边界。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from service.checkout_service import (
    CheckoutCancellationUnavailableError,
    CheckoutUnavailableError,
    PaymentNotCreatedError,
    PaymentStatusUnavailableError,
    cancel_checkout_session,
    create_checkout_session,
    refresh_customer_payment_status,
    resume_checkout_session,
)
from store.checkout_store import list_customer_checkout_orders

checkout_router = APIRouter(prefix="/api/v1/checkout", tags=["沙箱结算"])


class CreateCheckoutRequest(BaseModel):
    """从已展示商品创建一笔单商品沙箱订单。"""

    category: Literal["laptops", "phones"]
    product_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1, le=5)
    return_origin: str | None = Field(default=None, max_length=200)


class ResumeCheckoutRequest(BaseModel):
    """重新打开一笔待付款订单的支付宝付款页。"""

    return_origin: str | None = Field(default=None, max_length=200)


class CheckoutSessionResponse(BaseModel):
    """浏览器跳转支付宝沙箱所需的数据。"""

    order_no: str
    amount_cents: int
    payment_url: str


class CheckoutOrderItem(BaseModel):
    """客户订单页展示的应用自有结算订单。"""

    order_no: str
    status: str
    total_amount_cents: int
    product_name: str
    quantity: int
    payment_status: str
    fulfillment_status: str | None
    tracking_company: str | None
    tracking_number: str | None
    created_at: str


class CheckoutOrderListResponse(BaseModel):
    """当前客户的新结算订单列表。"""

    orders: list[CheckoutOrderItem]


class CancelCheckoutResponse(BaseModel):
    """取消待支付订单的明确结果。"""

    order_no: str
    cancelled: bool


@checkout_router.post("/orders", response_model=CheckoutSessionResponse, status_code=201)
async def create_order(body: CreateCheckoutRequest, request: Request) -> CheckoutSessionResponse:
    """客户确认购买后创建待支付订单；不由 Agent 文本直接触发。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以创建支付订单")
    try:
        session = await create_checkout_session(
            customer_user_id=int(user["id"]),
            category=body.category,
            product_id=body.product_id,
            quantity=body.quantity,
            return_origin=body.return_origin,
        )
    except CheckoutUnavailableError as exc:
        raise HTTPException(status_code=409, detail="商品暂不可购买") from exc
    return CheckoutSessionResponse(**session.__dict__)


@checkout_router.post("/orders/{order_no}/resume-payment", response_model=CheckoutSessionResponse)
async def resume_payment(
    order_no: str,
    body: ResumeCheckoutRequest,
    request: Request,
) -> CheckoutSessionResponse:
    """让客户继续支付自己的待支付订单，不重复写订单或支付交易。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以继续支付订单")
    try:
        session = await resume_checkout_session(
            customer_user_id=int(user["id"]), order_no=order_no, return_origin=body.return_origin
        )
    except CheckoutUnavailableError as exc:
        raise HTTPException(status_code=409, detail="该订单当前不能继续付款") from exc
    return CheckoutSessionResponse(**session.__dict__)


@checkout_router.post("/orders/{order_no}/cancel", response_model=CancelCheckoutResponse)
async def cancel_order(order_no: str, request: Request) -> CancelCheckoutResponse:
    """关闭客户本人的待支付支付宝交易并取消本地订单。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以取消支付订单")
    try:
        await cancel_checkout_session(customer_user_id=int(user["id"]), order_no=order_no)
    except CheckoutCancellationUnavailableError as exc:
        raise HTTPException(status_code=409, detail="该订单当前不能取消") from exc
    except PaymentStatusUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝暂时无法关闭交易") from exc
    return CancelCheckoutResponse(order_no=order_no, cancelled=True)


@checkout_router.get("/orders/my", response_model=CheckoutOrderListResponse)
async def my_checkout_orders(request: Request) -> CheckoutOrderListResponse:
    """读取当前客户的新结算订单及支付状态。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查看支付订单")
    orders = await list_customer_checkout_orders(int(user["id"]))
    return CheckoutOrderListResponse(orders=[CheckoutOrderItem(**order.__dict__) for order in orders])


@checkout_router.post("/orders/{order_no}/refresh-payment", response_model=CheckoutOrderItem)
async def refresh_payment(order_no: str, request: Request) -> CheckoutOrderItem:
    """从支付宝查询当前客户订单的支付状态；不信任浏览器回跳参数。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查询支付订单")
    try:
        await refresh_customer_payment_status(customer_user_id=int(user["id"]), order_no=order_no)
    except PaymentNotCreatedError as exc:
        raise HTTPException(status_code=409, detail="该订单未在支付宝侧创建交易，请重新下单") from exc
    except PaymentStatusUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝暂时无法确认支付状态") from exc
    orders = await list_customer_checkout_orders(int(user["id"]))
    order = next((item for item in orders if item.order_no == order_no), None)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不可用或无法核验")
    return CheckoutOrderItem(**order.__dict__)
