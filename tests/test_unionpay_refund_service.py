"""UnionPay U2 退款服务边界测试；不访问真实银联网关。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from infra.unionpay_test import UnionPayGatewayError, UnionPayQueryResult, UnionPayRefundResult, UnionPaySignatureError
from service.checkout_refund_service import (
    RefundGatewayUnavailableError,
    UnionPayRefundValidationError,
    _unionpay_failure_reason_code,
    confirm_customer_refund,
    refresh_customer_refund_status,
    request_customer_refund,
)
from store.checkout_refund_store import CheckoutRefund, RefundConfirmationStart


def _refund(*, status: str = "PROCESSING") -> CheckoutRefund:
    return CheckoutRefund(
        refund_id=uuid4(),
        order_no="SOUPAYREFUND01",
        merchant_payment_no="PMV2UNIONPAY01",
        merchant_refund_no="RF20260901010101ABCDEF",
        status=status,
        amount_cents=66900,
        currency="CNY",
        reason="测试退款",
        requested_at=datetime.now(UTC).isoformat(),
        processing_at="2026-09-01T01:01:01+00:00",
        provider="unionpay_test",
        provider_trade_no="PAYMENT-QUERY-1",
        provider_txn_time="20260901010000",
    )


def _submitted(
    refund: CheckoutRefund,
    *,
    orig_qry_id: str | None = None,
    txn_amt: str | None = None,
    signature_verified: bool = True,
    txn_time: str = "20260901090101",
    resp_code: str = "00",
):
    return UnionPayRefundResult(
        signature_verified=signature_verified,
        resp_code=resp_code,
        order_id=refund.merchant_refund_no,
        txn_time=txn_time,
        txn_amt=txn_amt or "",
        orig_qry_id=orig_qry_id,
    )


def _queried(refund: CheckoutRefund, **overrides: object) -> UnionPayQueryResult:
    values: dict[str, object] = {
        "signature_verified": True,
        "resp_code": "00",
        "orig_resp_code": "00",
        "query_id": "REFUND-QUERY-1",
        "txn_amt": str(refund.amount_cents),
        "order_id": refund.merchant_refund_no,
        "txn_time": "20260901090101",
        "orig_qry_id": refund.provider_trade_no,
    }
    values.update(overrides)
    return UnionPayQueryResult(**values)  # type: ignore[arg-type]


def test_unionpay_failure_diagnostics_use_explicit_safe_reason_codes() -> None:
    assert _unionpay_failure_reason_code(
        UnionPayRefundValidationError("TXN_AMOUNT_MISMATCH", "金额不匹配")
    ) == "TXN_AMOUNT_MISMATCH"
    assert _unionpay_failure_reason_code(UnionPaySignatureError("验签失败")) == "SIGNATURE_VERIFICATION_FAILED"
    assert _unionpay_failure_reason_code(UnionPayGatewayError("网关不可达")) == "TRANSPORT_FAILURE"
    assert _unionpay_failure_reason_code(RefundGatewayUnavailableError("结果未知")) == "RESULT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_unionpay_request_stays_pending_and_does_not_call_provider() -> None:
    refund = _refund(status="PENDING_CONFIRMATION")
    with (
        patch("service.checkout_refund_service.create_customer_refund_request", new=AsyncMock(return_value=refund)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings") as client,
    ):
        result = await request_customer_refund(
            customer_user_id=7,
            order_no=refund.order_no,
            reason=refund.reason,
            request_idempotency_key="unionpay-request-0001",
        )

    assert result.status == "PENDING_CONFIRMATION"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_unionpay_confirmation_submits_then_queries_and_never_calls_alipay() -> None:
    processing = _refund()
    succeeded = CheckoutRefund(**{**processing.__dict__, "status": "SUCCEEDED"})
    client = AsyncMock()
    client.refund_transaction.return_value = _submitted(processing)
    client.query_transaction.return_value = _queried(processing)
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings") as alipay,
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ) as marked,
    ):
        result = await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-0001",
        )

    assert result.status == "SUCCEEDED"
    client.refund_transaction.assert_awaited_once_with(
        order_id=processing.merchant_refund_no,
        txn_time="20260901090101",
        txn_amt=processing.amount_cents,
        orig_qry_id=processing.provider_trade_no,
    )
    client.query_transaction.assert_awaited_once_with(
        order_id=processing.merchant_refund_no,
        txn_time="20260901090101",
    )
    alipay.assert_not_called()
    marked.assert_awaited_once_with(
        refund_id=processing.refund_id,
        provider_refund_reference="REFUND-QUERY-1",
    )


@pytest.mark.asyncio
async def test_unionpay_query_amount_mismatch_stays_processing() -> None:
    processing = _refund()
    client = AsyncMock()
    client.refund_transaction.return_value = _submitted(processing)
    client.query_transaction.return_value = _queried(processing, txn_amt="1")
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.mark_checkout_refund_succeeded", new=AsyncMock()) as marked,
        pytest.raises(RefundGatewayUnavailableError),
    ):
        await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-0002",
        )
    marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_query_without_original_transaction_echo_can_succeed() -> None:
    processing = _refund()
    succeeded = CheckoutRefund(**{**processing.__dict__, "status": "SUCCEEDED"})
    client = AsyncMock()
    client.query_transaction.return_value = _queried(processing, orig_qry_id=None)
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ) as marked,
    ):
        result = await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)

    assert result.status == "SUCCEEDED"
    marked.assert_awaited_once_with(
        refund_id=processing.refund_id,
        provider_refund_reference="REFUND-QUERY-1",
    )


@pytest.mark.asyncio
async def test_unionpay_sync_response_does_not_require_request_echo_fields() -> None:
    processing = _refund()
    succeeded = CheckoutRefund(**{**processing.__dict__, "status": "SUCCEEDED"})
    client = AsyncMock()
    client.refund_transaction.return_value = _submitted(processing)
    client.query_transaction.return_value = _queried(processing, txn_amt="", orig_qry_id=None)
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ),
    ):
        result = await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-no-echo-fields",
        )

    assert result.status == "SUCCEEDED"
    client.refund_transaction.assert_awaited_once_with(
        order_id=processing.merchant_refund_no,
        txn_time="20260901090101",
        txn_amt=processing.amount_cents,
        orig_qry_id=processing.provider_trade_no,
    )


@pytest.mark.asyncio
async def test_unionpay_missing_original_payment_binding_stays_closed() -> None:
    processing = CheckoutRefund(**{**_refund().__dict__, "provider_trade_no": ""})
    client = AsyncMock()
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.mark_checkout_refund_succeeded", new=AsyncMock()) as marked,
        pytest.raises(RefundGatewayUnavailableError),
    ):
        await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-missing-original",
        )

    client.refund_transaction.assert_not_awaited()
    client.query_transaction.assert_not_awaited()
    marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_invalid_signature_never_marks_refund_succeeded() -> None:
    processing = _refund()
    client = AsyncMock()
    client.refund_transaction.return_value = _submitted(processing, signature_verified=False)
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.mark_checkout_refund_succeeded", new=AsyncMock()) as marked,
        pytest.raises(RefundGatewayUnavailableError),
    ):
        await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-0004",
        )
    client.query_transaction.assert_not_awaited()
    marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_unknown_query_keeps_processing_without_resubmission() -> None:
    processing = _refund()
    client = AsyncMock()
    client.query_transaction.return_value = _queried(processing, orig_resp_code="03")
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.mark_checkout_refund_succeeded", new=AsyncMock()) as marked,
    ):
        result = await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)

    assert result.status == "PROCESSING"
    client.refund_transaction.assert_not_awaited()
    client.query_transaction.assert_awaited_once_with(
        order_id=processing.merchant_refund_no,
        txn_time="20260901090101",
    )
    marked.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_confirmation_replay_does_not_submit_again() -> None:
    processing = _refund()
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, False)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings") as client,
    ):
        result = await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-replay",
        )

    assert result.status == "PROCESSING"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_unionpay_refund_txn_time_comes_from_persisted_processing_at() -> None:
    processing = CheckoutRefund(
        **{
            **_refund().__dict__,
            "merchant_refund_no": "RF20260901010101UNRELATED",
            "requested_at": "2026-09-01T00:00:00+00:00",
            "processing_at": "2026-09-01T08:12:34+00:00",
        }
    )
    client = AsyncMock()
    expected = "20260901161234"  # persisted 08:12:34 UTC in Asia/Shanghai
    client.refund_transaction.return_value = _submitted(processing, txn_time=expected)
    client.query_transaction.return_value = _queried(processing, txn_time=expected)
    succeeded = CheckoutRefund(**{**processing.__dict__, "status": "SUCCEEDED"})
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_refund_service.mark_checkout_refund_succeeded", new=AsyncMock(return_value=succeeded)),
    ):
        await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-processing-time",
        )

    client.refund_transaction.assert_awaited_once_with(
        order_id=processing.merchant_refund_no,
        txn_time=expected,
        txn_amt=processing.amount_cents,
        orig_qry_id=processing.provider_trade_no,
    )
    client.query_transaction.assert_awaited_once_with(order_id=processing.merchant_refund_no, txn_time=expected)


@pytest.mark.asyncio
async def test_unionpay_processing_without_persisted_submission_time_fails_closed() -> None:
    processing = CheckoutRefund(**{**_refund().__dict__, "processing_at": None})
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings") as client,
        pytest.raises(RefundGatewayUnavailableError),
    ):
        await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("orig_resp_code", ["03", "04", "05"])
async def test_unionpay_query_pending_codes_keep_processing(orig_resp_code: str) -> None:
    processing = _refund()
    client = AsyncMock()
    client.query_transaction.return_value = _queried(processing, orig_resp_code=orig_resp_code)
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
    ):
        result = await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)
    assert result.status == "PROCESSING"


@pytest.mark.asyncio
async def test_unionpay_query_not_found_keeps_processing_without_requiring_transaction_fields() -> None:
    processing = _refund()
    client = AsyncMock()
    client.query_transaction.return_value = _queried(
        processing,
        resp_code="34",
        orig_resp_code="",
        query_id="",
        txn_amt="",
        orig_qry_id=None,
    )
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
    ):
        result = await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)
    assert result.status == "PROCESSING"


@pytest.mark.asyncio
async def test_unionpay_query_explicit_failure_marks_failed() -> None:
    processing = _refund()
    failed = CheckoutRefund(**{**processing.__dict__, "status": "FAILED"})
    client = AsyncMock()
    client.query_transaction.return_value = _queried(processing, orig_resp_code="35")
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_failed",
            new=AsyncMock(return_value=failed),
        ) as marked,
    ):
        result = await refresh_customer_refund_status(customer_user_id=7, refund_id=processing.refund_id)
    assert result.status == "FAILED"
    marked.assert_awaited_once_with(processing.refund_id)


@pytest.mark.asyncio
async def test_unionpay_back_response_explicit_rejection_marks_failed_without_query() -> None:
    processing = _refund()
    failed = CheckoutRefund(**{**processing.__dict__, "status": "FAILED"})
    client = AsyncMock()
    client.refund_transaction.return_value = _submitted(processing, resp_code="35")
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_failed",
            new=AsyncMock(return_value=failed),
        ) as marked,
    ):
        result = await confirm_customer_refund(
            customer_user_id=7,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="unionpay-confirm-explicit-reject",
        )
    assert result.status == "FAILED"
    client.query_transaction.assert_not_awaited()
    marked.assert_awaited_once_with(processing.refund_id)
