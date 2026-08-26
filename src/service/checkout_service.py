"""客户发起 checkout 的确定性应用服务；模型不能直接调用支付网关。"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping
from uuid import uuid4

from config import settings
from exceptions import DependencyUnavailableError
from infra.alipay_sandbox import AlipayGatewayError, AlipaySandboxClient, AlipayTradeNotFoundError
from store.checkout_store import (
    CURRENT_PAYMENT_NO_PREFIX,
    CheckoutCategory,
    apply_alipay_callback,
    apply_alipay_trade_query,
    cancel_customer_pending_checkout,
    create_checkout_order,
    find_reusable_pending_checkout,
    get_checkout_product,
    get_customer_pending_checkout,
    get_customer_pending_payment,
)

logger = logging.getLogger(__name__)


class CheckoutUnavailableError(ValueError):
    """商品不存在、已缺货或不适合创建支付订单。"""


@dataclass(frozen=True)
class CheckoutSession:
    """前端跳转支付宝前需要的最小订单信息。"""

    order_no: str
    amount_cents: int
    payment_url: str
    payment_form_action: str | None = None
    payment_form_fields: dict[str, str] | None = None
    payment_qr_code: str | None = None


class AlipayCallbackRejectedError(ValueError):
    """支付宝回调验签、商户校验或订单金额校验失败。"""


class PaymentStatusUnavailableError(ValueError):
    """支付宝暂时无法查询支付状态。"""


class PaymentNotCreatedError(ValueError):
    """该本地订单在支付宝侧没有可查询的交易。"""


class CheckoutCancellationUnavailableError(ValueError):
    """订单已经不是客户可取消的待支付状态。"""


async def build_alipay_checkout_session(
    client: AlipaySandboxClient,
    *,
    order_no: str,
    merchant_payment_no: str,
    amount_cents: int,
    subject: str,
    return_origin: str | None,
) -> CheckoutSession:
    """构造支付宝电脑网站支付会话。

    Args:
        client: 已完成配置校验的支付宝沙箱客户端。
        order_no: 本地商城订单号。
        merchant_payment_no: 支付宝可见的商户交易号。
        amount_cents: 服务端确定的订单总金额（分）。
        subject: 收银台展示的订单标题。
        return_origin: 允许的浏览器回跳来源。

    Returns:
        由服务端签名的 POST 表单参数；前端必须提交该表单进入支付宝收银台。
    """
    form = client.build_page_pay_form(
        merchant_payment_no=merchant_payment_no,
        amount_cents=amount_cents,
        subject=subject,
        return_url=build_browser_return_url(return_origin, order_no),
    )
    return CheckoutSession(
        order_no=order_no,
        amount_cents=amount_cents,
        payment_url=form.url,
        payment_form_action=form.action,
        payment_form_fields=form.fields,
    )


async def build_alipay_qr_checkout_session(
    client: AlipaySandboxClient,
    *,
    order_no: str,
    merchant_payment_no: str,
    amount_cents: int,
    subject: str,
) -> CheckoutSession:
    """为商城购物车创建支付宝二维码支付会话。

    Args:
        client: 已完成配置校验的支付宝沙箱客户端。
        order_no: 本地商城订单号。
        merchant_payment_no: 支付宝可见的商户交易号。
        amount_cents: 服务端确定的订单总金额（分）。
        subject: 收银台展示的订单标题。

    Returns:
        包含支付宝二维码内容的支付会话。二维码本身不代表支付成功，付款结果
        仍须通过交易查询或异步通知确认。

    Raises:
        DependencyUnavailableError: 支付宝无法创建二维码，不能把订单伪装成可支付。
    """
    try:
        qr_code = await client.precreate_trade(
            merchant_payment_no=merchant_payment_no,
            amount_cents=amount_cents,
            subject=subject,
        )
    except AlipayGatewayError as exc:
        raise DependencyUnavailableError("支付宝暂时无法创建付款二维码") from exc
    return CheckoutSession(
        order_no=order_no,
        amount_cents=amount_cents,
        payment_url="",
        payment_qr_code=qr_code,
    )


def build_browser_return_url(return_origin: str | None, order_no: str) -> str | None:
    """仅接受本地开发前端的固定 origin，避免客户端把支付回跳变成开放重定向。"""
    if return_origin not in {"http://127.0.0.1:5173", "http://localhost:5173"}:
        return None
    return f"{return_origin}/?page=orders&payment_return=1&checkout_order={order_no}"


async def create_checkout_session(
    *,
    customer_user_id: int,
    category: CheckoutCategory,
    product_id: str,
    quantity: int,
    return_origin: str | None = None,
) -> CheckoutSession:
    """读取商品事实、原子创建待支付订单，并构造支付宝沙箱跳转地址。

    Returns:
        只包含订单号、金额和跳转地址；状态仍为 PENDING_PAYMENT。

    Raises:
        CheckoutUnavailableError: 商品不存在、缺货或数量不合法。
    """
    if quantity < 1 or quantity > 5:
        raise CheckoutUnavailableError("invalid quantity")
    # 先验证沙箱配置，再创建本地订单；配置缺失不能留下无法付款的待支付单。
    alipay_client = AlipaySandboxClient.from_settings()
    product = await get_checkout_product(category, product_id)
    if product is None or product.stock < quantity or product.unit_amount_cents <= 0:
        raise CheckoutUnavailableError("product unavailable")

    reusable = await find_reusable_pending_checkout(customer_user_id, product, quantity)
    if reusable is not None:
        return await build_alipay_checkout_session(
            alipay_client,
            order_no=reusable.order_no,
            merchant_payment_no=reusable.merchant_payment_no,
            amount_cents=reusable.amount_cents,
            subject=reusable.subject,
            return_origin=return_origin,
        )

    now = datetime.now(UTC)
    suffix = uuid4().hex[:12].upper()
    order_no = f"SO{now:%Y%m%d%H%M%S}{suffix}"
    merchant_payment_no = f"{CURRENT_PAYMENT_NO_PREFIX}{now:%Y%m%d%H%M%S}{suffix}"
    created = await create_checkout_order(
        sales_order_id=uuid4(),
        order_no=order_no,
        payment_id=uuid4(),
        merchant_payment_no=merchant_payment_no,
        customer_user_id=customer_user_id,
        product=product,
        quantity=quantity,
    )
    return await build_alipay_checkout_session(
        alipay_client,
        order_no=created.order_no,
        merchant_payment_no=created.merchant_payment_no,
        amount_cents=created.total_amount_cents,
        subject=product.product_name,
        return_origin=return_origin,
    )


async def resume_checkout_session(
    *,
    customer_user_id: int,
    order_no: str,
    return_origin: str | None,
) -> CheckoutSession:
    """为客户本人的现有待支付订单重新生成付款入口，不再创建第二笔本地订单。

    新旧订单统一重新生成电脑网站支付表单，不创建第二笔本地订单。
    """
    pending = await get_customer_pending_payment(customer_user_id, order_no)
    if pending is None:
        raise CheckoutUnavailableError("payment is not resumable")
    return await build_alipay_checkout_session(
        AlipaySandboxClient.from_settings(),
        order_no=order_no,
        merchant_payment_no=pending.merchant_payment_no,
        amount_cents=pending.amount_cents,
        subject=pending.subject,
        return_origin=return_origin,
    )


async def cancel_checkout_session(*, customer_user_id: int, order_no: str) -> None:
    """关闭支付宝待支付交易后，再原子取消本地订单。

    网关关闭成功或确认交易从未创建，才允许本地进入 CANCELLED，避免本地取消后
    仍可在支付宝侧付款。
    """
    pending = await get_customer_pending_checkout(customer_user_id, order_no)
    if pending is None:
        raise CheckoutCancellationUnavailableError("checkout is not cancellable")

    # 先主动收敛支付宝事实：支付成功或已被关闭时，不能再盲目调用关闭接口。
    # 这也修复了浏览器付款后异步通知缺失、本地仍显示 PENDING 的场景。
    try:
        if await refresh_customer_payment_status(customer_user_id=customer_user_id, order_no=order_no):
            raise CheckoutCancellationUnavailableError("checkout has been paid")
    except PaymentNotCreatedError:
        # 用户从未真正进入收银台时，支付宝无交易；本地待支付单可直接安全取消。
        pass

    pending = await get_customer_pending_checkout(customer_user_id, order_no)
    if pending is None:
        raise CheckoutCancellationUnavailableError("checkout state changed")
    try:
        await AlipaySandboxClient.from_settings().close_trade(pending.merchant_payment_no)
    except DependencyUnavailableError as exc:
        raise PaymentStatusUnavailableError("支付宝暂时无法关闭交易") from exc
    except AlipayGatewayError as exc:
        raise PaymentStatusUnavailableError("支付宝暂时无法关闭交易") from exc
    if not await cancel_customer_pending_checkout(customer_user_id, order_no):
        raise CheckoutCancellationUnavailableError("checkout state changed")


async def process_alipay_callback(parameters: Mapping[str, str]) -> bool:
    """验签并处理支付宝异步通知，绝不信任浏览器 return_url。

    Raises:
        AlipayCallbackRejectedError: 回调来源、商户、金额或本地订单不匹配。
    """
    client = AlipaySandboxClient.from_settings()
    try:
        client.verify_callback(parameters)
        if parameters.get("app_id") != settings.alipay_sandbox_app_id:
            raise AlipayCallbackRejectedError("unexpected app")
        if parameters.get("seller_id") != settings.alipay_sandbox_seller_id:
            raise AlipayCallbackRejectedError("unexpected seller")
        merchant_payment_no = parameters["out_trade_no"]
        provider_trade_no = parameters["trade_no"]
        provider_callback_id = parameters["notify_id"]
        amount_cents = int((Decimal(parameters["total_amount"]) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (KeyError, ValueError) as exc:
        raise AlipayCallbackRejectedError("invalid callback") from exc
    status = parameters.get("trade_status")
    if status not in {"TRADE_SUCCESS", "TRADE_CLOSED"}:
        return True
    matched = await apply_alipay_callback(
        merchant_payment_no=merchant_payment_no,
        provider_trade_no=provider_trade_no,
        provider_callback_id=provider_callback_id,
        amount_cents=amount_cents,
        succeeded=status == "TRADE_SUCCESS",
    )
    if not matched:
        raise AlipayCallbackRejectedError("unmatched callback")
    return True


async def refresh_customer_payment_status(*, customer_user_id: int, order_no: str) -> bool:
    """向支付宝查询客户本人的待支付订单，并收敛可信支付状态。

    浏览器回跳只负责带用户回订单页；真实状态以本次网关查询或异步通知为准。
    """
    pending = await get_customer_pending_payment(customer_user_id, order_no)
    if pending is None:
        return False
    try:
        result = await AlipaySandboxClient.from_settings().query_trade(pending.merchant_payment_no)
        returned_order_no = result.get("out_trade_no")
        returned_trade_no = result.get("trade_no")
        returned_status = result.get("trade_status")
        amount_cents = int((Decimal(str(result["total_amount"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        if (
            returned_order_no != pending.merchant_payment_no
            or not isinstance(returned_trade_no, str)
            or not isinstance(returned_status, str)
            or amount_cents != pending.amount_cents
        ):
            raise PaymentStatusUnavailableError("支付宝交易信息不匹配")
        matched = await apply_alipay_trade_query(
            merchant_payment_no=pending.merchant_payment_no,
            provider_trade_no=returned_trade_no,
            amount_cents=amount_cents,
            trade_status=returned_status,
        )
        if not matched:
            raise PaymentStatusUnavailableError("本地支付订单无法匹配")
        return returned_status == "TRADE_SUCCESS"
    except AlipayTradeNotFoundError as exc:
        raise PaymentNotCreatedError("该订单未在支付宝侧创建交易，请重新下单") from exc
    except DependencyUnavailableError as exc:
        logger.warning("Alipay payment status configuration unavailable")
        raise PaymentStatusUnavailableError("支付宝暂时无法确认支付状态") from exc
    except (AlipayGatewayError, KeyError, ValueError) as exc:
        logger.warning("Alipay payment status query unavailable", extra={"failure_type": type(exc).__name__})
        if isinstance(exc, PaymentStatusUnavailableError):
            raise
        raise PaymentStatusUnavailableError("支付宝暂时无法确认支付状态") from exc
