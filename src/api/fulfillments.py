"""运营履约接口：用于演示内部发货事件，不调用真实物流平台。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from service.fulfillment_service import FulfillmentUnavailableError, ShipOrderCommand, ship_order
from store.checkout_store import OperatorFulfillment, list_operator_fulfillments

fulfillment_router = APIRouter(prefix="/api/v1/fulfillments", tags=["运营履约"])


class FulfillmentItem(BaseModel):
    """运营和页面展示的履约摘要。"""

    order_no: str
    product_name: str
    quantity: int
    status: str
    carrier: str | None
    tracking_number: str | None
    created_at: str


class FulfillmentListResponse(BaseModel):
    """应用订单履约队列。"""

    fulfillments: list[FulfillmentItem]


class ShipOrderRequest(BaseModel):
    """运营登记发货时允许提交的固定字段。"""

    carrier: str = Field(min_length=1, max_length=64)
    tracking_number: str = Field(min_length=4, max_length=128, pattern=r"^[A-Za-z0-9-]+$")


def _as_item(value: OperatorFulfillment) -> FulfillmentItem:
    """转换 Store DTO，避免 API 直接依赖数据库记录。"""
    return FulfillmentItem(**value.__dict__)


@fulfillment_router.get("", response_model=FulfillmentListResponse)
async def fulfillments(request: Request) -> FulfillmentListResponse:
    """运营读取当前应用订单的待发货和已发货队列。"""
    if request.state.user["role"] != "operator":
        raise HTTPException(status_code=403, detail="只有运营可以查看履约队列")
    values = await list_operator_fulfillments()
    return FulfillmentListResponse(fulfillments=[_as_item(value) for value in values])


@fulfillment_router.post("/{order_no}/ship", response_model=FulfillmentItem)
async def ship(order_no: str, body: ShipOrderRequest, request: Request) -> FulfillmentItem:
    """运营登记物流单号，将待发货订单推进为已发货。"""
    if request.state.user["role"] != "operator":
        raise HTTPException(status_code=403, detail="只有运营可以登记发货")
    try:
        result = await ship_order(
            ShipOrderCommand(order_no=order_no, carrier=body.carrier.strip(), tracking_number=body.tracking_number)
        )
    except FulfillmentUnavailableError as exc:
        raise HTTPException(status_code=409, detail="订单当前不能登记发货") from exc
    return _as_item(result)
