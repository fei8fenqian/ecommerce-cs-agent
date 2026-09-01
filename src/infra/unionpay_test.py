"""独立的银联在线网关支付 5.1.0 测试协议客户端。

该模块只负责证书、签名、验签、前台表单和交易查询，不读取或写入商城订单、
支付、退款或任何业务状态。实际支付卡数据始终只在银联官方测试收银台输入。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization.pkcs12 import load_key_and_certificates

from config import settings

UNIONPAY_VERSION = "5.1.0"
UNIONPAY_ENCODING = "UTF-8"
UNIONPAY_SIGN_METHOD_RSA = "01"
UNIONPAY_FRONT_TEST_URL = "https://gateway.test.95516.com/gateway/api/frontTransReq.do"
UNIONPAY_QUERY_TEST_URL = "https://gateway.test.95516.com/gateway/api/queryTrans.do"
UNIONPAY_BACK_TEST_URL = "https://gateway.test.95516.com/gateway/api/backTransReq.do"
UNIONPAY_NO_BACK_NOTIFICATION_URL = "http://www.specialUrl.com"
UNIONPAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
SMOKE_AMOUNT_CENTS = 100
_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{8,40}$")
_TXN_TIME_PATTERN = re.compile(r"^\d{14}$")


class UnionPayProtocolError(ValueError):
    """银联协议报文、证书或验签不可信。"""


class UnionPayGatewayError(UnionPayProtocolError):
    """银联测试网关不可达或返回非成功 HTTP 状态。"""


class UnionPaySignatureError(UnionPayProtocolError):
    """银联报文签名缺失、证书不可信或验签失败。"""


@dataclass(frozen=True)
class UnionPaySigningMaterial:
    """从商户 PKCS#12 文件加载的最小签名材料。"""

    private_key: rsa.RSAPrivateKey
    certificate: x509.Certificate
    cert_id: str


@dataclass(frozen=True)
class UnionPayCertificateSet:
    """验签链所需的本地 CFCA 根/中间证书及配置的加密证书。"""

    root: x509.Certificate
    middle: x509.Certificate
    encryption: x509.Certificate


@dataclass(frozen=True)
class UnionPayFrontForm:
    """提交到银联测试收银台的一次性 POST 表单。"""

    action: str
    fields: dict[str, str]

    def as_html(self) -> str:
        """生成自动 POST 页面；不向 stdout 打印签名或证书内容。"""
        inputs = "\n".join(
            f'    <input type="hidden" name="{html.escape(key, quote=True)}" value="{html.escape(value, quote=True)}">'
            for key, value in self.fields.items()
        )
        action = html.escape(self.action, quote=True)
        return (
            "<!doctype html>\n"
            '<html lang="zh-CN"><head><meta charset="UTF-8">'
            "<title>UnionPay U0 Test Payment</title></head>\n"
            '<body onload="document.forms[0].submit()">\n'
            f'  <form method="post" action="{action}">\n{inputs}\n  </form>\n'
            "<p>正在跳转银联测试收银台……</p>\n"
            "</body></html>\n"
        )


@dataclass(frozen=True)
class UnionPaySmokeTransaction:
    """一次 U0 独立交易的可复用三元组。"""

    order_id: str
    txn_time: str
    txn_amt: int = SMOKE_AMOUNT_CENTS


@dataclass(frozen=True)
class UnionPayQueryResult:
    """已通过响应验签和交易三元组校验的最小查询结果。"""

    signature_verified: bool
    resp_code: str
    orig_resp_code: str
    query_id: str
    txn_amt: str
    order_id: str
    txn_time: str
    orig_qry_id: str | None = None

    @property
    def payment_success(self) -> bool:
        """只有查询请求和原交易都返回 00 才算支付成功。"""
        return self.signature_verified and self.resp_code == "00" and self.orig_resp_code == "00"


@dataclass(frozen=True)
class UnionPayRefundResult:
    """已通过验签和退款请求身份校验的银联退款响应摘要。"""

    signature_verified: bool
    resp_code: str
    order_id: str
    txn_time: str
    txn_amt: str
    orig_qry_id: str | None = None
    query_id: str | None = None


