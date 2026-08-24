"""支付宝沙箱协议层测试，不连接真实支付宝或数据库。"""

import base64
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from infra.alipay_sandbox import AlipaySandboxClient
from service.checkout_service import AlipayCallbackRejectedError, process_alipay_callback


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    """序列化临时测试私钥，测试结束后不落盘。"""
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _public_pem(key: rsa.RSAPrivateKey) -> bytes:
    """导出与临时测试私钥对应的公钥。"""
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_page_pay_url_is_signed_by_application_private_key():
    """支付宝收到的 page.pay 参数可用应用公钥验签。"""
    app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alipay_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = AlipaySandboxClient(
        app_id="test-app",
        gateway="https://sandbox.example/gateway.do",
        app_private_key_pem=_private_pem(app_key),
        alipay_public_key_pem=_public_pem(alipay_key),
        notify_url="https://merchant.example/callback",
        return_url="https://merchant.example/return",
    )

    url = client.build_page_pay_url(merchant_payment_no="PM202608240001", amount_cents=449900, subject="测试电脑")
    params = {key: values[-1] for key, values in parse_qs(urlparse(url).query).items()}
    signature = base64.b64decode(params.pop("sign"))

    app_key.public_key().verify(
        signature,
        client._canonical(params).encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert params["method"] == "alipay.trade.page.pay"
    assert '"total_amount":"4499.00"' in params["biz_content"]


def test_callback_signature_is_verified_with_alipay_public_key():
    """支付宝私钥签出的回调能由保存的支付宝公钥验证。"""
    app_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    alipay_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client = AlipaySandboxClient(
        app_id="test-app",
        gateway="https://sandbox.example/gateway.do",
        app_private_key_pem=_private_pem(app_key),
        alipay_public_key_pem=_public_pem(alipay_key),
        notify_url="https://merchant.example/callback",
        return_url="https://merchant.example/return",
    )
    parameters = {
        "app_id": "test-app",
        "notify_id": "notify-1",
        "out_trade_no": "PM202608240001",
        "seller_id": "seller-1",
        "total_amount": "4499.00",
        "trade_no": "202608240001",
        "trade_status": "TRADE_SUCCESS",
    }
    signature = alipay_key.sign(client._canonical(parameters).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    client.verify_callback({**parameters, "sign": base64.b64encode(signature).decode("ascii"), "sign_type": "RSA2"})


@pytest.mark.asyncio
async def test_callback_requires_expected_app_seller_and_amount(monkeypatch):
    """验签后的回调仍必须匹配当前应用、商户及支付金额。"""
    monkeypatch.setattr("service.checkout_service.settings.alipay_sandbox_app_id", "test-app")
    monkeypatch.setattr("service.checkout_service.settings.alipay_sandbox_seller_id", "seller-1")
    client = Mock()
    parameters = {
        "app_id": "test-app",
        "seller_id": "seller-1",
        "out_trade_no": "PM202608240001",
        "trade_no": "trade-1",
        "notify_id": "notify-1",
        "total_amount": "4499.00",
        "trade_status": "TRADE_SUCCESS",
    }
    with (
        patch("service.checkout_service.AlipaySandboxClient.from_settings", return_value=client),
        patch("service.checkout_service.apply_alipay_callback", new=AsyncMock(return_value=True)) as apply_callback,
    ):
        assert await process_alipay_callback(parameters) is True

    client.verify_callback.assert_called_once_with(parameters)
    apply_callback.assert_awaited_once_with(
        merchant_payment_no="PM202608240001",
        provider_trade_no="trade-1",
        provider_callback_id="notify-1",
        amount_cents=449900,
        succeeded=True,
    )


@pytest.mark.asyncio
async def test_callback_rejects_a_different_seller(monkeypatch):
    """即使签名有效，也不能接受另一个商户的回调。"""
    monkeypatch.setattr("service.checkout_service.settings.alipay_sandbox_app_id", "test-app")
    monkeypatch.setattr("service.checkout_service.settings.alipay_sandbox_seller_id", "seller-1")
    client = Mock()
    with patch("service.checkout_service.AlipaySandboxClient.from_settings", return_value=client):
        with pytest.raises(AlipayCallbackRejectedError):
            await process_alipay_callback({"app_id": "test-app", "seller_id": "other"})
