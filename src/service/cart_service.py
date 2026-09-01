"""购物车领域服务：商品事实在结算前实时读取，不把价格存入购物车。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from infra.alipay_sandbox import AlipaySandboxClient
from infra.unionpay_test import UNIONPAY_TIMEZONE, UnionPayTestClient
from service.checkout_service import CheckoutSession, _build_payment_checkout_session
from store.cart_store import find_cart_item, list_cart_items, put_cart_item, remove_cart_item
from store.checkout_store import (
    CURRENT_PAYMENT_NO_PREFIX,
    CartCheckoutLine,
    CheckoutCategory,
    CheckoutLine,
    CheckoutProduct,
    PaymentProviderName,
    create_checkout_order_from_lines,
    find_reusable_pending_cart_checkout,
    get_checkout_product,
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


async def create_cart_checkout_session(
    customer_user_id: int,
    return_origin: str | None,
    payment_provider: PaymentProviderName = "alipay_sandbox",
) -> CheckoutSession:
    """将当前购物车创建为一笔待支付订单，并返回选定支付渠道的表单。

    只有当前购物车与旧订单快照完全一致时才复用，避免把旧金额带到新购物车。
    ``return_origin`` 用于支付宝付款完成后的受控浏览器回跳。
    """
    if payment_provider not in {"alipay_sandbox", "unionpay_test"}:
        raise CartUnavailableError("unsupported payment provider")
    alipay_client = AlipaySandboxClient.from_settings() if payment_provider == "alipay_sandbox" else None
    unionpay_client = UnionPayTestClient.from_settings() if payment_provider == "unionpay_test" else None
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
    merchant_payment_no = f"{CURRENT_PAYMENT_NO_PREFIX}{now:%Y%m%d%H%M%S}{suffix}"
    total_amount_cents = sum(line.product.unit_amount_cents * line.quantity for line in lines)
    total_quantity = sum(line.quantity for line in lines)
    subject = f"Geex Digital 商品订单（{total_quantity} 件）"
    cart_lines = [
        CartCheckoutLine(
            category=item.category,
            product_id=item.product_id,
            quantity=item.quantity,
            unit_amount_cents=next(
                line.product.unit_amount_cents
                for line in lines
                if line.product.category == item.category and line.product.product_id == item.product_id
            ),
        )
        for item in stored_items
    ]
    existing = await find_reusable_pending_cart_checkout(
        customer_user_id,
        cart_lines,
        total_amount_cents,
        payment_provider,
    )
    if existing is not None:
        return await _build_payment_checkout_session(
            payment_provider=existing.provider,
            order_no=existing.order_no,
            merchant_payment_no=existing.merchant_payment_no,
            amount_cents=existing.amount_cents,
            subject=existing.subject,
            provider_txn_time=existing.provider_txn_time,
            return_origin=return_origin,
            alipay_client=alipay_client,
            unionpay_client=unionpay_client,
        )

    provider_txn_time = (
        datetime.now(UNIONPAY_TIMEZONE).strftime("%Y%m%d%H%M%S") if payment_provider == "unionpay_test" else None
    )
    await create_checkout_order_from_lines(
        sales_order_id=uuid4(),
        order_no=order_no,
        payment_id=uuid4(),
        merchant_payment_no=merchant_payment_no,
        customer_user_id=customer_user_id,
        lines=lines,
        cart_lines=cart_lines,
        provider=payment_provider,
        provider_txn_time=provider_txn_time,
    )
    return await _build_payment_checkout_session(
        payment_provider=payment_provider,
        order_no=order_no,
        merchant_payment_no=merchant_payment_no,
        amount_cents=total_amount_cents,
        subject=subject,
        provider_txn_time=provider_txn_time,
        return_origin=return_origin,
        alipay_client=alipay_client,
        unionpay_client=unionpay_client,
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
