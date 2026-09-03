"""新商城全额退款的确定性应用服务。

模型可以解释退款规则或引导客户进入订单页，但不能直接绕过本服务调用支付宝。
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4

from exceptions import DependencyUnavailableError
from infra.alipay_sandbox import AlipayGatewayError, AlipayRefundRejectedError, AlipaySandboxClient
from infra.unionpay_test import (
    UNIONPAY_TIMEZONE,
    UnionPayGatewayError,
    UnionPayProtocolError,
    UnionPaySignatureError,
    UnionPayTestClient,
)
from store.checkout_refund_store import (
    CheckoutRefund,
    RefundIdempotencyConflictError,
    RefundProviderUnsupportedError,
    create_customer_refund_request,
    get_checkout_refund,
    get_customer_checkout_refund,
    get_customer_refund_eligibility,
    mark_checkout_refund_failed,
    mark_checkout_refund_succeeded,
    reject_finance_refund,
    start_customer_refund_confirmation,
    start_finance_refund_approval,
)

logger = logging.getLogger(__name__)


class RefundNotEligibleError(ValueError):
    """订单不属于当前客户，或不满足首版全额退款条件。"""


class RefundProviderUnavailableError(ValueError):
    """当前支付渠道尚未接入退款能力。"""


class RefundConfirmationUnavailableError(ValueError):
    """退款已经完成、已失败，或确认状态发生并发变化。"""


async def generate_customer_refund_entry(
    *,
    customer_user_id: int,
    order_no: str,
    eligibility_already_verified: bool = False,
) -> str | None:
    """返回当前客户可安全进入的退款自助页面，不创建或确认退款。

    入口在服务端再次按客户和订单校验资格后才会返回。链接只进入已登录客户的
    “我的订单”页面；真正的退款申请与资金确认仍由该页面上的受控 API 完成。

    ``eligibility_already_verified`` 仅供同一轮客服 Workflow 在已通过当前客户、
    当前订单的资格读取后复用该可信结果；订单页的真实退款写入口仍会重新校验。
    """
    if not eligibility_already_verified and not await get_customer_refund_eligibility(
        customer_user_id=customer_user_id,
        order_no=order_no,
    ):
        return None
    # ``order_no`` 来自服务端订单事实，且 checkout 订单号只允许 SO 前缀；不接受
    # 模型直接拼接的 URL。前端仍会按登录用户重新读取订单，不能借此跨账户操作。
    return f"?page=orders&refund_order={order_no}"


class RefundGatewayUnavailableError(ValueError):
    """支付渠道结果未知，必须保留处理中状态而不是再次发起退款。"""


class UnionPayRefundValidationError(RefundGatewayUnavailableError):
    """银联退款响应在明确校验分支失败，携带脱敏诊断码。"""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


class FinanceRefundDecisionUnavailableError(ValueError):
    """财务决策对应的退款不存在、已处理或状态已发生变化。"""


def _unionpay_failure_reason_code(error: BaseException) -> str:
    """Map provider failures to a safe diagnostic code without logging details."""
    explicit_code = getattr(error, "reason_code", None)
    if isinstance(explicit_code, str) and explicit_code:
        return explicit_code
    if isinstance(error, RefundConfirmationUnavailableError):
        return "LOCAL_REFUND_STATE_CHANGED"
    if isinstance(error, UnionPaySignatureError):
        return "SIGNATURE_VERIFICATION_FAILED"
    if isinstance(error, UnionPayGatewayError):
        return "TRANSPORT_FAILURE"
    if isinstance(error, UnionPayProtocolError):
        return "UNIONPAY_PROTOCOL_ERROR"
    if isinstance(error, RefundGatewayUnavailableError):
        return "RESULT_UNAVAILABLE"
    if isinstance(error, DependencyUnavailableError):
        return "DEPENDENCY_UNAVAILABLE"
    if isinstance(error, KeyError):
        return "RESPONSE_FIELD_MISSING"
    if isinstance(error, (TypeError, ValueError)):
        return "RESPONSE_VALIDATION_FAILURE"
    return "UNKNOWN_PROVIDER_FAILURE"


def _log_unionpay_refund_failure(
    refund: CheckoutRefund,
    *,
    phase: str,
    error: BaseException,
    result: object | None = None,
    identity_validation_stage: str | None = None,
) -> None:
    """Emit only bounded refund diagnostics; never include gateway bodies or secrets."""
    logger.warning(
        "unionpay refund provider operation failed",
        extra={
            "refund_id": str(refund.refund_id),
            "provider": refund.provider,
            "phase": phase,
            "merchant_refund_no": refund.merchant_refund_no,
            "failure_reason_code": _unionpay_failure_reason_code(error),
            "resp_code": getattr(result, "resp_code", None) if result is not None else None,
            "orig_resp_code": getattr(result, "orig_resp_code", None) if result is not None else None,
            "has_query_id": bool(getattr(result, "query_id", None)) if result is not None else False,
            "has_orig_qry_id": bool(getattr(result, "orig_qry_id", None)) if result is not None else False,
            "identity_validation_stage": identity_validation_stage,
        },
    )


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


def customer_visible_refund_status(status: str | None) -> str | None:
    """将退款存储/财务内部状态映射为客户和 Agent 可见的状态。

    财务接口仍然使用 ``CustomerRefundResult.status`` 中的内部原值；只有
    客户侧投影和客户 Agent 查询使用这里的映射，避免把审批岗位或支付适配器
    的内部枚举暴露给客户。
    """
    if status is None:
        return None
    normalized = str(status).upper()
    return {
        "PENDING_FINANCE_APPROVAL": "PENDING_MERCHANT_REVIEW",
        "SUCCEEDED": "COMPLETED",
        "SUCCESS": "COMPLETED",
        "REFUND_SUCCESS": "COMPLETED",
        "REJECTED": "FAILED",
        "REFUND_FAIL": "FAILED",
    }.get(normalized, normalized)


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
    # This is a stable provider order ID only.  UnionPay txnTime is persisted
    # at the actual provider-submission boundary (``processing_at``), not at
    # request creation time.
    now = datetime.now(UNIONPAY_TIMEZONE)
    suffix = uuid4().hex[:12].upper()
    merchant_refund_no = f"RF{now:%Y%m%d%H%M%S}{suffix}"
    try:
        refund = await create_customer_refund_request(
            refund_id=uuid4(),
            customer_user_id=customer_user_id,
            order_no=order_no,
            merchant_refund_no=merchant_refund_no,
            request_idempotency_key=request_idempotency_key,
            reason=reason,
            status="AUTO",
        )
    except RefundIdempotencyConflictError as exc:
        raise RefundNotEligibleError("idempotency key is bound to another order") from exc
    except RefundProviderUnsupportedError as exc:
        raise RefundProviderUnavailableError("current payment provider has no refund support") from exc
    if refund is None:
        raise RefundNotEligibleError("checkout refund is not eligible")
    return _to_result(refund, idempotent_replay=refund.merchant_refund_no != merchant_refund_no)


async def confirm_customer_refund(
    *,
    customer_user_id: int,
    refund_id: UUID,
    confirmation_idempotency_key: str,
) -> CustomerRefundResult:
    """在客户明确确认后提交一次全额退款。

    网关超时或网络失败时不把退款改回可重试状态，因为支付渠道可能已经受理；系统
    保持 PROCESSING，后续只能通过退款查询/对账收敛，避免二次退款。该模式是
    at-most-once provider submission + query reconciliation，并非 crash-safe exactly-once。

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

    return await _submit_refund_to_provider(started.refund)


