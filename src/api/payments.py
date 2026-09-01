"""支付渠道回报入口；浏览器回跳永远不是支付成功 authority。"""

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from infra.unionpay_test import UnionPayProtocolError, parse_form_parameters
from service.checkout_service import (
    AlipayCallbackRejectedError,
    UnionPayFrontReturnOutcome,
    build_unionpay_customer_return_url,
    process_alipay_callback,
    process_unionpay_front_return,
)

logger = logging.getLogger(__name__)
payment_router = APIRouter(prefix="/api/v1/payments", tags=["沙箱支付"])


@payment_router.post("/alipay/callback", response_class=PlainTextResponse)
async def alipay_callback(request: Request) -> PlainTextResponse:
    """接收支付宝表单回调，验签通过后才允许本地支付状态推进。"""
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw_body, keep_blank_values=True)
    parameters = {key: values[-1] for key, values in parsed.items() if values}
    try:
        await process_alipay_callback(parameters)
    except AlipayCallbackRejectedError:
        return PlainTextResponse("failure", status_code=400)
    return PlainTextResponse("success")


@payment_router.api_route("/unionpay/front-return", methods=["GET", "POST"])
async def unionpay_front_return(request: Request) -> RedirectResponse:
    """接收银联前台回跳，验签后主动 queryTrans，再安全 303 回商城。"""
    parameters: dict[str, str] = {}
    try:
        if request.method == "POST":
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            raw_body = await request.body()
            if content_type == "application/x-www-form-urlencoded" and len(raw_body) <= 64 * 1024:
                parameters = parse_form_parameters(raw_body)
            elif content_type != "application/x-www-form-urlencoded":
                logger.warning("UnionPay front return rejected unsupported content type")
            else:
                logger.warning("UnionPay front return body too large")
        else:
            raw_query = request.scope.get("query_string", b"")
            if isinstance(raw_query, bytes) and len(raw_query) <= 64 * 1024:
                parameters = parse_form_parameters(raw_query)
    except UnionPayProtocolError as exc:
        logger.warning("UnionPay front return parameters rejected", extra={"failure_type": type(exc).__name__})

    try:
        outcome = await process_unionpay_front_return(parameters)
    except Exception as exc:
        # 公共回跳不能把用户留在 500 白页；支付状态仍保持本地原值，订单页稍后可重试。
        logger.exception("UnionPay front return processing failed", extra={"failure_type": type(exc).__name__})
        outcome = UnionPayFrontReturnOutcome(order_no=None, payment_result="unknown")

    return RedirectResponse(
        url=build_unionpay_customer_return_url(
            outcome.order_no,
            payment_result=outcome.payment_result,
        ),
        status_code=303,
    )
