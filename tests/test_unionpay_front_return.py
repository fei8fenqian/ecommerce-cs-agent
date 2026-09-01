"""UnionPay U1.1 前台回跳安全边界；不访问真实银联网关。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from config import settings
from infra.unionpay_test import (
    UnionPayGatewayError,
    UnionPayProtocolError,
    UnionPayQueryResult,
    parse_form_parameters,
)
from middleware.auth import ALLOWLIST_PATHS, AuthMiddleware
from service.checkout_service import (
    CheckoutUnavailableError,
    UnionPayFrontReturnOutcome,
    build_unionpay_customer_return_url,
    build_unionpay_front_return_url,
    process_unionpay_front_return,
)
from store.checkout_store import UnionPayFrontReturnPayment

MER_ID = "777290058213462"
MERCHANT_PAYMENT_NO = "PMV2FRONTRETURN01"
TXN_TIME = "20260831153000"
ORDER_NO = "SOFRONTRETURN01"


def _front_parameters(
    *,
    order_id: str = MERCHANT_PAYMENT_NO,
    txn_time: str = TXN_TIME,
    txn_amt: str = "299900",
) -> dict[str, str]:
    return {
        "encoding": "UTF-8",
        "certId": "69903319369",
        "merId": MER_ID,
        "orderId": order_id,
        "txnTime": txn_time,
        "txnAmt": txn_amt,
        "respCode": "00",
        "origRespCode": "00",
        "signature": "signed-front-response",
        "signPubKeyCert": "signed-response-certificate",
    }


def _local_payment(
    *,
    provider: str = "unionpay_test",
    txn_time: str | None = TXN_TIME,
) -> UnionPayFrontReturnPayment:
    return UnionPayFrontReturnPayment(
        order_no=ORDER_NO,
        customer_user_id=101,
        merchant_payment_no=MERCHANT_PAYMENT_NO,
        amount_cents=299900,
        provider=provider,  # type: ignore[arg-type]
        provider_txn_time=txn_time,
        payment_status="PENDING",
        order_status="PENDING_PAYMENT",
    )


def _query_result(
    *,
    order_id: str = MERCHANT_PAYMENT_NO,
    txn_time: str = TXN_TIME,
    txn_amt: str = "299900",
    query_id: str = "UP-QUERY-FRONT-1",
    signature_verified: bool = True,
) -> UnionPayQueryResult:
    return UnionPayQueryResult(
        signature_verified=signature_verified,
        resp_code="00",
        orig_resp_code="00",
        query_id=query_id,
        txn_amt=txn_amt,
        order_id=order_id,
        txn_time=txn_time,
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.mer_id = MER_ID
    client.certificates = object()
    return client


@pytest.mark.asyncio
async def test_valid_signed_front_return_queries_and_converges_verified_payment() -> None:
    client = _client()
    client.query_transaction = AsyncMock(return_value=_query_result())
    lookup = AsyncMock(return_value=_local_payment())
    apply = AsyncMock(return_value=True)
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.verify_response_signature", return_value=True) as verify,
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
        patch("service.checkout_service.apply_verified_payment_success", apply),
    ):
        outcome = await process_unionpay_front_return(_front_parameters())

    assert outcome == UnionPayFrontReturnOutcome(order_no=ORDER_NO, payment_result="paid")
    verify.assert_called_once()
    lookup.assert_awaited_once_with(MERCHANT_PAYMENT_NO)
    client.query_transaction.assert_awaited_once_with(order_id=MERCHANT_PAYMENT_NO, txn_time=TXN_TIME)
    apply.assert_awaited_once_with(
        provider="unionpay_test",
        merchant_payment_no=MERCHANT_PAYMENT_NO,
        provider_trade_no="UP-QUERY-FRONT-1",
        amount_cents=299900,
    )


@pytest.mark.asyncio
async def test_tampered_front_return_does_not_lookup_or_query_untrusted_order_id() -> None:
    client = _client()
    lookup = AsyncMock()
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch(
            "service.checkout_service.verify_response_signature",
            side_effect=UnionPayProtocolError("invalid signature"),
        ),
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
    ):
        outcome = await process_unionpay_front_return(_front_parameters(order_id="ATTACKERORDER"))

    assert outcome == UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")
    lookup.assert_not_awaited()
    client.query_transaction.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_front_signature_does_not_mutate_payment() -> None:
    client = _client()
    lookup = AsyncMock(return_value=_local_payment())
    apply = AsyncMock()
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.verify_response_signature", return_value=False),
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
        patch("service.checkout_service.apply_verified_payment_success", apply),
    ):
        outcome = await process_unionpay_front_return(_front_parameters())

    assert outcome.payment_result == "unknown"
    lookup.assert_not_awaited()
    apply.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("txn_amt", "100"),
        ("order_id", "PMV2OTHERORDER"),
        ("txn_time", "20260831153100"),
    ],
)
@pytest.mark.asyncio
async def test_query_identity_or_amount_mismatch_does_not_mark_payment_paid(field: str, value: str) -> None:
    client = _client()
    lookup = AsyncMock(return_value=_local_payment())
    apply = AsyncMock()
    front = _front_parameters()
    query_kwargs = {"txn_amt": "299900", "order_id": MERCHANT_PAYMENT_NO, "txn_time": TXN_TIME}
    if field == "txn_amt":
        query_kwargs["txn_amt"] = value
    elif field == "order_id":
        query_kwargs["order_id"] = value
    else:
        query_kwargs["txn_time"] = value
    client.query_transaction = AsyncMock(
        return_value=_query_result(
            order_id=query_kwargs["order_id"],
            txn_time=query_kwargs["txn_time"],
            txn_amt=query_kwargs["txn_amt"],
        )
    )
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.verify_response_signature", return_value=True),
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
        patch("service.checkout_service.apply_verified_payment_success", apply),
    ):
        outcome = await process_unionpay_front_return(front)

    assert outcome.payment_result == "unknown"
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_unavailable_keeps_local_payment_unchanged_and_returns_unknown() -> None:
    client = _client()
    client.query_transaction = AsyncMock(side_effect=UnionPayGatewayError("timeout"))
    lookup = AsyncMock(return_value=_local_payment())
    apply = AsyncMock()
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.verify_response_signature", return_value=True),
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
        patch("service.checkout_service.apply_verified_payment_success", apply),
    ):
        outcome = await process_unionpay_front_return(_front_parameters())

    assert outcome == UnionPayFrontReturnOutcome(order_no=ORDER_NO, payment_result="unknown")
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_alipay_payment_is_not_eligible_for_unionpay_front_return_mutation() -> None:
    client = _client()
    lookup = AsyncMock(return_value=_local_payment(provider="alipay_sandbox"))
    apply = AsyncMock()
    with (
        patch("service.checkout_service.UnionPayTestClient.from_settings", return_value=client),
        patch("service.checkout_service.verify_response_signature", return_value=True),
        patch("service.checkout_service.get_unionpay_front_return_payment", lookup),
        patch("service.checkout_service.apply_verified_payment_success", apply),
    ):
        outcome = await process_unionpay_front_return(_front_parameters())

    assert outcome.payment_result == "unknown"
    client.query_transaction.assert_not_called()
    apply.assert_not_awaited()


def test_front_return_form_parser_uses_standard_form_semantics() -> None:
    original = {
        "signature": "abc+def/ghi==",
        "signPubKeyCert": "-----BEGIN CERTIFICATE-----\nMIIB+/x=\n-----END CERTIFICATE-----",
        "respMsg": "Success Test",
    }
    parameters = parse_form_parameters(urlencode(original).encode("utf-8"))
    assert parameters == original


@pytest.mark.parametrize(
    "body",
    [
        b"signature=a&signature=b",
        b"orderId=A&orderId=B",
    ],
)
def test_front_return_form_parser_rejects_duplicate_fields(body: bytes) -> None:
    with pytest.raises(UnionPayProtocolError):
        parse_form_parameters(body)


def test_front_return_form_parser_does_not_preserve_unescaped_plus_as_literal_plus() -> None:
    parameters = parse_form_parameters(b"signature=a+b/c%3D%3D&respMsg=hello+world")
    assert parameters == {
        "signature": "a b/c==",
        "respMsg": "hello world",
    }


def test_unionpay_front_return_url_requires_safe_public_http_base() -> None:
    with patch.object(settings, "unionpay_public_base_url", "https://return.example.test/root/"):
        assert build_unionpay_front_return_url() == (
            "https://return.example.test/root/api/v1/payments/unionpay/front-return"
        )
    with patch.object(settings, "unionpay_public_base_url", "javascript:alert(1)"):
        with pytest.raises(CheckoutUnavailableError):
            build_unionpay_front_return_url()


def test_customer_redirect_ignores_untrusted_return_origin() -> None:
    with patch.object(settings, "unionpay_front_url", "https://evil.example.test"):
        redirect_url = build_unionpay_customer_return_url(ORDER_NO, payment_result="unknown")
    parsed = urlsplit(redirect_url)
    assert parsed.netloc == "127.0.0.1:5173"
    assert parse_qs(parsed.query) == {
        "page": ["orders"],
        "payment_return": ["1"],
        "checkout_order": [ORDER_NO],
        "payment_result": ["unknown"],
    }


def _api_app() -> FastAPI:
    from api.payments import payment_router

    app = FastAPI()
    app.include_router(payment_router)

    @app.get("/api/v1/checkout/orders/my")
    async def protected_checkout_route() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/checkout/orders/SOFRONTRETURN01/refunds")
    async def protected_refund_route() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(AuthMiddleware)
    return app


@pytest.mark.asyncio
async def test_public_front_return_parses_post_and_returns_303_orders_redirect() -> None:
    received: list[dict[str, str]] = []

    async def process(parameters: dict[str, str]) -> UnionPayFrontReturnOutcome:
        received.append(parameters)
        return UnionPayFrontReturnOutcome(order_no=ORDER_NO, payment_result="paid")

    with (
        patch("api.payments.process_unionpay_front_return", side_effect=process),
        patch.object(settings, "unionpay_front_url", "http://127.0.0.1:5173/"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_api_app()),
            base_url="https://public-return.example.test",
        ) as client:
            response = await client.post(
                "/api/v1/payments/unionpay/front-return",
                content=urlencode({"signature": "a+b/c==", "orderId": MERCHANT_PAYMENT_NO}).encode("utf-8"),
                headers={"content-type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert received == [{"signature": "a+b/c==", "orderId": MERCHANT_PAYMENT_NO}]
    assert response.headers["location"] == (
        f"http://127.0.0.1:5173/?page=orders&payment_return=1&checkout_order={ORDER_NO}"
    )


@pytest.mark.asyncio
async def test_public_get_without_signed_payload_is_safe_unknown_redirect() -> None:
    with (
        patch(
            "api.payments.process_unionpay_front_return",
            new=AsyncMock(return_value=UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")),
        ) as process,
        patch.object(settings, "unionpay_front_url", "http://127.0.0.1:5173/"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_api_app()),
            base_url="https://public-return.example.test",
        ) as client:
            response = await client.get(
                "/api/v1/payments/unionpay/front-return?return_origin=https%3A%2F%2Fevil.example.test",
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert response.headers["location"] == "http://127.0.0.1:5173/?page=orders&payment_return=1&payment_result=unknown"
    process.assert_awaited_once_with({"return_origin": "https://evil.example.test"})


def test_unionpay_front_return_is_publicly_allowlisted() -> None:
    assert "/api/v1/payments/unionpay/front-return" in ALLOWLIST_PATHS


@pytest.mark.asyncio
async def test_anonymous_checkout_and_refund_routes_stay_protected() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_api_app()),
        base_url="https://public-return.example.test",
    ) as client:
        checkout_response = await client.get("/api/v1/checkout/orders/my")
        refund_response = await client.post("/api/v1/checkout/orders/SOFRONTRETURN01/refunds")

    assert checkout_response.status_code == 401
    assert refund_response.status_code == 401
