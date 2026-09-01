"""客户发起 checkout 的确定性应用服务；模型不能直接调用支付网关。"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Mapping
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from config import settings
from exceptions import DependencyUnavailableError
from infra.alipay_sandbox import AlipayGatewayError, AlipaySandboxClient, AlipayTradeNotFoundError
from infra.unionpay_test import (
    UNIONPAY_TIMEZONE,
    UnionPayProtocolError,
    UnionPayTestClient,
    validate_order_id,
    validate_txn_time,
    verify_response_signature,
)
from store.checkout_store import (
    CURRENT_PAYMENT_NO_PREFIX,
    CheckoutCategory,
    CustomerPendingPayment,
    PaymentProviderName,
    UnionPayFrontReturnPayment,
    apply_alipay_callback,
    apply_alipay_trade_query,
    apply_verified_payment_success,
    cancel_customer_pending_checkout,
    create_checkout_order,
    find_reusable_pending_checkout,
    get_checkout_product,
    get_customer_pending_checkout,
    get_customer_pending_payment,
    get_unionpay_front_return_payment,
)

logger = logging.getLogger(__name__)


class CheckoutUnavailableError(ValueError):
    """商品不存在、已缺货或不适合创建支付订单。"""


@dataclass(frozen=True)
class CheckoutSession:
    """前端跳转受支持支付收银台前需要的最小订单信息。"""

    order_no: str
    amount_cents: int
    payment_url: str
    payment_form_action: str | None = None
    payment_form_fields: dict[str, str] | None = None
    payment_qr_code: str | None = None
    payment_provider: PaymentProviderName = "alipay_sandbox"


class AlipayCallbackRejectedError(ValueError):
    """支付宝回调验签、商户校验或订单金额校验失败。"""


class PaymentStatusUnavailableError(ValueError):
    """支付渠道暂时无法查询支付状态。"""


class PaymentNotCreatedError(ValueError):
    """该本地订单在支付渠道侧没有可查询的交易。"""


class CheckoutCancellationUnavailableError(ValueError):
    """订单已经不是客户可取消的待支付状态。"""


class UnionPayCancellationUnavailableError(CheckoutCancellationUnavailableError):
    """U1 尚未实现银联待支付订单的取消语义。"""


@dataclass(frozen=True)
class UnionPayFrontReturnOutcome:
    """前台回跳处理后的安全结果；支付成功只能来自 queryTrans。"""

    order_no: str | None
    payment_result: Literal["paid", "unknown"]


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
        payment_provider="alipay_sandbox",
    )


async def build_unionpay_checkout_session(
    client: UnionPayTestClient,
    *,
    order_no: str,
    merchant_payment_no: str,
    amount_cents: int,
    provider_txn_time: str | None,
    return_origin: str | None,
) -> CheckoutSession:
    """构造一笔使用已持久化 txnTime 的银联测试收银台会话。

    ``merchant_payment_no`` 和 ``provider_txn_time`` 都来自本地支付记录；该函数
    不生成新的支付单标识，也不接受客户端提供的银联协议字段。
    """
    if provider_txn_time is None:
        raise CheckoutUnavailableError("UnionPay payment transaction time is missing")
    form = client.build_front_payment_form(
        order_id=merchant_payment_no,
        txn_time=provider_txn_time,
        txn_amt=amount_cents,
        front_url=build_unionpay_front_return_url(),
    )
    return CheckoutSession(
        order_no=order_no,
        amount_cents=amount_cents,
        payment_url="",
        payment_form_action=form.action,
        payment_form_fields=form.fields,
        payment_provider="unionpay_test",
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
        payment_provider="alipay_sandbox",
    )


def build_browser_return_url(return_origin: str | None, order_no: str) -> str | None:
    """仅接受本地开发前端的固定 origin，避免客户端把支付回跳变成开放重定向。"""
    if return_origin not in {"http://127.0.0.1:5173", "http://localhost:5173"}:
        return None
    return f"{return_origin}/?page=orders&payment_return=1&checkout_order={order_no}"


def build_unionpay_front_return_url() -> str:
    """构造服务端接收银联前台回报的公网地址，不接受客户端 URL。"""
    base_url = str(settings.unionpay_public_base_url or "").strip().rstrip("/")
    try:
        parts = urlsplit(base_url)
        hostname = parts.hostname
        _port = parts.port
    except ValueError as exc:
        raise CheckoutUnavailableError("UnionPay public front return URL is not configured") from exc
    if (
        not base_url
        or parts.scheme not in {"http", "https"}
        or not parts.netloc
        or not hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise CheckoutUnavailableError("UnionPay public front return URL is not configured")
    return f"{base_url}/api/v1/payments/unionpay/front-return"


_CUSTOMER_RETURN_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173"})


def _configured_customer_return_origin() -> str:
    """从服务端配置选择现有 allowlist 中的商城 origin，拒绝开放重定向。"""
    configured = str(settings.unionpay_front_url or "").strip().rstrip("/")
    try:
        parts = urlsplit(configured)
    except ValueError:
        return "http://127.0.0.1:5173"
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""
    return origin if origin in _CUSTOMER_RETURN_ORIGINS else "http://127.0.0.1:5173"


def build_unionpay_customer_return_url(
    order_no: str | None,
    *,
    payment_result: Literal["paid", "unknown"],
) -> str:
    """生成固定商城 Orders 回跳；不使用银联报文或请求参数中的 URL。"""
    query: list[tuple[str, str]] = [("page", "orders"), ("payment_return", "1")]
    if order_no:
        query.append(("checkout_order", order_no))
    if payment_result == "unknown":
        query.append(("payment_result", "unknown"))
    return f"{_configured_customer_return_origin()}/?{urlencode(query)}"


def _validate_payment_provider(payment_provider: str) -> PaymentProviderName:
    if payment_provider not in {"alipay_sandbox", "unionpay_test"}:
        raise CheckoutUnavailableError("unsupported payment provider")
    return payment_provider  # type: ignore[return-value]


async def _build_payment_checkout_session(
    *,
    payment_provider: PaymentProviderName,
    order_no: str,
    merchant_payment_no: str,
    amount_cents: int,
    subject: str,
    provider_txn_time: str | None,
    return_origin: str | None,
    alipay_client: AlipaySandboxClient | None = None,
    unionpay_client: UnionPayTestClient | None = None,
) -> CheckoutSession:
    """按本地支付记录分发到一个已配置的支付表单构造器。"""
    if payment_provider == "alipay_sandbox":
        if alipay_client is None:
            alipay_client = AlipaySandboxClient.from_settings()
        return await build_alipay_checkout_session(
            alipay_client,
            order_no=order_no,
            merchant_payment_no=merchant_payment_no,
            amount_cents=amount_cents,
            subject=subject,
            return_origin=return_origin,
        )
    if payment_provider != "unionpay_test":
        raise CheckoutUnavailableError("unsupported payment provider")
    if unionpay_client is None:
        unionpay_client = UnionPayTestClient.from_settings()
    return await build_unionpay_checkout_session(
        unionpay_client,
        order_no=order_no,
        merchant_payment_no=merchant_payment_no,
        amount_cents=amount_cents,
        provider_txn_time=provider_txn_time,
        return_origin=return_origin,
    )


async def create_checkout_session(
    *,
    customer_user_id: int,
    category: CheckoutCategory,
    product_id: str,
    quantity: int,
    return_origin: str | None = None,
    payment_provider: PaymentProviderName = "alipay_sandbox",
) -> CheckoutSession:
    """读取商品事实、原子创建待支付订单，并构造选定支付渠道的跳转表单。

    Returns:
        只包含订单号、金额和跳转地址；状态仍为 PENDING_PAYMENT。

    Raises:
        CheckoutUnavailableError: 商品不存在、缺货或数量不合法。
    """
    if quantity < 1 or quantity > 5:
        raise CheckoutUnavailableError("invalid quantity")
    # 先验证沙箱配置，再创建本地订单；配置缺失不能留下无法付款的待支付单。
    payment_provider = _validate_payment_provider(payment_provider)
    alipay_client = AlipaySandboxClient.from_settings() if payment_provider == "alipay_sandbox" else None
    unionpay_client = UnionPayTestClient.from_settings() if payment_provider == "unionpay_test" else None
    product = await get_checkout_product(category, product_id)
    if product is None or product.stock < quantity or product.unit_amount_cents <= 0:
        raise CheckoutUnavailableError("product unavailable")

    reusable = await find_reusable_pending_checkout(customer_user_id, product, quantity, payment_provider)
    if reusable is not None:
        return await _build_payment_checkout_session(
            payment_provider=reusable.provider,
            order_no=reusable.order_no,
            merchant_payment_no=reusable.merchant_payment_no,
            amount_cents=reusable.amount_cents,
            subject=reusable.subject,
            provider_txn_time=reusable.provider_txn_time,
            return_origin=return_origin,
            alipay_client=alipay_client,
            unionpay_client=unionpay_client,
        )

    now = datetime.now(UTC)
    suffix = uuid4().hex[:12].upper()
    order_no = f"SO{now:%Y%m%d%H%M%S}{suffix}"
    merchant_payment_no = f"{CURRENT_PAYMENT_NO_PREFIX}{now:%Y%m%d%H%M%S}{suffix}"
    provider_txn_time = (
        datetime.now(UNIONPAY_TIMEZONE).strftime("%Y%m%d%H%M%S") if payment_provider == "unionpay_test" else None
    )
    created = await create_checkout_order(
        sales_order_id=uuid4(),
        order_no=order_no,
        payment_id=uuid4(),
        merchant_payment_no=merchant_payment_no,
        customer_user_id=customer_user_id,
        product=product,
        quantity=quantity,
        provider=payment_provider,
        provider_txn_time=provider_txn_time,
    )
    return await _build_payment_checkout_session(
        payment_provider=created.provider,
        order_no=created.order_no,
        merchant_payment_no=created.merchant_payment_no,
        amount_cents=created.total_amount_cents,
        subject=product.product_name,
        provider_txn_time=created.provider_txn_time,
        return_origin=return_origin,
        alipay_client=alipay_client,
        unionpay_client=unionpay_client,
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
    return await _build_payment_checkout_session(
        payment_provider=pending.provider,
        order_no=order_no,
        merchant_payment_no=pending.merchant_payment_no,
        amount_cents=pending.amount_cents,
        subject=pending.subject,
        provider_txn_time=pending.provider_txn_time,
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
    if pending.provider == "unionpay_test":
        raise UnionPayCancellationUnavailableError("UnionPay pending payment cancellation is not supported in U1")
    if pending.provider != "alipay_sandbox":
        raise CheckoutCancellationUnavailableError("payment provider cancellation is not supported")

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
    if not await cancel_customer_pending_checkout(customer_user_id, order_no, pending.provider):
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
    """按本地支付渠道查询客户本人的待支付订单，并收敛可信支付状态。

    浏览器回跳只负责带用户回订单页；真实状态以本次网关查询或异步通知为准。
    """
    pending = await get_customer_pending_payment(customer_user_id, order_no)
    if pending is None:
        return False
    if pending.provider == "unionpay_test":
        return await _refresh_unionpay_payment_status(pending)
    if pending.provider != "alipay_sandbox":
        raise PaymentStatusUnavailableError("支付渠道暂时无法确认支付状态")
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


async def _refresh_unionpay_payment_status(pending: CustomerPendingPayment) -> bool:
    """只将银联已验签、三元组和金额一致的 00/00 查询收敛为成功。"""
    merchant_payment_no = str(pending.merchant_payment_no)
    provider_txn_time = pending.provider_txn_time
    if not isinstance(provider_txn_time, str) or not provider_txn_time:
        raise PaymentStatusUnavailableError("支付渠道交易信息不完整")
    try:
        result = await UnionPayTestClient.from_settings().query_transaction(
            order_id=merchant_payment_no,
            txn_time=provider_txn_time,
        )
    except UnionPayProtocolError as exc:
        logger.warning("UnionPay payment status query unavailable", extra={"failure_type": type(exc).__name__})
        raise PaymentStatusUnavailableError("支付渠道暂时无法确认支付状态") from exc

    try:
        returned_amount_cents = int(result.txn_amt)
    except (TypeError, ValueError) as exc:
        raise PaymentStatusUnavailableError("支付渠道交易金额无法核验") from exc
    if (
        not result.signature_verified
        or result.order_id != merchant_payment_no
        or result.txn_time != provider_txn_time
        or returned_amount_cents != pending.amount_cents
    ):
        raise PaymentStatusUnavailableError("支付渠道交易信息不匹配")
    if result.resp_code != "00":
        raise PaymentStatusUnavailableError("支付渠道暂时无法确认支付状态")
    if result.orig_resp_code != "00":
        # 查询本身成功，但原交易仍不是已成功状态；不把未知结果写成失败。
        return False
    if not isinstance(result.query_id, str) or not result.query_id.strip():
        raise PaymentStatusUnavailableError("支付渠道交易号缺失")
    matched = await apply_verified_payment_success(
        provider="unionpay_test",
        merchant_payment_no=merchant_payment_no,
        provider_trade_no=result.query_id,
        amount_cents=pending.amount_cents,
    )
    if not matched:
        raise PaymentStatusUnavailableError("本地支付订单无法匹配")
    return True


async def process_unionpay_front_return(
    parameters: Mapping[str, str],
) -> UnionPayFrontReturnOutcome:
    """处理银联前台回报；只有后续已验真的 queryTrans 能推进本地支付状态。

    前台回报是 public、可被浏览器重放的非权威信号。此函数先验签并用银联商户
    交易号定位本地 ``unionpay_test`` 支付，再主动 queryTrans；任何不完整、失配
    或不可确认的情况都只返回安全的 ``unknown``，不改变数据库。
    """
    try:
        client = UnionPayTestClient.from_settings()
    except UnionPayProtocolError as exc:
        logger.warning("UnionPay front return configuration unavailable", extra={"failure_type": type(exc).__name__})
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")

    required_fields = ("signature", "signPubKeyCert", "merId", "orderId", "txnTime")
    if any(not parameters.get(field) for field in required_fields):
        logger.warning("UnionPay front return has no complete signed payload")
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")

    try:
        signature_verified = verify_response_signature(parameters, client.certificates)
    except UnionPayProtocolError as exc:
        logger.warning(
            "UnionPay front return signature rejected",
            extra={"failure_type": type(exc).__name__, "failure_reason": str(exc)},
        )
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")
    if signature_verified is not True:
        logger.warning("UnionPay front return signature was not verified")
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")

    merchant_payment_no = parameters["orderId"]
    txn_time = parameters["txnTime"]
    if parameters["merId"] != client.mer_id:
        logger.warning("UnionPay front return merchant mismatch")
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")
    try:
        validate_order_id(merchant_payment_no)
        validate_txn_time(txn_time)
    except UnionPayProtocolError as exc:
        logger.warning("UnionPay front return identity format rejected", extra={"failure_type": type(exc).__name__})
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")

    try:
        local_payment: UnionPayFrontReturnPayment | None = await get_unionpay_front_return_payment(merchant_payment_no)
    except Exception as exc:
        logger.warning("UnionPay front return local lookup unavailable", extra={"failure_type": type(exc).__name__})
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")
    if local_payment is None or local_payment.provider != "unionpay_test":
        logger.warning("UnionPay front return local payment was not matched")
        return UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")
    if (
        local_payment.merchant_payment_no != merchant_payment_no
        or local_payment.provider_txn_time != txn_time
        or local_payment.provider != "unionpay_test"
    ):
        logger.warning("UnionPay front return local transaction identity mismatch")
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")

    front_amount = parameters.get("txnAmt")
    if front_amount is not None:
        try:
            if int(front_amount) != local_payment.amount_cents:
                raise ValueError("amount mismatch")
        except (TypeError, ValueError):
            logger.warning("UnionPay front return amount mismatch")
            return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")

    try:
        query_result = await client.query_transaction(order_id=merchant_payment_no, txn_time=txn_time)
    except UnionPayProtocolError as exc:
        logger.warning("UnionPay front return query unavailable", extra={"failure_type": type(exc).__name__})
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")

    try:
        query_amount = int(query_result.txn_amt)
    except (TypeError, ValueError):
        logger.warning("UnionPay query amount could not be verified")
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")
    if (
        query_result.signature_verified is not True
        or query_result.order_id != merchant_payment_no
        or query_result.txn_time != txn_time
        or query_amount != local_payment.amount_cents
    ):
        logger.warning("UnionPay query transaction identity or amount mismatch")
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")
    if query_result.resp_code != "00" or query_result.orig_resp_code != "00" or not query_result.query_id.strip():
        logger.info("UnionPay query did not confirm payment success")
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")

    try:
        matched = await apply_verified_payment_success(
            provider="unionpay_test",
            merchant_payment_no=merchant_payment_no,
            provider_trade_no=query_result.query_id,
            amount_cents=local_payment.amount_cents,
        )
    except Exception as exc:
        logger.warning(
            "UnionPay front return success convergence unavailable",
            extra={"failure_type": type(exc).__name__},
        )
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")
    if not matched:
        logger.warning("UnionPay front return success convergence did not match local payment")
        return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="unknown")
    return UnionPayFrontReturnOutcome(order_no=local_payment.order_no, payment_result="paid")
