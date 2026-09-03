"""UnionPay U1 checkout integration boundaries; external gateway calls are mocked."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from config import settings
from infra.unionpay_test import UnionPayFrontForm, UnionPayGatewayError, UnionPayQueryResult
from service.cart_service import create_cart_checkout_session
from service.checkout_refund_service import RefundProviderUnavailableError, request_customer_refund
from service.checkout_service import (
    PaymentStatusUnavailableError,
    UnionPayCancellationUnavailableError,
    UnionPayResumePaidError,
    UnionPayResumePendingError,
    cancel_checkout_session,
    create_checkout_session,
    refresh_customer_payment_status,
    resume_checkout_session,
)
from store.cart_store import StoredCartItem
from store.checkout_refund_store import RefundProviderUnsupportedError, get_customer_refund_eligibility
from store.checkout_store import (
    CartCheckoutLine,
    CheckoutProduct,
    CreatedCheckoutOrder,
    CustomerPendingCheckout,
    CustomerPendingPayment,
    apply_verified_payment_success,
)


def _product() -> CheckoutProduct:
    return CheckoutProduct(
        category="laptops",
        product_id="laptop-unionpay",
        product_name="测试银联笔记本",
        brand="测试品牌",
        unit_amount_cents=299900,
        stock=8,
    )


def _front_form() -> UnionPayFrontForm:
    return UnionPayFrontForm(
        action="https://gateway.test.95516.com/gateway/api/frontTransReq.do",
        fields={"orderId": "PMV2TEST", "txnAmt": "299900"},
    )


def _query_result(
    *,
    merchant_payment_no: str = "PMV2TEST",
    txn_time: str = "20260831153000",
    txn_amt: str = "299900",
    query_id: str = "UP-QUERY-1",
    resp_code: str = "00",
    orig_resp_code: str = "00",
    signature_verified: bool = True,
) -> UnionPayQueryResult:
    return UnionPayQueryResult(
        signature_verified=signature_verified,
        resp_code=resp_code,
        orig_resp_code=orig_resp_code,
        query_id=query_id,
        txn_amt=txn_amt,
        order_id=merchant_payment_no,
        txn_time=txn_time,
    )


@pytest.mark.asyncio
async def test_create_unionpay_checkout_persists_txn_time_and_builds_server_form() -> None:
    product = _product()
    client = MagicMock()
    client.build_front_payment_form.return_value = _front_form()

    async def create_order(**kwargs: object) -> CreatedCheckoutOrder:
        return CreatedCheckoutOrder(
            sales_order_id=uuid4(),
            order_no=str(kwargs["order_no"]),
            merchant_payment_no=str(kwargs["merchant_payment_no"]),
            total_amount_cents=product.unit_amount_cents,
            provider="unionpay_test",
            provider_txn_time=str(kwargs["provider_txn_time"]),
        )

    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch.object(settings, "unionpay_public_base_url", "https://return.test"),
        patch("service.checkout_service.get_checkout_product", new=AsyncMock(return_value=product)),
        patch("service.checkout_service.find_reusable_pending_checkout", new=AsyncMock(return_value=None)) as reuse,
        patch("service.checkout_service.create_checkout_order", new=AsyncMock(side_effect=create_order)) as create,
    ):
        session = await create_checkout_session(
            customer_user_id=101,
            category="laptops",
            product_id=product.product_id,
            quantity=1,
            return_origin="http://127.0.0.1:5173",
            payment_provider="unionpay_test",
        )

    assert session.payment_provider == "unionpay_test"
    assert session.payment_form_action == _front_form().action
    assert session.payment_form_fields == _front_form().fields
    reuse.assert_awaited_once_with(101, product, 1, "unionpay_test")
    create_call = create.await_args
    assert create_call is not None
    provider_txn_time = create_call.kwargs["provider_txn_time"]
    assert isinstance(provider_txn_time, str)
    assert re.fullmatch(r"\d{14}", provider_txn_time)
    assert create_call.kwargs["provider"] == "unionpay_test"
    client.build_front_payment_form.assert_called_once_with(
        order_id=create_call.kwargs["merchant_payment_no"],
        txn_time=provider_txn_time,
        txn_amt=product.unit_amount_cents,
        front_url="https://return.test/api/v1/payments/unionpay/front-return",
    )


@pytest.mark.asyncio
async def test_cart_unionpay_checkout_is_provider_isolated_and_persists_txn_time() -> None:
    product = _product()
    client = MagicMock()
    client.build_front_payment_form.return_value = _front_form()
    cart_item = StoredCartItem(item_id=7, category="laptops", product_id=product.product_id, quantity=2)

    with (
        patch("service.cart_service.UnionPayTestClient.from_settings", return_value=client),
        patch.object(settings, "unionpay_public_base_url", "https://return.test"),
        patch("service.cart_service.list_cart_items", new=AsyncMock(return_value=[cart_item])),
        patch("service.cart_service.get_checkout_product", new=AsyncMock(return_value=product)),
        patch("service.cart_service.find_reusable_pending_cart_checkout", new=AsyncMock(return_value=None)) as reuse,
        patch("service.cart_service.create_checkout_order_from_lines", new=AsyncMock()) as create,
    ):
        session = await create_cart_checkout_session(
            101,
            "http://localhost:5173",
            "unionpay_test",
        )

    assert session.payment_provider == "unionpay_test"
    reuse.assert_awaited_once()
    reuse_call = reuse.await_args
    create_call = create.await_args
    assert reuse_call is not None
    assert create_call is not None
    assert reuse_call.args[3] == "unionpay_test"
    assert create_call.kwargs["provider"] == "unionpay_test"
    assert re.fullmatch(r"\d{14}", create_call.kwargs["provider_txn_time"])
    assert create_call.kwargs["cart_lines"] == [
        CartCheckoutLine(product.category, product.product_id, 2, product.unit_amount_cents)
    ]


@pytest.mark.asyncio
async def test_resume_unionpay_pending_preflight_never_reopens_same_provider_transaction() -> None:
    pending = CustomerPendingPayment(
        merchant_payment_no="PMV2ORIGINAL",
        amount_cents=299900,
        subject="测试银联笔记本",
        provider="unionpay_test",
        provider_txn_time="20260831153000",
    )
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.refresh_customer_payment_status", new=AsyncMock(return_value=False)) as refresh,
        pytest.raises(UnionPayResumePendingError),
    ):
        await resume_checkout_session(
            customer_user_id=101,
            order_no="SOORIGINAL",
            return_origin="http://localhost:5173",
        )
    refresh.assert_awaited_once_with(customer_user_id=101, order_no="SOORIGINAL")


@pytest.mark.asyncio
async def test_resume_unionpay_paid_preflight_converges_before_any_new_front_form() -> None:
    pending = CustomerPendingPayment("PMV2ORIGINAL", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.refresh_customer_payment_status", new=AsyncMock(return_value=True)) as refresh,
        pytest.raises(UnionPayResumePaidError),
    ):
        await resume_checkout_session(customer_user_id=101, order_no="SOORIGINAL", return_origin="http://127.0.0.1:5173")
    refresh.assert_awaited_once_with(customer_user_id=101, order_no="SOORIGINAL")


@pytest.mark.asyncio
async def test_refresh_unionpay_success_requires_verified_matching_query_and_dispatches_once() -> None:
    pending = CustomerPendingPayment("PMV2TEST", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    client = MagicMock()
    client.query_transaction = AsyncMock(return_value=_query_result())
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.apply_verified_payment_success", new=AsyncMock(return_value=True)) as apply,
    ):
        changed = await refresh_customer_payment_status(customer_user_id=101, order_no="SOUPAY")

    assert changed is True
    client.query_transaction.assert_awaited_once_with(order_id="PMV2TEST", txn_time="20260831153000")
    apply.assert_awaited_once_with(
        provider="unionpay_test",
        merchant_payment_no="PMV2TEST",
        provider_trade_no="UP-QUERY-1",
        amount_cents=299900,
    )


@pytest.mark.asyncio
async def test_refresh_unionpay_amount_mismatch_fails_closed_without_state_update() -> None:
    pending = CustomerPendingPayment("PMV2TEST", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    client = MagicMock()
    client.query_transaction = AsyncMock(return_value=_query_result(txn_amt="100"))
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.apply_verified_payment_success", new=AsyncMock()) as apply,
        pytest.raises(PaymentStatusUnavailableError),
    ):
        await refresh_customer_payment_status(customer_user_id=101, order_no="SOUPAY")
    apply.assert_not_awaited()


@pytest.mark.parametrize("query_id", ["", "   "])
@pytest.mark.asyncio
async def test_refresh_unionpay_missing_query_id_fails_closed(query_id: str) -> None:
    pending = CustomerPendingPayment("PMV2TEST", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    client = MagicMock()
    client.query_transaction = AsyncMock(return_value=_query_result(query_id=query_id))
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        pytest.raises(PaymentStatusUnavailableError),
    ):
        await refresh_customer_payment_status(customer_user_id=101, order_no="SOUPAY")


@pytest.mark.asyncio
async def test_refresh_unionpay_network_failure_keeps_local_payment_pending() -> None:
    pending = CustomerPendingPayment("PMV2TEST", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    client = MagicMock()
    client.query_transaction = AsyncMock(side_effect=UnionPayGatewayError("gateway unavailable"))
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.apply_verified_payment_success", new=AsyncMock()) as apply,
        pytest.raises(PaymentStatusUnavailableError),
    ):
        await refresh_customer_payment_status(customer_user_id=101, order_no="SOUPAY")
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_unionpay_non_success_original_response_keeps_local_pending() -> None:
    pending = CustomerPendingPayment("PMV2TEST", 299900, "测试银联笔记本", "unionpay_test", "20260831153000")
    client = MagicMock()
    client.query_transaction = AsyncMock(return_value=_query_result(orig_resp_code="03"))
    with (
        patch("service.checkout_service.get_customer_pending_payment", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.apply_verified_payment_success", new=AsyncMock()) as apply,
    ):
        changed = await refresh_customer_payment_status(customer_user_id=101, order_no="SOUPAY")
    assert changed is False
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_pending_cancel_fails_closed_before_any_gateway_or_local_cancel() -> None:
    pending = CustomerPendingCheckout("SOUPAY", "PMV2TEST", "unionpay_test", "20260831153000")
    with (
        patch("service.checkout_service.get_customer_pending_checkout", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.AlipaySandboxClient.from_settings") as alipay,
        patch("service.checkout_service.cancel_customer_pending_checkout", new=AsyncMock()) as cancel,
    ):
        with pytest.raises(UnionPayCancellationUnavailableError):
            await cancel_checkout_session(customer_user_id=101, order_no="SOUPAY")
    alipay.assert_not_called()
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_unionpay_refund_request_is_rejected_before_refund_record_creation() -> None:
    with patch(
        "service.checkout_refund_service.create_customer_refund_request",
        new=AsyncMock(side_effect=RefundProviderUnsupportedError()),
    ) as create:
        with pytest.raises(RefundProviderUnavailableError):
            await request_customer_refund(
                customer_user_id=101,
                order_no="SOUPAY",
                reason="测试",
                request_idempotency_key="unionpay-refund-1",
            )
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_unionpay_refund_eligibility_is_supported_when_business_facts_match() -> None:
    cursor = MagicMock()
    cursor.fetchone = AsyncMock(return_value=(uuid4(), uuid4(), "PMV2TEST", 299900, 299900, "unionpay_test"))
    connection = MagicMock()
    connection.execute = AsyncMock(return_value=cursor)
    with (
        patch("store.checkout_refund_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_refund_store.put_connection", new=AsyncMock()),
    ):
        eligible = await get_customer_refund_eligibility(customer_user_id=101, order_no="SOUPAY")
    assert eligible is True


@pytest.mark.asyncio
async def test_verified_unionpay_success_converges_payment_order_and_fulfillment_in_one_transaction() -> None:
    payment_id = uuid4()
    sales_order_id = uuid4()
    payment_cursor = MagicMock()
    payment_cursor.fetchone = AsyncMock(return_value=(payment_id, sales_order_id, 299900, "PENDING"))
    cart_cursor = MagicMock()
    cart_cursor.fetchall = AsyncMock(return_value=[])
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=[None, payment_cursor, None, None, cart_cursor, None])
    connection.commit = AsyncMock()
    connection.rollback = AsyncMock()
    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()),
    ):
        changed = await apply_verified_payment_success(
            provider="unionpay_test",
            merchant_payment_no="PMV2TEST",
            provider_trade_no="UP-QUERY-1",
            amount_cents=299900,
        )

    assert changed is True
    connection.commit.assert_awaited_once()
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert "AND provider = %s" in statements[1]
    assert any("SET status = 'SUCCEEDED'" in statement for statement in statements)
    assert any("SET status = 'PAID'" in statement for statement in statements)
    assert any("INSERT INTO fulfillments" in statement for statement in statements)