def _resolve_path(path_value: str) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return Path(__file__).resolve().parents[2] / path


def _load_certificate(path: Path) -> x509.Certificate:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise UnionPayProtocolError("银联证书文件不可读") from exc
    try:
        return x509.load_der_x509_certificate(raw)
    except ValueError:
        try:
            return x509.load_pem_x509_certificate(raw)
        except ValueError as exc:
            raise UnionPayProtocolError("银联证书格式无效") from exc


def load_signing_material(cert_path: Path, password: str) -> UnionPaySigningMaterial:
    """加载 PFX 私钥和叶子证书，并以十进制 serial 作为 certId。"""
    try:
        private_key, certificate, _chain = load_key_and_certificates(cert_path.read_bytes(), password.encode())
    except (OSError, TypeError, ValueError) as exc:
        raise UnionPayProtocolError("银联签名 PFX 不可用") from exc
    if not isinstance(private_key, rsa.RSAPrivateKey) or certificate is None:
        raise UnionPayProtocolError("银联签名 PFX 缺少 RSA 私钥或叶子证书")
    return UnionPaySigningMaterial(
        private_key=private_key,
        certificate=certificate,
        cert_id=str(certificate.serial_number),
    )


def canonicalize_parameters(parameters: Mapping[str, str | None]) -> str:
    """按官方规范生成待签名串。

    ``signature`` 不参与自身签名；其余存在于报文中的字段按 key 的 ASCII 顺序
    排序，空字符串保留为 ``key=``。特殊字符和中文保持原样，URL 编码只发生在
    HTTP 表单传输层。
    """
    pairs = [(str(key), value) for key, value in parameters.items() if str(key) != "signature" and value is not None]
    return "&".join(f"{key}={value}" for key, value in sorted(pairs, key=lambda item: item[0]))


def sign_parameters(
    parameters: Mapping[str, str | None],
    material: UnionPaySigningMaterial,
    *,
    encoding: str = UNIONPAY_ENCODING,
) -> dict[str, str]:
    """按银联官方 SDK 5.1.0 ``signRsa2`` 流程签名。

    官方 SDK 并不是直接对 canonical string 调用 ``SHA256withRSA``。它先对
    canonical string 做 SHA-256，取小写十六进制字符串，再把这 64 个 ASCII
    字节交给 ``SHA256withRSA``。请求入口也复现官方 ``filterBlank``：空白值
    不进入报文，其余值先 ``trim``。
    """
    data = {}
    for key, value in parameters.items():
        name = str(key)
        if name == "signature" or value is None:
            continue
        text = str(value)
        if not text.strip():
            continue
        data[name] = text.strip()
    data["certId"] = material.cert_id
    signable = canonicalize_parameters(data).encode(encoding)
    digest_hex = hashlib.sha256(signable).hexdigest().lower()
    signature_input = digest_hex.encode(encoding)
    signature = material.private_key.sign(signature_input, padding.PKCS1v15(), hashes.SHA256())
    data["signature"] = base64.b64encode(signature).decode("ascii")
    return data


def _certificate_time(cert: x509.Certificate, field: str) -> datetime:
    utc_name = f"{field}_utc"
    value = getattr(cert, utc_name, None)
    if value is not None:
        return value
    return getattr(cert, field).replace(tzinfo=timezone.utc)


def _ensure_current_certificate(cert: x509.Certificate) -> None:
    now = datetime.now(timezone.utc)
    if not (_certificate_time(cert, "not_valid_before") <= now <= _certificate_time(cert, "not_valid_after")):
        raise UnionPaySignatureError("银联响应签名证书已过期或尚未生效")


