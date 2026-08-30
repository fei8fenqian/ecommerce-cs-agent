"""客户购物车 API；结算仍由 checkout 应用服务处理。"""

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from service.cart_service import (
    CartUnavailableError,
    add_cart_item,
    create_cart_checkout_session,
    delete_cart_item,
    get_customer_cart,
    update_cart_item,
)

cart_router = APIRouter(prefix="/api/v1/cart", tags=["购物车"])


class AddCartItemRequest(BaseModel):
    """向客户购物车增加一个目录商品。"""

    category: Literal["laptops", "phones", "components"]
    product_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1, le=5)


class UpdateCartItemRequest(BaseModel):
    """覆盖一项购物车商品的数量。"""

    quantity: int = Field(ge=1, le=5)


class CartItemResponse(BaseModel):
    """购物车页面可公开展示的最新商品信息。"""

    item_id: int
    category: str
    product_id: str
    product_name: str
    brand: str
    price: float | None
    stock: int
    quantity: int
    available: bool


class CartResponse(BaseModel):
    """当前客户的一辆购物车。"""

    items: list[CartItemResponse]


class CartCheckoutRequest(BaseModel):
    """从购物车发起结算时允许的浏览器回跳来源。"""

    return_origin: str | None = Field(default=None, max_length=200)


class CartCheckoutResponse(BaseModel):
    """购物车支付宝沙箱二维码付款所需的订单信息。"""

    order_no: str
    amount_cents: int
    payment_url: str
    payment_form_action: str | None = None
    payment_form_fields: dict[str, str] | None = None
    payment_qr_code: str | None = None


def _customer_id(request: Request) -> int:
    """取得受认证中间件保护的客户身份，拒绝内部角色借用客户购物车。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以使用购物车")
    return int(user["id"])


@cart_router.get("", response_model=CartResponse)
async def get_cart(request: Request) -> CartResponse:
    """读取当前客户的购物车，不创建订单。"""
    items = await get_customer_cart(_customer_id(request))
    return CartResponse(items=[CartItemResponse(**item.__dict__) for item in items])


@cart_router.post("/items", response_model=CartItemResponse)
async def add_item(body: AddCartItemRequest, request: Request) -> CartItemResponse:
    """增加商品数量，并依据当前库存拒绝无效条目。"""
    try:
        item = await add_cart_item(_customer_id(request), body.category, body.product_id, body.quantity)
    except CartUnavailableError as exc:
        raise HTTPException(status_code=409, detail="商品暂不可加入购物车") from exc
    return CartItemResponse(**item.__dict__)


@cart_router.patch("/items/{item_id}", response_model=CartItemResponse)
async def update_item(item_id: int, body: UpdateCartItemRequest, request: Request) -> CartItemResponse:
    """更新客户自己的购物车数量。"""
    try:
        item = await update_cart_item(_customer_id(request), item_id, body.quantity)
    except CartUnavailableError as exc:
        raise HTTPException(status_code=404, detail="购物车商品不可用或无法核验") from exc
    return CartItemResponse(**item.__dict__)


@cart_router.delete("/items/{item_id}", response_model=CartResponse)
async def delete_item(item_id: int, request: Request) -> CartResponse:
    """删除客户自己的购物车条目，并返回剩余购物车。"""
    customer_user_id = _customer_id(request)
    await delete_cart_item(customer_user_id, item_id)
    return CartResponse(items=[CartItemResponse(**item.__dict__) for item in await get_customer_cart(customer_user_id)])


@cart_router.post("/checkout", response_model=CartCheckoutResponse)
async def checkout_cart(body: CartCheckoutRequest, request: Request) -> CartCheckoutResponse:
    """以当前购物车创建或复用一笔待支付订单，再让浏览器跳转收银台。"""
    try:
        session = await create_cart_checkout_session(_customer_id(request), body.return_origin)
    except CartUnavailableError as exc:
        raise HTTPException(status_code=409, detail="购物车为空或有商品暂不可结算") from exc
    return CartCheckoutResponse(**session.__dict__)