async def approve_finance_refund(
    *,
    finance_user_id: int,
    refund_id: UUID,
    decision_idempotency_key: str,
    decision_note: str,
) -> CustomerRefundResult:
    """财务批准一笔超出自动资格范围的退款并提交同一确定性支付服务。

    Args:
        finance_user_id: 当前财务用户 ID。
        refund_id: 待审批退款 UUID。
        decision_idempotency_key: 财务命令幂等键。
        decision_note: 财务审批备注。

    Returns:
        处理中或最终成功/失败的退款结果；相同幂等键重放不会再次调用支付渠道。

    Raises:
        FinanceRefundDecisionUnavailableError: 退款不在待审批状态。
        RefundGatewayUnavailableError: 支付结果未知，退款保持处理中。
    """
    started = await start_finance_refund_approval(
        finance_user_id=finance_user_id,
        refund_id=refund_id,
        decision_idempotency_key=decision_idempotency_key,
        decision_note=decision_note,
    )
    if started is None:
        raise FinanceRefundDecisionUnavailableError("refund is not awaiting finance approval")
    if not started.should_submit_to_provider:
        return _to_result(started.refund, idempotent_replay=started.idempotent_replay)
    return await _submit_refund_to_provider(started.refund)


async def reject_finance_refund_request(
    *,
    finance_user_id: int,
    refund_id: UUID,
    decision_idempotency_key: str,
    decision_note: str,
) -> CustomerRefundResult:
    """财务驳回退款申请；不调用支付网关、不修改订单支付事实。"""
    rejected = await reject_finance_refund(
        finance_user_id=finance_user_id,
        refund_id=refund_id,
        decision_idempotency_key=decision_idempotency_key,
        decision_note=decision_note,
    )
    if rejected is None:
        raise FinanceRefundDecisionUnavailableError("refund is not awaiting finance approval")
    refund, replay = rejected
    return _to_result(refund, idempotent_replay=replay)