def _verify_certificate_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise UnionPaySignatureError("银联证书链不是 RSA 证书")
    signature_hash_algorithm = child.signature_hash_algorithm
    if signature_hash_algorithm is None:
        raise UnionPaySignatureError("银联证书签名算法不可用")
    try:
        public_key.verify(
            child.signature,
            child.tbs_certificate_bytes,
            padding.PKCS1v15(),
            signature_hash_algorithm,
        )
    except Exception as exc:
        raise UnionPaySignatureError("银联响应签名证书链无效") from exc


def _verify_unionpay_certificate_chain(
    leaf: x509.Certificate,
    certificates: UnionPayCertificateSet,
) -> None:
    """验证响应叶子证书到本地 CFCA 根/中间证书的信任链。"""
    for cert in (certificates.root, certificates.middle):
        _ensure_current_certificate(cert)
    _ensure_current_certificate(leaf)

    if leaf.issuer == certificates.middle.subject:
        _verify_certificate_signature(leaf, certificates.middle)
    elif leaf.issuer == certificates.root.subject:
        _verify_certificate_signature(leaf, certificates.root)
    else:
        raise UnionPaySignatureError("银联响应证书 issuer 不在本地信任链中")

    if certificates.middle.issuer != certificates.root.subject:
        raise UnionPaySignatureError("本地银联中间证书不属于配置根证书")
    _verify_certificate_signature(certificates.middle, certificates.root)


