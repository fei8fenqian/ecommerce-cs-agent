"""购物车领域服务：商品事实在结算前实时读取，不把价格存入购物车。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from infra.alipay_sandbox import AlipaySandboxClient
from service.checkout_service import CheckoutSession, build_alipay_checkout_session
from store.cart_store import find_cart_item, list_cart_items, put_cart_item, remove_cart_item
from store.checkout_store import (
    CartCheckoutLine,
    CheckoutCategory,
    CheckoutLine,
    CheckoutProduct,
    create_checkout_order_from_lines,
    get_checkout_product,
    get_customer_latest_pending_checkout,
)


class CartUnavailableError(ValueError):
    """购物车条目不存在、商品下架或库存不足。"""


@dataclass(frozen=True)
class CartItemView:
    """客户购物车页面需要的当前商品事实。"""

    item_id: int
    category: CheckoutCategory
    product_id: str
    product_name: str
    brand: str
    price: float | None
    stock: int
    quantity: int
    available: bool


async def add_cart_item(
    customer_user_id: int,
    category: CheckoutCategory,
    product_id: str,
    quantity: int,
) -> CartItemView:
    """将一件可售商品加入购物车；同商品再次加入会累加数量。"""
    product = await get_checkout_product(category, product_id)
    if product is None or product.stock < 1:
        raise CartUnavailableError("product unavailable")
    existing = await find_cart_item(customer_user_id, category, product_id)
    next_quantity = quantity + (existing.quantity if existing else 0)
    if next_quantity < 1 or next_quantity > 5 or next_quantity > product.stock:
        raise CartUnavailableError("quantity unavailable")
    item_id = await put_cart_item(customer_user_id, category, product_id, next_quantity)
    return _to_view(item_id, product, next_quantity)


async def update_cart_item(customer_user_id: int, item_id: int, quantity: int) -> CartItemView:
    """按用户指定数量更新一项购物车条目，并重新校验库存。"""
    stored = next((item for item in await list_cart_items(customer_user_id) if item.item_id == item_id), None)
    if stored is None:
        raise CartUnavailableError("item unavailable")
    product = await get_checkout_product(stored.category, stored.product_id)
    if product is None or quantity < 1 or quantity > 5 or quantity > product.stock:
        raise CartUnavailableError("quantity unavailable")
    item_id = await put_cart_item(customer_user_id, stored.category, stored.product_id, quantity)
    return _to_view(item_id, product, quantity)


async def get_customer_cart(customer_user_id: int) -> list[CartItemView]:
    """读取购物车并用最新目录事实渲染价格、库存和下架状态。"""
    views: list[CartItemView] = []
    for stored in await list_cart_items(customer_user_id):
        product = await get_checkout_product(stored.category, stored.product_id)
        if product is None:
            views.append(
                CartItemView(
                    item_id=stored.item_id,
                    category=stored.category,
                    product_id=stored.product_id,
                    product_name="商品已下架",
                    brand="",
                    price=None,
                    stock=0,
                    quantity=stored.quantity,
                    available=False,
                )
            )
        else:
            views.append(_to_view(stored.item_id, product, stored.quantity))
    return views


async def delete_cart_item(customer_user_id: int, item_id: int) -> bool:
    """删除客户自己的购物车条目。"""
    return await remove_cart_item(customer_user_id, item_id)


async def create_cart_checkout_session(customer_user_id: int, return_origin: str | None) -> CheckoutSession:
    """将当前购物车创建为一笔待支付订单，并返回支付宝沙箱跳转地址。

    同一客户已有待支付订单时优先复用，避免双击或跳转失败反复生成废单。
    """
    alipay_client = AlipaySandboxClient.from_settings()
    existing = await get_customer_latest_pending_checkout(customer_user_id)
    if existing is not None:
        return await build_alipay_checkout_session(
            alipay_client,
            order_no=existing.order_no,
            merchant_payment_no=existing.merchant_payment_no,
            amount_cents=existing.amount_cents,
            subject=existing.subject,
            return_origin=return_origin,
        )

    stored_items = await list_cart_items(customer_user_id)
    if not stored_items:
        raise CartUnavailableError("cart empty")
    lines: list[CheckoutLine] = []
    for stored in stored_items:
        product = await get_checkout_product(stored.category, stored.product_id)
        if product is None or product.stock < stored.quantity:
            raise CartUnavailableError("cart item unavailable")
        lines.append(CheckoutLine(product=product, quantity=stored.quantity))

    now = datetime.now(UTC)
    suffix = uuid4().hex[:12].upper()
    order_no = f"SO{now:%Y%m%d%H%M%S}{suffix}"
    merchant_payment_no = f"PM{now:%Y%m%d%H%M%S}{suffix}"
    total_amount_cents = sum(line.product.unit_amount_cents * line.quantity for line in lines)
    total_quantity = sum(line.quantity for line in lines)
    subject = f"Geex Digital 商品订单（{total_quantity} 件）"
    await create_checkout_order_from_lines(
        sales_order_id=uuid4(),
        order_no=order_no,
        payment_id=uuid4(),
        merchant_payment_no=merchant_payment_no,
        customer_user_id=customer_user_id,
        lines=lines,
        cart_lines=[
            CartCheckoutLine(
                category=item.category,
                product_id=item.product_id,
                quantity=item.quantity,
            )
            for item in stored_items
        ],
    )
    return await build_alipay_checkout_session(
        alipay_client,
        order_no=order_no,
        merchant_payment_no=merchant_payment_no,
        amount_cents=total_amount_cents,
        subject=subject,
        return_origin=return_origin,
    )


def _to_view(item_id: int, product: CheckoutProduct, quantity: int) -> CartItemView:
    """将结算用的整数分商品事实转为前端展示对象。"""
    return CartItemView(
        item_id=item_id,
        category=product.category,
        product_id=product.product_id,
        product_name=product.product_name,
        brand=product.brand,
        price=product.unit_amount_cents / 100,
        stock=product.stock,
        quantity=quantity,
        available=product.stock >= quantity,
    )
