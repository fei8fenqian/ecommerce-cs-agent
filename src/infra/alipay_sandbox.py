"""支付宝沙箱的请求签名和回调验签，不承载订单状态变化。"""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config import settings
from exceptions import DependencyUnavailableError

logger = logging.getLogger(__name__)


class AlipaySignatureError(ValueError):
    """支付宝参数签名不可信或参数格式不完整。"""


class AlipayGatewayError(ValueError):
    """支付宝网关未返回可用于本地状态收敛的交易结果。"""


class AlipayTradeNotFoundError(AlipayGatewayError):
    """本地待支付单未曾在支付宝侧成功创建交易。"""


class AlipayRefundRejectedError(AlipayGatewayError):
    """支付宝明确拒绝一笔退款；这与网络超时的未知结果不同。"""


@dataclass(frozen=True)
class AlipayPagePayForm:
    """浏览器提交到支付宝收银台的一次性表单。"""

    action: str
    fields: dict[str, str]

    @property
    def url(self) -> str:
        """保留 GET URL 作为旧客户端的兼容回退。"""
        separator = "&" if "?" in self.action else "?"
        return f"{self.action}{separator}{urlencode(self.fields)}"


@dataclass(frozen=True)
class AlipaySandboxClient:
    """仅处理支付宝沙箱协议，不直接写入订单或支付表。"""

    app_id: str
    gateway: str
    app_private_key_pem: bytes
    alipay_public_key_pem: bytes
    notify_url: str
    return_url: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_settings(cls) -> "AlipaySandboxClient":
        """从受控配置和本机密钥文件创建客户端。

        Raises:
            DependencyUnavailableError: 沙箱尚未完成 APPID、回调地址或密钥配置。
        """
        required = (
            settings.alipay_sandbox_app_id,
            settings.alipay_sandbox_seller_id,
            settings.alipay_sandbox_app_private_key_path,
            settings.alipay_sandbox_public_key_path,
            settings.alipay_sandbox_notify_url,
            settings.alipay_sandbox_return_url,
        )
        if not all(required):
            raise DependencyUnavailableError("支付宝沙箱尚未配置")
        try:
            client = cls(
                app_id=settings.alipay_sandbox_app_id,
                gateway=settings.alipay_sandbox_gateway,
                app_private_key_pem=Path(settings.alipay_sandbox_app_private_key_path).read_bytes(),
                alipay_public_key_pem=Path(settings.alipay_sandbox_public_key_path).read_bytes(),
                notify_url=settings.alipay_sandbox_notify_url,
                return_url=settings.alipay_sandbox_return_url,
                timeout_seconds=settings.alipay_sandbox_timeout_seconds,
            )
            client._load_app_private_key()
            client._load_alipay_public_key()
            return client
        except (OSError, TypeError, ValueError) as exc:
            raise DependencyUnavailableError("支付宝沙箱密钥不可用") from exc

    def build_page_pay_url(
        self,
        *,
        merchant_payment_no: str,
        amount_cents: int,
        subject: str,
        return_url: str | None = None,
    ) -> str:
        """构建已 RSA2 签名的电脑网站支付跳转链接。

        新客户端优先使用 :meth:`build_page_pay_form` 的 POST 表单；保留此方法
        是为了兼容已有订单页和外部调用方。
        """
        return self.build_page_pay_form(
            merchant_payment_no=merchant_payment_no,
            amount_cents=amount_cents,
            subject=subject,
            return_url=return_url,
        ).url

    def build_page_pay_form(
        self,
        *,
        merchant_payment_no: str,
        amount_cents: int,
        subject: str,
        return_url: str | None = None,
    ) -> AlipayPagePayForm:
        """构建支付宝电脑网站支付的浏览器 POST 表单数据。

        Args:
            merchant_payment_no: 本系统生成的唯一商户交易号。
            amount_cents: 服务端核验后的 CNY 整数分金额。
            subject: 客户可见的订单标题。
            return_url: 支付完成后的受控浏览器回跳地址。

        Returns:
            可直接由浏览器 ``form.submit()`` 提交的 action 和签名字段。
        """
        parameters = self._build_common_parameters("alipay.trade.page.pay")
        parameters.update(
            {
                "return_url": return_url or self.return_url,
                "biz_content": json.dumps(
                    {
                        "out_trade_no": merchant_payment_no,
                        "product_code": "FAST_INSTANT_TRADE_PAY",
                        "total_amount": self._format_amount(amount_cents),
                        # 沙箱的 page.pay 对长爬虫标题和临时回调地址都不稳定；
                        # 商品明细仍由本地订单页展示，收银台只需稳定的订单概览。
                        "subject": "Geex Digital Order",
                    },
                    # 收银台标题固定为 ASCII，避免浏览器表单编码改变签名串。
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            }
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        # 页面支付完成后以前端回跳为触发点，由服务端 query_trade 收敛状态。
        # 沙箱的临时 trycloudflare 回调会使其在创建收银台时重定向到 /error，故不
        # 传 notify_url；生产适配器必须改用稳定、可公网访问的异步回调地址。
        # charset 必须留在 action 查询字符串中，同时参与原始签名。
        charset = parameters.pop("charset")
        return AlipayPagePayForm(action=f"{self.gateway}?charset={charset}", fields=parameters)

    async def precreate_trade(
        self,
        *,
        merchant_payment_no: str,
        amount_cents: int,
        subject: str,
    ) -> str:
        """创建支付宝当面付交易，并返回仅供前端生成二维码的内容。

        Args:
            merchant_payment_no: 本系统生成的唯一商户交易号。
            amount_cents: 服务端核验后的 CNY 整数分金额。
            subject: 收银台和沙箱钱包展示的简短商品标题。

        Returns:
            支付宝返回的 ``qr_code`` 字符串。它不是支付成功凭据，付款结果仍需
            通过 :meth:`query_trade` 确认。

        Raises:
            AlipayGatewayError: 网关不可达、拒绝创建交易或未返回二维码。
        """
        parameters = self._build_common_parameters("alipay.trade.precreate")
        parameters.update(
            {
                "biz_content": json.dumps(
                    {
                        "out_trade_no": merchant_payment_no,
                        "total_amount": self._format_amount(amount_cents),
                        "subject": subject[:128],
                        "timeout_express": "15m",
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
            }
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.gateway, data=parameters)
                response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except UnicodeDecodeError:
                payload = json.loads(response.content.decode("gbk"))
            result = payload.get("alipay_trade_precreate_response")
            if not isinstance(result, dict) or result.get("code") != "10000":
                logger.warning(
                    "Alipay precreate rejected",
                    extra={
                        "gateway_code": str(result.get("code", "")) if isinstance(result, dict) else "",
                        "gateway_sub_code": str(result.get("sub_code", "")) if isinstance(result, dict) else "",
                    },
                )
                raise AlipayGatewayError("支付宝暂时无法创建二维码付款")
            qr_code = result.get("qr_code")
            if not isinstance(qr_code, str) or not qr_code:
                raise AlipayGatewayError("支付宝未返回付款二维码")
            return qr_code
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, AlipayGatewayError):
                raise
            logger.warning("Alipay precreate request failed", extra={"failure_type": type(exc).__name__})
            raise AlipayGatewayError("支付宝暂时无法创建二维码付款") from exc

    async def query_trade(self, merchant_payment_no: str) -> Mapping[str, object]:
        """查询支付宝侧交易事实，用于回跳或通知缺失时的状态收敛。

        Args:
            merchant_payment_no: 本系统生成并发送给支付宝的商户交易号。

        Returns:
            已通过网关基础响应校验的支付宝交易数据。

        Raises:
            AlipayGatewayError: 网关不可达、响应无效或交易查询未成功。
        """
        parameters = self._build_common_parameters("alipay.trade.query")
        parameters["biz_content"] = json.dumps(
            {"out_trade_no": merchant_payment_no}, ensure_ascii=True, separators=(",", ":")
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.gateway, data=parameters)
                response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except UnicodeDecodeError:
                # 支付宝沙箱的部分错误响应仍使用 GBK，即使请求参数声明 utf-8。
                payload = json.loads(response.content.decode("gbk"))
            result = payload.get("alipay_trade_query_response")
            if not isinstance(result, dict):
                raise AlipayGatewayError("支付宝暂时无法确认交易状态")
            if result.get("code") != "10000":
                logger.warning(
                    "Alipay trade query rejected",
                    extra={
                        "gateway_code": str(result.get("code", "")),
                        "gateway_sub_code": str(result.get("sub_code", "")),
                    },
                )
                if result.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
                    raise AlipayTradeNotFoundError("支付宝未找到该交易")
                raise AlipayGatewayError("支付宝暂时无法确认交易状态")
            return result
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, AlipayGatewayError):
                raise
            logger.warning(
                "Alipay trade query request failed",
                extra={"failure_type": type(exc).__name__},
            )
            raise AlipayGatewayError("支付宝暂时无法确认交易状态") from exc

    async def close_trade(self, merchant_payment_no: str) -> None:
        """关闭一笔尚未支付的支付宝交易。

        支付宝还没有创建该交易时也视为可安全取消：本地订单不会再被继续支付。
        """
        parameters = self._build_common_parameters("alipay.trade.close")
        parameters["biz_content"] = json.dumps(
            {"out_trade_no": merchant_payment_no}, ensure_ascii=True, separators=(",", ":")
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.gateway, data=parameters)
                response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except UnicodeDecodeError:
                payload = json.loads(response.content.decode("gbk"))
            result = payload.get("alipay_trade_close_response")
            if not isinstance(result, dict):
                raise AlipayGatewayError("支付宝暂时无法关闭交易")
            if result.get("code") == "10000" or result.get("sub_code") == "ACQ.TRADE_NOT_EXIST":
                return
            raise AlipayGatewayError("支付宝暂时无法关闭交易")
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, AlipayGatewayError):
                raise
            logger.warning("Alipay trade close request failed", extra={"failure_type": type(exc).__name__})
            raise AlipayGatewayError("支付宝暂时无法关闭交易") from exc

    async def refund_trade(
        self,
        *,
        merchant_payment_no: str,
        merchant_refund_no: str,
        amount_cents: int,
    ) -> Mapping[str, object]:
        """提交一笔全额退款并返回经网关基础校验的结果。

        Args:
            merchant_payment_no: 原支付请求的商户交易号。
            merchant_refund_no: 本系统生成且全局唯一的商户退款号。
            amount_cents: 仅支持 CNY 整数分，由应用服务核验为原支付全额。

        Returns:
            支付宝 ``alipay_trade_refund_response`` 的业务字段。

        Raises:
            AlipayRefundRejectedError: 网关明确返回业务拒绝。
            AlipayGatewayError: 网络或网关结果不可信，调用方必须保留处理中状态。
        """
        parameters = self._build_common_parameters("alipay.trade.refund")
        parameters["biz_content"] = json.dumps(
            {
                "out_trade_no": merchant_payment_no,
                "out_request_no": merchant_refund_no,
                "refund_amount": self._format_amount(amount_cents),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.gateway, data=parameters)
                response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except UnicodeDecodeError:
                payload = json.loads(response.content.decode("gbk"))
            result = payload.get("alipay_trade_refund_response")
            if not isinstance(result, dict):
                raise AlipayGatewayError("支付宝退款结果无效")
            if result.get("code") != "10000":
                logger.warning(
                    "Alipay refund rejected",
                    extra={
                        "gateway_code": str(result.get("code", "")),
                        "gateway_sub_code": str(result.get("sub_code", "")),
                    },
                )
                raise AlipayRefundRejectedError("支付宝拒绝退款")
            return result
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, AlipayGatewayError):
                raise
            logger.warning("Alipay refund request failed", extra={"failure_type": type(exc).__name__})
            raise AlipayGatewayError("支付宝退款结果暂时不可用") from exc

    async def query_refund(
        self,
        *,
        merchant_payment_no: str,
        merchant_refund_no: str,
    ) -> Mapping[str, object]:
        """查询一笔已提交退款的支付宝侧状态。

        该方法只读取支付宝结果。调用方根据返回状态在本地确定性地收敛退款和订单，
        因此网关不可达时不能把本地 ``PROCESSING`` 误改为失败或重新提交退款。

        Args:
            merchant_payment_no: 原支付请求的商户交易号。
            merchant_refund_no: 本系统生成的商户退款号。

        Returns:
            ``alipay_trade_fastpay_refund_query_response`` 的业务字段。

        Raises:
            AlipayGatewayError: 网关不可达、响应无效或未能可靠查询退款。
        """
        parameters = self._build_common_parameters("alipay.trade.fastpay.refund.query")
        parameters["biz_content"] = json.dumps(
            {
                "out_trade_no": merchant_payment_no,
                "out_request_no": merchant_refund_no,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        parameters["sign"] = self._sign(self._canonical(parameters))
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.gateway, data=parameters)
                response.raise_for_status()
            try:
                payload = json.loads(response.content.decode("utf-8"))
            except UnicodeDecodeError:
                payload = json.loads(response.content.decode("gbk"))
            result = payload.get("alipay_trade_fastpay_refund_query_response")
            if not isinstance(result, dict) or result.get("code") != "10000":
                logger.warning(
                    "Alipay refund query unavailable",
                    extra={
                        "gateway_code": str(result.get("code", "")) if isinstance(result, dict) else "",
                        "gateway_sub_code": str(result.get("sub_code", "")) if isinstance(result, dict) else "",
                    },
                )
                raise AlipayGatewayError("支付宝暂时无法确认退款状态")
            return result
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            if isinstance(exc, AlipayGatewayError):
                raise
            logger.warning("Alipay refund query request failed", extra={"failure_type": type(exc).__name__})
            raise AlipayGatewayError("支付宝暂时无法确认退款状态") from exc

    def verify_callback(self, parameters: Mapping[str, str]) -> None:
        """验证支付宝回调签名；调用方仍需核验订单、金额和应用 ID。"""
        signature = parameters.get("sign")
        if not signature:
            raise AlipaySignatureError("missing signature")
        public_key = self._load_alipay_public_key()
        signable = {key: value for key, value in parameters.items() if key not in {"sign", "sign_type"}}
        try:
            public_key.verify(
                base64.b64decode(signature),
                self._canonical(signable).encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (ValueError, TypeError) as exc:
            raise AlipaySignatureError("invalid signature") from exc
        except Exception as exc:
            raise AlipaySignatureError("invalid signature") from exc

    def _sign(self, content: str) -> str:
        private_key = self._load_app_private_key()
        signature = private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("ascii")

    def _build_common_parameters(self, method: str) -> dict[str, str]:
        """构造所有支付宝网关请求共同的签名参数。"""
        return {
            "app_id": self.app_id,
            "method": method,
            "format": "JSON",
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
        }

    def _load_app_private_key(self) -> rsa.RSAPrivateKey:
        """加载并限制应用签名密钥为支付宝要求的 RSA 私钥。"""
        private_key = serialization.load_pem_private_key(self.app_private_key_pem, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("unexpected private key type")
        return private_key

    def _load_alipay_public_key(self) -> rsa.RSAPublicKey:
        """加载并限制回调验签密钥为支付宝 RSA 公钥。"""
        public_key = serialization.load_pem_public_key(self.alipay_public_key_pem)
        if not isinstance(public_key, rsa.RSAPublicKey):
            raise AlipaySignatureError("unexpected public key type")
        return public_key

    @staticmethod
    def _canonical(parameters: Mapping[str, str]) -> str:
        return "&".join(f"{key}={value}" for key, value in sorted(parameters.items()) if value is not None)

    @staticmethod
    def _format_amount(amount_cents: int) -> str:
        return f"{amount_cents // 100}.{amount_cents % 100:02d}"
