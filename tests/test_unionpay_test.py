"""UnionPay 5.1.0 协议边界测试；不访问真实银联网关。"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization.pkcs12 import serialize_key_and_certificates
from cryptography.x509.oid import NameOID

from infra.unionpay_test import (
    SMOKE_AMOUNT_CENTS,
    UnionPayCertificateSet,
    UnionPayProtocolError,
    UnionPayRefundResult,
    UnionPaySignatureError,
    UnionPaySigningMaterial,
    UnionPayTestClient,
    _parse_response_parameters,
    canonicalize_parameters,
    load_signing_material,
    new_smoke_transaction,
    parse_form_parameters,
    sign_parameters,
    verify_response_signature,
)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    key: rsa.RSAPrivateKey,
    issuer_key: rsa.RSAPrivateKey,
    serial: int,
    is_ca: bool,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now.replace(microsecond=0))
        .not_valid_after(now.replace(microsecond=0).replace(year=now.year + 1))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=1 if is_ca else None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


@pytest.fixture
def certificate_fixture(tmp_path: Path) -> tuple[UnionPaySigningMaterial, UnionPayCertificateSet]:
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    middle_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = _name("CFCA TEST CS CA")
    middle_name = _name("CFCA TEST OCA1")
    leaf_name = _name("UnionPay Test Sign")
    root = _certificate(
        subject=root_name,
        issuer=root_name,
        key=root_key,
        issuer_key=root_key,
        serial=1001,
        is_ca=True,
    )
    middle = _certificate(
        subject=middle_name,
        issuer=root_name,
        key=middle_key,
        issuer_key=root_key,
        serial=1002,
        is_ca=True,
    )
    leaf = _certificate(
        subject=leaf_name,
        issuer=middle_name,
        key=leaf_key,
        issuer_key=middle_key,
        serial=1003,
        is_ca=False,
    )
    pfx = serialize_key_and_certificates(
        b"unionpay-test",
        leaf_key,
        leaf,
        [middle, root],
        serialization.NoEncryption(),
    )
    pfx_path = tmp_path / "sign.pfx"
    pfx_path.write_bytes(pfx)
    material = load_signing_material(pfx_path, "")
    encryption = _certificate(
        subject=_name("UnionPay Test Encryption"),
        issuer=middle_name,
        key=leaf_key,
        issuer_key=middle_key,
        serial=1004,
        is_ca=False,
    )
    return material, UnionPayCertificateSet(root=root, middle=middle, encryption=encryption)


def test_pfx_loads_and_cert_id_comes_from_decimal_serial(certificate_fixture):
    material, _certificates = certificate_fixture
    assert material.cert_id == "1003"
    assert material.private_key.key_size == 2048


def test_canonical_order_empty_value_and_signature_exclusion():
    assert canonicalize_parameters({"z": "last", "a": "", "signature": "ignore", "m": "中"}) == "a=&m=中&z=last"


def _official_rsa2_signature(material: UnionPaySigningMaterial, fields: dict[str, str]) -> bytes:
    canonical = canonicalize_parameters(fields)
    digest_hex = hashlib.sha256(canonical.encode("utf-8")).hexdigest().lower()
    return material.private_key.sign(
        digest_hex.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


def test_signature_matches_official_rsa2_pipeline_and_excludes_itself(certificate_fixture):
    material, _certificates = certificate_fixture
    fields = sign_parameters({"b": "2", "a": "1", "optional": ""}, material)
    signature = base64.b64decode(fields.pop("signature"))
    assert signature == _official_rsa2_signature(material, fields)
    assert signature != material.private_key.sign(
        canonicalize_parameters(fields).encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert fields["certId"] == "1003"


def test_request_filter_blank_matches_official_sdk_before_signing(certificate_fixture):
    material, _certificates = certificate_fixture
    fields = sign_parameters({"z": "  value  ", "empty": "", "spaces": "  ", "none": None}, material)
    assert fields["z"] == "value"
    assert "empty" not in fields
    assert "spaces" not in fields
    assert "none" not in fields


def test_fixed_official_canonical_and_digest_are_stable():
    fields = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "signMethod": "01",
        "txnType": "01",
        "txnSubType": "01",
        "bizType": "000201",
        "channelType": "07",
        "accessType": "0",
        "merId": "777290058213462",
        "orderId": "UPDIFFTEST01",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "currencyCode": "156",
        "frontUrl": "http://localhost:5173/front-fixed",
        "backUrl": "http://www.specialUrl.com",
        "certId": "69903319369",
    }
    canonical = canonicalize_parameters(fields)
    assert canonical == (
        "accessType=0&backUrl=http://www.specialUrl.com&bizType=000201&"
        "certId=69903319369&channelType=07&currencyCode=156&encoding=UTF-8&"
        "frontUrl=http://localhost:5173/front-fixed&merId=777290058213462&"
        "orderId=UPDIFFTEST01&signMethod=01&txnAmt=100&txnSubType=01&"
        "txnTime=20260831120000&txnType=01&version=5.1.0"
    )
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == (
        "92a1928c5b6a087f3963360230f0e62f457a2a914edda5079dfcc60f1cdbd069"
    )


def test_changing_one_signed_field_changes_signature(certificate_fixture):
    material, _certificates = certificate_fixture
    first = sign_parameters({"a": "1", "b": "2"}, material)["signature"]
    changed = sign_parameters({"a": "1", "b": "3"}, material)["signature"]
    assert changed != first


class _HiddenInputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        values = dict(attrs)
        name = values.get("name")
        if values.get("type") == "hidden" and name is not None:
            self.fields[name] = values.get("value") or ""


def test_browser_form_encoding_round_trip_preserves_signed_values(certificate_fixture):
    material, certificates = certificate_fixture
    client = UnionPayTestClient(
        mer_id="777290058213462",
        signing_material=material,
        certificates=certificates,
        front_url="https://merchant.test/return?source=u0&label=测试订单",
        back_url="http://www.specialUrl.com",
    )
    form = client.build_front_payment_form(order_id="UP20260831120000", txn_time="20260831120000")
    parser = _HiddenInputParser()
    parser.feed(form.as_html())
    browser_encoded = urlencode(parser.fields)
    assert dict(parse_qsl(browser_encoded, keep_blank_values=True)) == form.fields


def test_tampered_response_signature_fails(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "encoding": "UTF-8",
        "certId": material.cert_id,
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "respCode": "00",
        "origRespCode": "00",
        "signature": "tampered",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(),
    }
    with pytest.raises(UnionPaySignatureError):
        verify_response_signature(response, certificates)


def test_response_signature_and_chain_are_verified(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "encoding": "UTF-8",
        "certId": material.cert_id,
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(),
    }
    signature = _official_rsa2_signature(material, response)
    response["signature"] = base64.b64encode(signature).decode()
    assert verify_response_signature(response, certificates) is True


def test_response_signature_uses_embedded_certificate_without_cert_id(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode()
    assert verify_response_signature(response, certificates) is True


def test_response_signature_does_not_treat_request_cert_id_as_response_cert_serial(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        # certId identifies the merchant signing certificate in request-oriented
        # messages; the response signature is verified by signPubKeyCert.
        "certId": "69903319369",
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(
            "ascii"
        ),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode("ascii")
    assert str(material.certificate.serial_number) != response["certId"]
    assert verify_response_signature(response, certificates) is True


def test_response_signature_accepts_embedded_pem_certificate(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": material.certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode()
    assert verify_response_signature(response, certificates) is True


def test_browser_form_roundtrip_preserves_pem_and_verifies_response_signature(certificate_fixture):
    material, certificates = certificate_fixture
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "merId": "777290058213462",
        "orderId": "UP202608310001",
        "txnTime": "20260831120000",
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": material.certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode("ascii")

    parsed = parse_form_parameters(urlencode(response).encode("utf-8"))

    assert parsed["signPubKeyCert"] == response["signPubKeyCert"]
    assert verify_response_signature(parsed, certificates) is True


def test_response_parser_preserves_unescaped_base64_plus():
    response = _parse_response_parameters("signature=a+b/c==&signPubKeyCert=x+y==&nested={a=1&b=2}")
    assert response["signature"] == "a+b/c=="
    assert response["signPubKeyCert"] == "x+y=="
    assert response["nested"] == "{a=1&b=2}"


def test_smoke_transaction_uses_shanghai_time_and_integer_cents():
    transaction = new_smoke_transaction(
        now=datetime(2026, 8, 31, 4, 5, 6, tzinfo=timezone.utc),
        amount_cents=SMOKE_AMOUNT_CENTS,
    )
    assert transaction.txn_time == "20260831120506"
    assert transaction.txn_amt == 100
    assert transaction.order_id.startswith("UP20260831120506")
    assert transaction.order_id.isalnum()


def test_naive_smoke_time_is_interpreted_as_shanghai():
    transaction = new_smoke_transaction(now=datetime(2026, 8, 31, 12, 5, 6))
    assert transaction.txn_time == "20260831120506"


def test_front_form_contains_official_u0_fields_and_signed_amount(certificate_fixture):
    material, certificates = certificate_fixture
    client = UnionPayTestClient(
        mer_id="777290058213462",
        signing_material=material,
        certificates=certificates,
    )
    form = client.build_front_payment_form(order_id="UP20260831120000", txn_time="20260831120000")
    assert form.fields["version"] == "5.1.0"
    assert form.fields["txnAmt"] == "100"
    assert form.fields["bizType"] == "000201"
    assert form.fields["certId"] == material.cert_id
    assert form.fields["backUrl"] == "http://www.specialUrl.com"


def test_invalid_order_id_is_rejected(certificate_fixture):
    material, certificates = certificate_fixture
    client = UnionPayTestClient("777290058213462", material, certificates)
    with pytest.raises(UnionPayProtocolError):
        client.build_front_payment_form(order_id="UP-INVALID", txn_time="20260831120000")


@pytest.mark.asyncio
async def test_query_verifies_response_and_checks_transaction_identity(certificate_fixture):
    material, certificates = certificate_fixture
    client = UnionPayTestClient(
        mer_id="777290058213462",
        signing_material=material,
        certificates=certificates,
    )
    order_id = "UP20260831120000"
    txn_time = "20260831120000"
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "certId": material.cert_id,
        "merId": client.mer_id,
        "orderId": order_id,
        "txnTime": txn_time,
        "txnAmt": "100",
        "respCode": "00",
        "origRespCode": "00",
        "queryId": "202608311200001",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(
            "ascii"
        ),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode("ascii")
    captured: dict[str, str] = {}

    class FakeResponse:
        content = "&".join(f"{key}={value}" for key, value in response.items()).encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured.update(data)
            return FakeResponse()

    with patch("infra.unionpay_test.httpx.AsyncClient", return_value=FakeAsyncClient()):
        result = await client.query_transaction(order_id=order_id, txn_time=txn_time)

    assert captured["url"] == client.query_trans_url
    assert captured["txnType"] == "00"
    assert captured["orderId"] == order_id
    assert captured["txnTime"] == txn_time
    assert result.signature_verified is True
    assert result.payment_success is True
    assert result.query_id == "202608311200001"


@pytest.mark.asyncio
async def test_refund_request_uses_back_trans_protocol_and_verifies_response(certificate_fixture):
    material, certificates = certificate_fixture
    client = UnionPayTestClient(
        mer_id="777290058213462",
        signing_material=material,
        certificates=certificates,
    )
    refund_order_id = "RF20260831120000ABCDEF"
    txn_time = "20260831120000"
    original_query_id = "202608311200001"
    response = {
        "version": "5.1.0",
        "encoding": "UTF-8",
        "certId": material.cert_id,
        "merId": client.mer_id,
        "orderId": refund_order_id,
        "txnTime": txn_time,
        "txnAmt": "100",
        "origQryId": original_query_id,
        "respCode": "00",
        "signPubKeyCert": base64.b64encode(material.certificate.public_bytes(serialization.Encoding.DER)).decode(
            "ascii"
        ),
    }
    response["signature"] = base64.b64encode(_official_rsa2_signature(material, response)).decode("ascii")
    captured: dict[str, str] = {}

    class FakeResponse:
        content = "&".join(f"{key}={value}" for key, value in response.items()).encode("utf-8")

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url: str, *, data: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured.update(data)
            return FakeResponse()

    with patch("infra.unionpay_test.httpx.AsyncClient", return_value=FakeAsyncClient()):
        result = await client.refund_transaction(
            order_id=refund_order_id,
            txn_time=txn_time,
            txn_amt=100,
            orig_qry_id=original_query_id,
        )

    assert isinstance(result, UnionPayRefundResult)
    assert captured["url"] == client.back_trans_url
    assert captured["txnType"] == "04"
    assert captured["txnSubType"] == "00"
    assert captured["origQryId"] == original_query_id
    assert result.signature_verified is True
    assert result.resp_code == "00"
