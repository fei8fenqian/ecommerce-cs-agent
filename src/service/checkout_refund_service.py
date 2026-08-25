"""新商城全额退款的确定性应用服务。

模型可以解释退款规则或引导客户进入订单页，但不能直接绕过本服务调用支付宝。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from exceptions import DependencyUnavailableError
from infra.alipay_sandbox import AlipayGatewayError, AlipayRefundRejectedError, AlipaySandboxClient
from store.checkout_refund_store import (
    CheckoutRefund,
    create_customer_refund_request,
    get_customer_checkout_refund,
    mark_checkout_refund_failed,
    mark_checkout_refund_succeeded,
    start_customer_refund_confirmation,
)


class RefundNotEligibleError(ValueError):
    """订单不属于当前客户，或不满足首版全额退款条件。"""


class RefundConfirmationUnavailableError(ValueError):
    """退款已经完成、已失败，或确认状态发生并发变化。"""


class RefundGatewayUnavailableError(ValueError):
    """支付宝结果未知，必须保留处理中状态而不是再次发起退款。"""


@dataclass(frozen=True)
class CustomerRefundResult:
    """提供给 API 的最小退款状态，不包含支付宝原始报文。"""

    refund_id: UUID
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    requested_at: str
    idempotent_replay: bool


def _to_result(refund: CheckoutRefund, *, idempotent_replay: bool) -> CustomerRefundResult:
    """把存储记录转换为稳定的服务返回对象。"""
    return CustomerRefundResult(
        refund_id=refund.refund_id,
        order_no=refund.order_no,
        status=refund.status,
        amount_cents=refund.amount_cents,
        currency=refund.currency,
        reason=refund.reason,
        requested_at=refund.requested_at,
        idempotent_replay=idempotent_replay,
    )


async def request_customer_refund(
    *,
    customer_user_id: int,
    order_no: str,
    reason: str,
    request_idempotency_key: str,
) -> CustomerRefundResult:
    """创建一笔待客户确认的全额退款，不触发任何外部资金动作。

    Args:
        customer_user_id: 当前已认证客户的内部用户 ID。
        order_no: 仅应用自有 checkout 订单号。
        reason: 客户填写的简短退款原因，只保存为售后事实，不写日志。
        request_idempotency_key: 浏览器为本次状态变更生成的稳定重放键。

    Returns:
        PENDING_CONFIRMATION 的退款摘要；同一个键重放会返回已创建的同一资源。

    Raises:
        RefundNotEligibleError: 订单未支付、已发货、超期、不属于客户或已存在退款。
    """
    now = datetime.now(UTC)
    suffix = uuid4().hex[:12].upper()
    merchant_refund_no = f"RF{now:%Y%m%d%H%M%S}{suffix}"
    refund = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=customer_user_id,
        order_no=order_no,
        merchant_refund_no=merchant_refund_no,
        request_idempotency_key=request_idempotency_key,
        reason=reason,
    )
    if refund is None:
        raise RefundNotEligibleError("checkout refund is not eligible")
    return _to_result(refund, idempotent_replay=refund.merchant_refund_no != merchant_refund_no)


async def confirm_customer_refund(
    *,
    customer_user_id: int,
    refund_id: UUID,
    confirmation_idempotency_key: str,
) -> CustomerRefundResult:
    """在客户明确确认后提交一次支付宝沙箱全额退款。

    网关超时或网络失败时不把退款改回可重试状态，因为支付宝可能已经受理；系统
    保持 PROCESSING，后续只能通过退款查询/对账收敛，避免二次退款。

    Raises:
        RefundConfirmationUnavailableError: 退款已不处于可确认状态。
        RefundGatewayUnavailableError: 网关结果未知，本地保持 PROCESSING。
    """
    started = await start_customer_refund_confirmation(
        customer_user_id=customer_user_id,
        refund_id=refund_id,
        confirmation_idempotency_key=confirmation_idempotency_key,
    )
    if started is None:
        raise RefundConfirmationUnavailableError("refund is not confirmable")
    if not started.should_submit_to_provider:
        return _to_result(started.refund, idempotent_replay=True)

    try:
        gateway_result = await AlipaySandboxClient.from_settings().refund_trade(
            merchant_payment_no=started.refund.merchant_payment_no,
            merchant_refund_no=started.refund.merchant_refund_no,
            amount_cents=started.refund.amount_cents,
        )
        returned_order_no = gateway_result.get("out_trade_no")
        refunded_cents = int(
            (Decimal(str(gateway_result["refund_fee"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if returned_order_no != started.refund.merchant_payment_no or refunded_cents != started.refund.amount_cents:
            raise RefundGatewayUnavailableError("支付宝退款结果无法匹配")
    except AlipayRefundRejectedError:
        failed = await mark_checkout_refund_failed(started.refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)
    except (DependencyUnavailableError, AlipayGatewayError, KeyError, ValueError) as exc:
        if isinstance(exc, RefundGatewayUnavailableError):
            raise
        raise RefundGatewayUnavailableError("支付宝退款结果暂时无法确认") from exc

    succeeded = await mark_checkout_refund_succeeded(
        refund_id=started.refund.refund_id,
        provider_refund_reference=str(gateway_result.get("trade_no") or "") or None,
    )
    if succeeded is None:
        raise RefundConfirmationUnavailableError("refund state changed")
    return _to_result(succeeded, idempotent_replay=False)


async def refresh_customer_refund_status(
    *,
    customer_user_id: int,
    refund_id: UUID,
) -> CustomerRefundResult:
    """主动查询支付宝，将结果未知的退款收敛为最终状态。

    只有 ``PROCESSING`` 会访问支付宝。已成功、失败或仍待客户确认的退款直接返回，
    因而浏览器重复点击刷新不会创建或重复提交退款。

    Args:
        customer_user_id: 当前已认证客户的内部用户 ID。
        refund_id: 客户只能查询自己的退款记录。

    Returns:
        当前退款状态；支付宝仍在处理中时保留 ``PROCESSING``。

    Raises:
        RefundConfirmationUnavailableError: 退款不属于当前客户或不存在。
        RefundGatewayUnavailableError: 支付宝无法可靠返回查询结果。
    """
    refund = await get_customer_checkout_refund(customer_user_id, refund_id)
    if refund is None:
        raise RefundConfirmationUnavailableError("refund is unavailable")
    if refund.status != "PROCESSING":
        return _to_result(refund, idempotent_replay=True)

    try:
        gateway_result = await AlipaySandboxClient.from_settings().query_refund(
            merchant_payment_no=refund.merchant_payment_no,
            merchant_refund_no=refund.merchant_refund_no,
        )
        returned_payment_no = gateway_result.get("out_trade_no")
        returned_refund_no = gateway_result.get("out_request_no")
        refunded_cents = int(
            (Decimal(str(gateway_result["refund_amount"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if (
            returned_payment_no != refund.merchant_payment_no
            or returned_refund_no != refund.merchant_refund_no
            or refunded_cents != refund.amount_cents
        ):
            raise RefundGatewayUnavailableError("支付宝退款查询结果无法匹配")
    except (DependencyUnavailableError, AlipayGatewayError, KeyError, ValueError) as exc:
        if isinstance(exc, RefundGatewayUnavailableError):
            raise
        raise RefundGatewayUnavailableError("支付宝退款状态暂时无法确认") from exc

    status = str(gateway_result.get("refund_status") or "")
    if status == "REFUND_SUCCESS":
        succeeded = await mark_checkout_refund_succeeded(
            refund_id=refund.refund_id,
            provider_refund_reference=str(gateway_result.get("trade_no") or "") or None,
        )
        if succeeded is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(succeeded, idempotent_replay=False)
    if status == "REFUND_FAIL":
        failed = await mark_checkout_refund_failed(refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)
    return _to_result(refund, idempotent_replay=True)