def _decode_response_certificate(value: str) -> x509.Certificate:
    stripped_text = value.strip()

    def encoding_summary() -> str:
        compact = "".join(stripped_text.split())
        standard_chars = bool(re.fullmatch(r"[A-Za-z0-9+/=]*", compact))
        urlsafe_chars = bool(re.fullmatch(r"[A-Za-z0-9_\-=]*", compact))
        return (
            f"len={len(stripped_text)},compact_len={len(compact)},mod4={len(compact) % 4},"
            f"pem={stripped_text.startswith('-----BEGIN CERTIFICATE-----')},"
            f"percent={stripped_text.count('%')},whitespace={len(stripped_text) - len(compact)},"
            f"standard_base64_chars={standard_chars},urlsafe_base64_chars={urlsafe_chars}"
        )

    if "-----BEGIN CERTIFICATE-----" in stripped_text:
        try:
            return x509.load_pem_x509_certificate(stripped_text.encode("ascii"))
        except (UnicodeEncodeError, ValueError) as exc:
            raise UnionPaySignatureError(f"银联响应签名证书格式无效（{encoding_summary()}）") from exc
    try:
        # 网关也可能返回 Base64(DER)；兼容传输层插入的空白，但不接受
        # base64.b64decode 的宽松垃圾字符模式。
        stripped = "".join(stripped_text.split()).encode("ascii")
        raw = base64.b64decode(stripped, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise UnionPaySignatureError(f"银联响应签名证书编码无效（{encoding_summary()}）") from exc
    try:
        return x509.load_der_x509_certificate(raw)
    except ValueError:
        try:
            return x509.load_pem_x509_certificate(raw)
        except ValueError as exc:
            raise UnionPaySignatureError(f"银联响应签名证书格式无效（{encoding_summary()}）") from exc


def verify_response_signature(
    response: Mapping[str, str | None],
    certificates: UnionPayCertificateSet,
) -> bool:
    """按官方响应字段验签并验证响应中的银联签名证书。

    RSA 验签优先使用响应默认携带的 ``signPubKeyCert``；如果响应不带该字段，
    U0 选择 fail closed，而不是猜测或信任未知本地证书。
    """
    signature_text = response.get("signature")
    cert_text = response.get("signPubKeyCert")
    if not signature_text or not cert_text:
        raise UnionPaySignatureError("银联响应缺少验签所需字段")
    try:
        signature = base64.b64decode(signature_text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise UnionPaySignatureError("银联响应签名编码无效") from exc

    if cert_text:
        certificate = _decode_response_certificate(cert_text)
        _verify_unionpay_certificate_chain(certificate, certificates)
    encoding = str(response.get("encoding") or UNIONPAY_ENCODING)
    try:
        signable = canonicalize_parameters(response).encode(encoding)
    except (LookupError, UnicodeEncodeError) as exc:
        raise UnionPaySignatureError("银联响应编码无效") from exc
    public_key = certificate.public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise UnionPaySignatureError("银联响应签名公钥不是 RSA")
    digest_hex = hashlib.sha256(signable).hexdigest().lower()
    signature_input = digest_hex.encode(encoding)
    try:
        public_key.verify(signature, signature_input, padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:
        raise UnionPaySignatureError("银联响应验签失败") from exc
    return True


def validate_order_id(order_id: str) -> str:
    if not _ORDER_ID_PATTERN.fullmatch(order_id):
        raise UnionPayProtocolError("orderId 必须是 8-40 位字母数字且不能含连字符")
    return order_id


def validate_txn_time(txn_time: str) -> str:
    if not _TXN_TIME_PATTERN.fullmatch(txn_time):
        raise UnionPayProtocolError("txnTime 必须是 YYYYMMDDhhmmss")
    return txn_time


def new_smoke_transaction(
    *, now: datetime | None = None, amount_cents: int = SMOKE_AMOUNT_CENTS
) -> UnionPaySmokeTransaction:
    """生成 U0 唯一交易；时间统一使用 Asia/Shanghai。"""
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise UnionPayProtocolError("txnAmt 必须是正整数分")
    supplied_now = now or datetime.now(UNIONPAY_TIMEZONE)
    if supplied_now.tzinfo is None:
        supplied_now = supplied_now.replace(tzinfo=UNIONPAY_TIMEZONE)
    local_now = supplied_now.astimezone(UNIONPAY_TIMEZONE)
    txn_time = local_now.strftime("%Y%m%d%H%M%S")
    order_id = f"UP{txn_time}{secrets.token_hex(4).upper()}"
    validate_order_id(order_id)
    return UnionPaySmokeTransaction(order_id=order_id, txn_time=txn_time, txn_amt=amount_cents)


@dataclass
class UnionPayTestClient:
    """只面向银联测试网关的协议客户端，不承载商城业务状态。"""

    mer_id: str
    signing_material: UnionPaySigningMaterial
    certificates: UnionPayCertificateSet
    front_url: str = "http://localhost:5173/"
    back_url: str = UNIONPAY_NO_BACK_NOTIFICATION_URL
    front_trans_url: str = UNIONPAY_FRONT_TEST_URL
    query_trans_url: str = UNIONPAY_QUERY_TEST_URL
    back_trans_url: str = UNIONPAY_BACK_TEST_URL
    timeout_seconds: float = 30.0

    @classmethod
    def from_settings(cls) -> "UnionPayTestClient":
        if not settings.unionpay_mer_id:
            raise UnionPayProtocolError("银联测试商户号未配置")
        password = settings.unionpay_sign_cert_password.get_secret_value()
        if not password:
            raise UnionPayProtocolError("银联签名证书密码未配置")
        signing = load_signing_material(_resolve_path(settings.unionpay_sign_cert_path), password)
        certificates = UnionPayCertificateSet(
            root=_load_certificate(_resolve_path(settings.unionpay_root_cert_path)),
            middle=_load_certificate(_resolve_path(settings.unionpay_middle_cert_path)),
            # U0 的 PC B2C 表单不发送敏感账户字段；仍加载配置以确保本地证书包完整，
            # 但不把该证书当作响应签名证书，也不在这里实现加密业务。
            encryption=_load_certificate(_resolve_path(settings.unionpay_encrypt_cert_path)),
        )
        return cls(
            mer_id=settings.unionpay_mer_id,
            signing_material=signing,
            certificates=certificates,
            front_url=settings.unionpay_front_url,
            back_url=settings.unionpay_back_url,
            front_trans_url=settings.unionpay_front_gateway,
            query_trans_url=settings.unionpay_query_gateway,
            back_trans_url=settings.unionpay_back_gateway,
            timeout_seconds=settings.unionpay_timeout_seconds,
        )

    def build_front_payment_form(
        self,
        *,
        order_id: str,
        txn_time: str,
        txn_amt: int = SMOKE_AMOUNT_CENTS,
        front_url: str | None = None,
    ) -> UnionPayFrontForm:
        validate_order_id(order_id)
        validate_txn_time(txn_time)
        if isinstance(txn_amt, bool) or not isinstance(txn_amt, int) or txn_amt <= 0:
            raise UnionPayProtocolError("txnAmt 必须是正整数分")
        data = {
            "version": UNIONPAY_VERSION,
            "encoding": UNIONPAY_ENCODING,
            "signMethod": UNIONPAY_SIGN_METHOD_RSA,
            "txnType": "01",
            "txnSubType": "01",
            "bizType": "000201",
            "channelType": "07",
            "accessType": "0",
            "merId": self.mer_id,
            "orderId": order_id,
            "txnTime": txn_time,
            "txnAmt": str(txn_amt),
            "currencyCode": "156",
            "frontUrl": front_url if front_url is not None else self.front_url,
            "backUrl": self.back_url,
        }
        return UnionPayFrontForm(action=self.front_trans_url, fields=sign_parameters(data, self.signing_material))

    async def query_transaction(self, *, order_id: str, txn_time: str) -> UnionPayQueryResult:
        """查询一笔 U0 交易，先验签再校验 merchant/order/time 三元组。"""
        validate_order_id(order_id)
        validate_txn_time(txn_time)
        data = {
            "version": UNIONPAY_VERSION,
            "encoding": UNIONPAY_ENCODING,
            "signMethod": UNIONPAY_SIGN_METHOD_RSA,
            "txnType": "00",
            "txnSubType": "00",
            "bizType": "000201",
            "accessType": "0",
            "merId": self.mer_id,
            "orderId": order_id,
            "txnTime": txn_time,
        }
        request_data = sign_parameters(data, self.signing_material)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.query_trans_url, data=request_data)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UnionPayGatewayError("银联 queryTrans 请求失败") from exc

        body = _decode_http_body(response.content)
        response_data = _parse_response_parameters(body)
        if not response_data:
            raise UnionPayProtocolError("银联 queryTrans 返回空报文")
        verify_response_signature(response_data, self.certificates)
        if (
            response_data.get("merId") != self.mer_id
            or response_data.get("orderId") != order_id
            or response_data.get("txnTime") != txn_time
        ):
            raise UnionPayProtocolError("银联 queryTrans 返回交易三元组不匹配")
        return UnionPayQueryResult(
            signature_verified=True,
            resp_code=response_data.get("respCode", ""),
            orig_resp_code=response_data.get("origRespCode", ""),
            query_id=response_data.get("queryId", ""),
            txn_amt=response_data.get("txnAmt", ""),
            order_id=response_data["orderId"],
            txn_time=response_data["txnTime"],
            orig_qry_id=response_data.get("origQryId"),
        )

    async def refund_transaction(
        self,
        *,
        order_id: str,
        txn_time: str,
        txn_amt: int,
        orig_qry_id: str,
    ) -> UnionPayRefundResult:
        """向银联测试网关提交一笔全额退款请求并返回已验签响应。

        该方法只负责 5.1.0 ``backTransReq`` 协议，不改变商城支付或退款状态。
        调用方必须在后续 queryTrans 结果再次完成最终收敛。
        """
        validate_order_id(order_id)
        validate_txn_time(txn_time)
        if isinstance(txn_amt, bool) or not isinstance(txn_amt, int) or txn_amt <= 0:
            raise UnionPayProtocolError("txnAmt 必须是正整数分")
        if not isinstance(orig_qry_id, str) or not orig_qry_id.strip():
            raise UnionPayProtocolError("origQryId 不可为空")
        data = {
            "version": UNIONPAY_VERSION,
            "encoding": UNIONPAY_ENCODING,
            "signMethod": UNIONPAY_SIGN_METHOD_RSA,
            "txnType": "04",
            "txnSubType": "00",
            "bizType": "000201",
            "channelType": "07",
            "accessType": "0",
            "merId": self.mer_id,
            "orderId": order_id,
            "txnTime": txn_time,
            "txnAmt": str(txn_amt),
            "currencyCode": "156",
            "origQryId": orig_qry_id,
            "backUrl": self.back_url,
        }
        request_data = sign_parameters(data, self.signing_material)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.back_trans_url, data=request_data)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UnionPayGatewayError("银联退款请求失败") from exc

        body = _decode_http_body(response.content)
        response_data = _parse_response_parameters(body)
        if not response_data:
            raise UnionPayProtocolError("银联退款返回空报文")
        verify_response_signature(response_data, self.certificates)
        if (
            response_data.get("merId") != self.mer_id
            or response_data.get("orderId") != order_id
            or response_data.get("txnTime") != txn_time
        ):
            raise UnionPayProtocolError("银联退款返回交易三元组不匹配")
        returned_amount = response_data.get("txnAmt", "")
        if returned_amount != str(txn_amt):
            raise UnionPayProtocolError("银联退款返回金额不匹配")
        returned_orig_qry_id = response_data.get("origQryId")
        if returned_orig_qry_id != orig_qry_id:
            raise UnionPayProtocolError("银联退款返回原支付交易不匹配")
        return UnionPayRefundResult(
            signature_verified=True,
            resp_code=response_data.get("respCode", ""),
            order_id=response_data["orderId"],
            txn_time=response_data["txnTime"],
            txn_amt=returned_amount,
            orig_qry_id=returned_orig_qry_id,
            query_id=response_data.get("queryId"),
        )


def _decode_http_body(content: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UnionPayProtocolError("银联响应编码无法解析")


def _parse_response_parameters(body: str) -> dict[str, str]:
    """按官方 SDK ``parseRespString`` 解析 5.1 响应，保留 Base64 的 ``+``。

    5.1 网关响应不是标准 URL-form 编码。使用 ``parse_qsl`` 会把未转义的
    ``+`` 误解为空格，破坏 ``signature`` 和 ``signPubKeyCert``。这里复现官方
    解析器的关键行为：仅按顶层 ``&``/``=`` 分隔，不做 URL 解码，并允许值中
    出现成对的 ``{...}`` 或 ``[...]``。
    """
    if not body:
        return {}
    result: dict[str, str] = {}
    buffer: list[str] = []
    key: str | None = None
    closing: str | None = None

    def put_current() -> None:
        nonlocal key, buffer
        if key is None:
            if buffer:
                result["".join(buffer)] = ""
        else:
            result[key] = "".join(buffer)
        key = None
        buffer = []

    for character in body:
        if key is None:
            if character == "=":
                key = "".join(buffer)
                buffer = []
            else:
                buffer.append(character)
            continue

        if closing is not None:
            if character == closing:
                closing = None
        elif character == "{":
            closing = "}"
        elif character == "[":
            closing = "]"

        if character == "&" and closing is None:
            put_current()
        else:
            buffer.append(character)

    put_current()
    return result


def parse_form_parameters(body: bytes, *, encoding: str = UNIONPAY_ENCODING) -> dict[str, str]:
    """解析银联前台 ``application/x-www-form-urlencoded`` 回跳报文。

    前台回跳是浏览器标准表单语义：``+`` 表示空格，字面量 ``+`` 必须由
    ``%2B`` 传输。所有字段统一使用标准解析，避免把 PEM 头部的
    ``BEGIN+CERTIFICATE`` 错误保留下来。重复字段也拒绝，防止 public endpoint
    出现 last-value-wins 的歧义。
    """
    try:
        text = body.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise UnionPayProtocolError("银联前台回跳编码无法解析") from exc
    if not text:
        return {}

    try:
        pairs = parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            encoding=encoding,
            errors="strict",
            max_num_fields=256,
        )
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise UnionPayProtocolError("银联前台回跳字段编码无效") from exc
    result: dict[str, str] = {}
    for key, value in pairs:
        if key in result:
            raise UnionPayProtocolError("银联前台回跳包含重复字段")
        result[key] = value
    return result