async def _submit_refund_to_provider(refund: CheckoutRefund) -> CustomerRefundResult:
    """将已获得唯一提交资格的退款分发到已支持的支付渠道。"""
    if refund.provider == "alipay_sandbox":
        return await _submit_alipay_refund(refund)
    if refund.provider == "unionpay_test":
        return await _submit_unionpay_refund(refund)
    raise RefundProviderUnavailableError("current payment provider has no refund support")


async def _submit_alipay_refund(refund: CheckoutRefund) -> CustomerRefundResult:
    """保留现有支付宝退款行为；不与银联协议混用。"""
    try:
        gateway_result = await AlipaySandboxClient.from_settings().refund_trade(
            merchant_payment_no=refund.merchant_payment_no,
            merchant_refund_no=refund.merchant_refund_no,
            amount_cents=refund.amount_cents,
        )
        returned_order_no = gateway_result.get("out_trade_no")
        refunded_cents = int(
            (Decimal(str(gateway_result["refund_fee"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        if returned_order_no != refund.merchant_payment_no or refunded_cents != refund.amount_cents:
            raise RefundGatewayUnavailableError("支付宝退款结果无法匹配")
    except AlipayRefundRejectedError:
        failed = await mark_checkout_refund_failed(refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)
    except (DependencyUnavailableError, AlipayGatewayError, KeyError, ValueError) as exc:
        if isinstance(exc, RefundGatewayUnavailableError):
            raise
        raise RefundGatewayUnavailableError("退款结果暂时无法确认") from exc

    succeeded = await mark_checkout_refund_succeeded(
        refund_id=refund.refund_id,
        provider_refund_reference=str(gateway_result.get("trade_no") or "") or None,
    )
    if succeeded is None:
        raise RefundConfirmationUnavailableError("refund state changed")
    return _to_result(succeeded, idempotent_replay=False)


def _unionpay_refund_txn_time(refund: CheckoutRefund) -> str:
    if not refund.processing_at:
        raise UnionPayRefundValidationError("TXN_TIME_MISSING", "银联退款交易时间无法恢复")
    try:
        raw = refund.processing_at.replace("Z", "+00:00")
        processing_at = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise UnionPayRefundValidationError("TXN_TIME_MISSING", "银联退款交易时间无法恢复") from exc
    if processing_at.tzinfo is None:
        raise UnionPayRefundValidationError("TXN_TIME_MISSING", "银联退款交易时间无法恢复")
    return processing_at.astimezone(UNIONPAY_TIMEZONE).strftime("%Y%m%d%H%M%S")


def _validate_unionpay_query_identity(
    refund: CheckoutRefund,
    result: object,
    txn_time: str,
) -> None:
    """验证银联退款查询的签名结果与本地退款事实。"""
    if getattr(result, "signature_verified", False) is not True:
        raise UnionPayRefundValidationError("SIGNATURE_VERIFICATION_FAILED", "退款查询响应验签未通过")
    if getattr(result, "order_id", None) != refund.merchant_refund_no:
        raise UnionPayRefundValidationError("ORDER_ID_MISMATCH", "银联退款查询订单号无法匹配")
    if getattr(result, "txn_time", None) != txn_time:
        raise UnionPayRefundValidationError("TXN_TIME_MISMATCH", "银联退款查询交易时间无法匹配")
    # A signed query ``34`` means no provider record is currently available.
    # It is intentionally kept PROCESSING and may omit transaction fields.
    if getattr(result, "resp_code", None) == "34":
        return

    # ``txnAmt`` is optional at this protocol boundary: if the provider sends
    # it, it is an additional amount assertion; if it does not, its absence is
    # not a transport/protocol failure because queryTrans does not guarantee
    # this echo for every response shape.  ``origQryId`` belongs to the
    # server-generated backTransReq request and is intentionally not required
    # in the queryTrans response.
    returned_amount = getattr(result, "txn_amt", None)
    if returned_amount not in (None, ""):
        try:
            amount_cents = int(str(returned_amount))
        except (TypeError, ValueError) as exc:
            raise UnionPayRefundValidationError("TXN_AMOUNT_MISMATCH", "银联退款查询金额无法核验") from exc
        if amount_cents != refund.amount_cents:
            raise UnionPayRefundValidationError("TXN_AMOUNT_MISMATCH", "银联退款查询金额无法匹配")


def _validate_unionpay_submission(refund: CheckoutRefund, result: object, txn_time: str) -> str:
    """验证 backTransReq 的已验签响应，最终成功仍以 queryTrans 为准。"""
    if getattr(result, "signature_verified", False) is not True:
        raise UnionPayRefundValidationError("SIGNATURE_VERIFICATION_FAILED", "银联退款响应验签未通过")
    if getattr(result, "order_id", None) != refund.merchant_refund_no:
        raise UnionPayRefundValidationError("ORDER_ID_MISMATCH", "银联退款响应订单号无法匹配")
    if getattr(result, "txn_time", None) != txn_time:
        raise UnionPayRefundValidationError("TXN_TIME_MISMATCH", "银联退款响应交易时间无法匹配")
    resp_code = str(getattr(result, "resp_code", ""))
    if not resp_code:
        raise UnionPayRefundValidationError("RESP_CODE_UNEXPECTED", "银联退款响应缺少响应码")
    if resp_code == "00":
        return "ACCEPTED"
    if resp_code in {"03", "04", "05"}:
        return "PROCESSING"
    # A signed, identity-bound deterministic rejection is a real provider
    # failure; unlike a network/signature failure it must not remain forever
    # indistinguishable from an in-flight request.
    return "FAILED"


def _unionpay_query_outcome(result: object) -> str:
    """Classify only protocol-confirmed UnionPay refund query outcomes."""
    resp_code = str(getattr(result, "resp_code", ""))
    if resp_code == "34":
        return "PROCESSING"
    if resp_code != "00":
        raise UnionPayRefundValidationError("RESP_CODE_UNEXPECTED", "银联退款查询返回了非成功响应码")
    orig_resp_code = str(getattr(result, "orig_resp_code", ""))
    if orig_resp_code == "00":
        return "SUCCEEDED"
    if orig_resp_code in {"03", "04", "05"}:
        return "PROCESSING"
    if orig_resp_code:
        return "FAILED"
    raise UnionPayRefundValidationError("ORIG_RESP_CODE_UNEXPECTED", "银联退款查询返回了明确非成功状态")


async def _submit_unionpay_refund(refund: CheckoutRefund) -> CustomerRefundResult:
    """提交并查询一笔银联全额退款；未知结果始终留在 PROCESSING。"""
    if not isinstance(refund.provider_trade_no, str) or not refund.provider_trade_no.strip():
        error = UnionPayRefundValidationError("ORIG_QRY_ID_MISSING", "银联原支付交易号缺失")
        _log_unionpay_refund_failure(refund, phase="prepare", error=error)
        raise error
    try:
        txn_time = _unionpay_refund_txn_time(refund)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="prepare", error=exc)
        raise

    try:
        client = UnionPayTestClient.from_settings()
        submitted = await client.refund_transaction(
            order_id=refund.merchant_refund_no,
            txn_time=txn_time,
            txn_amt=refund.amount_cents,
            orig_qry_id=refund.provider_trade_no,
        )
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="submit_http", error=exc)
        raise
    except (DependencyUnavailableError, UnionPayProtocolError, KeyError, TypeError, ValueError) as exc:
        _log_unionpay_refund_failure(refund, phase="submit_http", error=exc)
        raise RefundGatewayUnavailableError("退款结果暂时无法确认") from exc

    try:
        submission_outcome = _validate_unionpay_submission(refund, submitted, txn_time)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(
            refund,
            phase="submit_validate",
            error=exc,
            result=submitted,
            identity_validation_stage="backTransReq",
        )
        raise

    if submission_outcome == "FAILED":
        _log_unionpay_refund_failure(
            refund,
            phase="submit_classify",
            error=UnionPayRefundValidationError("RESP_CODE_UNEXPECTED", "银联退款响应返回了明确拒绝码"),
            result=submitted,
            identity_validation_stage="backTransReq",
        )
        failed = await mark_checkout_refund_failed(refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)

    try:
        queried = await client.query_transaction(order_id=refund.merchant_refund_no, txn_time=txn_time)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="query_http", error=exc)
        raise
    except (DependencyUnavailableError, UnionPayProtocolError, KeyError, TypeError, ValueError) as exc:
        _log_unionpay_refund_failure(refund, phase="query_http", error=exc)
        raise RefundGatewayUnavailableError("退款结果暂时无法确认") from exc

    try:
        _validate_unionpay_query_identity(refund, queried, txn_time)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(
            refund,
            phase="query_validate",
            error=exc,
            result=queried,
            identity_validation_stage="queryTrans",
        )
        raise

    try:
        query_outcome = _unionpay_query_outcome(queried)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="query_classify", error=exc, result=queried)
        raise

    if query_outcome == "PROCESSING":
        return _to_result(refund, idempotent_replay=False)
    if query_outcome == "FAILED":
        _log_unionpay_refund_failure(
            refund,
            phase="query_classify",
            error=UnionPayRefundValidationError("ORIG_RESP_CODE_UNEXPECTED", "银联退款查询返回了明确失败状态"),
            result=queried,
        )
        failed = await mark_checkout_refund_failed(refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)
    if not isinstance(queried.query_id, str) or not queried.query_id.strip():
        error = UnionPayRefundValidationError("QUERY_ID_MISSING", "银联退款查询交易号缺失")
        _log_unionpay_refund_failure(
            refund,
            phase="query_validate",
            error=error,
            result=queried,
            identity_validation_stage="query_id",
        )
        raise error

    succeeded = await mark_checkout_refund_succeeded(
        refund_id=refund.refund_id,
        provider_refund_reference=queried.query_id,
    )
    if succeeded is None:
        raise RefundConfirmationUnavailableError("refund state changed")
    return _to_result(succeeded, idempotent_replay=False)


async def refresh_customer_refund_status(
    *,
    customer_user_id: int,
    refund_id: UUID,
) -> CustomerRefundResult:
    """主动查询支付渠道，将结果未知的退款收敛为最终状态。

    只有 ``PROCESSING`` 会访问支付渠道。已成功、失败或仍待客户确认的退款直接返回，
    因而浏览器重复点击刷新不会创建或重复提交退款。

    Args:
        customer_user_id: 当前已认证客户的内部用户 ID。
        refund_id: 客户只能查询自己的退款记录。

    Returns:
        当前退款状态；支付渠道仍在处理中时保留 ``PROCESSING``。

    Raises:
        RefundConfirmationUnavailableError: 退款不属于当前客户或不存在。
        RefundGatewayUnavailableError: 支付渠道无法可靠返回查询结果。
    """
    refund = await get_customer_checkout_refund(customer_user_id, refund_id)
    if refund is None:
        raise RefundConfirmationUnavailableError("refund is unavailable")
    return await _refresh_stored_refund(refund)


async def refresh_finance_refund_status(*, refund_id: UUID) -> CustomerRefundResult:
    """财务只读查询一笔已提交退款；绝不重复提交资金请求。"""
    refund = await get_checkout_refund(refund_id)
    if refund is None:
        raise RefundConfirmationUnavailableError("refund is unavailable")
    return await _refresh_stored_refund(refund)


async def _refresh_stored_refund(refund: CheckoutRefund) -> CustomerRefundResult:
    """按已持久化 provider identity 查询退款状态。"""
    if refund.status != "PROCESSING":
        return _to_result(refund, idempotent_replay=True)

    if refund.provider == "unionpay_test":
        return await _refresh_unionpay_refund(refund)
    if refund.provider != "alipay_sandbox":
        raise RefundProviderUnavailableError("current payment provider has no refund support")

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
            raise RefundGatewayUnavailableError("退款查询结果无法匹配")
    except (DependencyUnavailableError, AlipayGatewayError, KeyError, ValueError) as exc:
        if isinstance(exc, RefundGatewayUnavailableError):
            raise
        raise RefundGatewayUnavailableError("退款状态暂时无法确认") from exc

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


async def _refresh_unionpay_refund(refund: CheckoutRefund) -> CustomerRefundResult:
    """只查询银联退款，不重复提交资金请求。"""
    if not isinstance(refund.provider_trade_no, str) or not refund.provider_trade_no.strip():
        error = UnionPayRefundValidationError("ORIG_QRY_ID_MISSING", "银联原支付交易号缺失")
        _log_unionpay_refund_failure(refund, phase="prepare", error=error)
        raise error
    try:
        txn_time = _unionpay_refund_txn_time(refund)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="prepare", error=exc)
        raise

    try:
        queried = await UnionPayTestClient.from_settings().query_transaction(
            order_id=refund.merchant_refund_no,
            txn_time=txn_time,
        )
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="query_http", error=exc)
        raise
    except (DependencyUnavailableError, UnionPayProtocolError, KeyError, TypeError, ValueError) as exc:
        _log_unionpay_refund_failure(refund, phase="query_http", error=exc)
        raise RefundGatewayUnavailableError("退款状态暂时无法确认") from exc

    try:
        _validate_unionpay_query_identity(refund, queried, txn_time)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(
            refund,
            phase="query_validate",
            error=exc,
            result=queried,
            identity_validation_stage="queryTrans",
        )
        raise

    try:
        query_outcome = _unionpay_query_outcome(queried)
    except RefundGatewayUnavailableError as exc:
        _log_unionpay_refund_failure(refund, phase="query_classify", error=exc, result=queried)
        raise

    if query_outcome == "PROCESSING":
        return _to_result(refund, idempotent_replay=True)
    if query_outcome == "FAILED":
        _log_unionpay_refund_failure(
            refund,
            phase="query_classify",
            error=UnionPayRefundValidationError("ORIG_RESP_CODE_UNEXPECTED", "银联退款查询返回了明确失败状态"),
            result=queried,
        )
        failed = await mark_checkout_refund_failed(refund.refund_id)
        if failed is None:
            raise RefundConfirmationUnavailableError("refund state changed")
        return _to_result(failed, idempotent_replay=False)
    if not isinstance(queried.query_id, str) or not queried.query_id.strip():
        error = UnionPayRefundValidationError("QUERY_ID_MISSING", "银联退款查询交易号缺失")
        _log_unionpay_refund_failure(
            refund,
            phase="query_validate",
            error=error,
            result=queried,
            identity_validation_stage="query_id",
        )
        raise error
    succeeded = await mark_checkout_refund_succeeded(
        refund_id=refund.refund_id,
        provider_refund_reference=queried.query_id,
    )
    if succeeded is None:
        raise RefundConfirmationUnavailableError("refund state changed")
    return _to_result(succeeded, idempotent_replay=False)
