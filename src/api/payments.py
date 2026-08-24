"""支付宝沙箱异步通知入口；不使用客户认证或普通命令幂等键。"""

from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from service.checkout_service import AlipayCallbackRejectedError, process_alipay_callback

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
