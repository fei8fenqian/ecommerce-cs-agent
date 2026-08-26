"""客户发起支付宝沙箱 checkout 的 API 边界。"""

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from config import settings
from service.checkout_refund_service import (
    CustomerRefundResult,
    FinanceRefundDecisionUnavailableError,
    RefundConfirmationUnavailableError,
    RefundGatewayUnavailableError,
    RefundNotEligibleError,
    approve_finance_refund,
    confirm_customer_refund,
    refresh_customer_refund_status,
    reject_finance_refund_request,
    request_customer_refund,
)
from service.checkout_service import (
    CheckoutCancellationUnavailableError,
    CheckoutUnavailableError,
    PaymentNotCreatedError,
    PaymentStatusUnavailableError,
    cancel_checkout_session,
    create_checkout_session,
    refresh_customer_payment_status,
    resume_checkout_session,
)
from store.checkout_refund_store import list_finance_anomalies, list_finance_refunds
from store.checkout_store import list_customer_checkout_orders

checkout_router = APIRouter(prefix="/api/v1/checkout", tags=["沙箱结算"])


class CreateCheckoutRequest(BaseModel):
    """从已展示商品创建一笔单商品沙箱订单。"""

    category: Literal["laptops", "phones", "components"]
    product_id: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, ge=1, le=5)
    return_origin: str | None = Field(default=None, max_length=200)


class ResumeCheckoutRequest(BaseModel):
    """重新打开一笔待付款订单的支付宝付款页。"""

    return_origin: str | None = Field(default=None, max_length=200)


class CheckoutSessionResponse(BaseModel):
    """浏览器跳转支付宝沙箱所需的数据。"""

    order_no: str
    amount_cents: int
    payment_url: str
    payment_form_action: str | None = None
    payment_form_fields: dict[str, str] | None = None
    payment_qr_code: str | None = None


class CheckoutOrderItem(BaseModel):
    """客户订单页展示的应用自有结算订单。"""

    order_no: str
    status: str
    total_amount_cents: int
    product_name: str
    quantity: int
    payment_status: str
    fulfillment_status: str | None
    tracking_company: str | None
    tracking_number: str | None
    created_at: str
    refund_id: str | None = None
    refund_status: str | None = None


class CheckoutOrderListResponse(BaseModel):
    """当前客户的新结算订单列表。"""

    orders: list[CheckoutOrderItem]


class FinanceRefundItem(BaseModel):
    """财务工作台展示的一笔退款摘要。"""

    refund_id: str
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    requested_at: str
    finance_decision_note: str
    finance_decided_at: str | None


class FinanceRefundListResponse(BaseModel):
    """财务可见的退款队列。"""

    refunds: list[FinanceRefundItem]


class FinanceAnomalyItem(BaseModel):
    """财务异常扫描的一条只读事实摘要。"""

    anomaly_type: str
    reference_id: str
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    occurred_at: str
    age_seconds: int


class FinanceAnomalyListResponse(BaseModel):
    """需要财务关注的支付/退款异常。"""

    anomalies: list[FinanceAnomalyItem]


class FinanceAnomalySummaryResponse(BaseModel):
    """根据当前扫描事实生成的财务核查摘要。"""

    summary: str
    anomaly_count: int
    generated_at: str


class FinanceRefundDecisionRequest(BaseModel):
    """财务审批或驳回的受控备注；退款金额始终读取服务端事实。"""

    decision_note: str = Field(default="", max_length=500)


class CancelCheckoutResponse(BaseModel):
    """取消待支付订单的明确结果。"""

    order_no: str
    cancelled: bool


class CreateCheckoutRefundRequest(BaseModel):
    """客户提交一笔全额退款申请；金额始终由已支付订单确定。"""

    reason: str = Field(default="", max_length=500)


class CheckoutRefundResponse(BaseModel):
    """客户可读取的退款状态，不暴露支付宝网关原始响应。"""

    refund_id: str
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    requested_at: str
    idempotent_replay: bool


def _refund_response(result: CustomerRefundResult) -> CheckoutRefundResponse:
    """转换内部 UUID 结果，保持 API JSON 的标量字段稳定。"""
    return CheckoutRefundResponse(
        refund_id=str(result.refund_id),
        order_no=result.order_no,
        status=result.status,
        amount_cents=result.amount_cents,
        currency=result.currency,
        reason=result.reason,
        requested_at=result.requested_at,
        idempotent_replay=result.idempotent_replay,
    )


@checkout_router.post("/orders", response_model=CheckoutSessionResponse, status_code=201)
async def create_order(body: CreateCheckoutRequest, request: Request) -> CheckoutSessionResponse:
    """客户确认购买后创建待支付订单；不由 Agent 文本直接触发。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以创建支付订单")
    try:
        session = await create_checkout_session(
            customer_user_id=int(user["id"]),
            category=body.category,
            product_id=body.product_id,
            quantity=body.quantity,
            return_origin=body.return_origin,
        )
    except CheckoutUnavailableError as exc:
        raise HTTPException(status_code=409, detail="商品暂不可购买") from exc
    return CheckoutSessionResponse(**session.__dict__)


@checkout_router.post("/orders/{order_no}/resume-payment", response_model=CheckoutSessionResponse)
async def resume_payment(
    order_no: str,
    body: ResumeCheckoutRequest,
    request: Request,
) -> CheckoutSessionResponse:
    """让客户继续支付自己的待支付订单，不重复写订单或支付交易。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以继续支付订单")
    try:
        session = await resume_checkout_session(
            customer_user_id=int(user["id"]), order_no=order_no, return_origin=body.return_origin
        )
    except CheckoutUnavailableError as exc:
        raise HTTPException(status_code=409, detail="该订单当前不能继续付款") from exc
    return CheckoutSessionResponse(**session.__dict__)


@checkout_router.post("/orders/{order_no}/cancel", response_model=CancelCheckoutResponse)
async def cancel_order(order_no: str, request: Request) -> CancelCheckoutResponse:
    """关闭客户本人的待支付支付宝交易并取消本地订单。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以取消支付订单")
    try:
        await cancel_checkout_session(customer_user_id=int(user["id"]), order_no=order_no)
    except CheckoutCancellationUnavailableError as exc:
        raise HTTPException(status_code=409, detail="该订单当前不能取消") from exc
    except PaymentStatusUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝暂时无法关闭交易") from exc
    return CancelCheckoutResponse(order_no=order_no, cancelled=True)


@checkout_router.get("/orders/my", response_model=CheckoutOrderListResponse)
async def my_checkout_orders(request: Request) -> CheckoutOrderListResponse:
    """读取当前客户的新结算订单及支付状态。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查看支付订单")
    orders = await list_customer_checkout_orders(int(user["id"]))
    return CheckoutOrderListResponse(orders=[CheckoutOrderItem(**order.__dict__) for order in orders])


@checkout_router.get("/finance/refunds", response_model=FinanceRefundListResponse)
async def finance_refunds(request: Request) -> FinanceRefundListResponse:
    """财务读取退款队列；只读，不直接改变退款或订单状态。"""
    if request.state.user["role"] != "finance":
        raise HTTPException(status_code=403, detail="只有财务可以查看退款队列")
    refunds = await list_finance_refunds()
    return FinanceRefundListResponse(
        refunds=[
            FinanceRefundItem(
                refund_id=str(refund.refund_id),
                order_no=refund.order_no,
                status=refund.status,
                amount_cents=refund.amount_cents,
                currency=refund.currency,
                reason=refund.reason,
                requested_at=refund.requested_at,
                finance_decision_note=refund.finance_decision_note,
                finance_decided_at=refund.finance_decided_at,
            )
            for refund in refunds
        ]
    )


@checkout_router.get("/finance/anomalies", response_model=FinanceAnomalyListResponse)
async def finance_anomalies(request: Request) -> FinanceAnomalyListResponse:
    """扫描长时间未收敛的支付/退款事实；只读，不改变业务状态。"""
    if request.state.user["role"] != "finance":
        raise HTTPException(status_code=403, detail="只有财务可以查看资金异常")
    anomalies = await list_finance_anomalies(timeout_minutes=settings.finance_anomaly_timeout_minutes)
    return FinanceAnomalyListResponse(anomalies=[FinanceAnomalyItem(**anomaly.__dict__) for anomaly in anomalies])


@checkout_router.post("/finance/anomalies/summary", response_model=FinanceAnomalySummaryResponse)
async def summarize_finance_anomalies(request: Request) -> FinanceAnomalySummaryResponse:
    """根据一次新扫描的只读事实生成 Agent 核查摘要，不执行财务动作。"""
    if request.state.user["role"] != "finance":
        raise HTTPException(status_code=403, detail="只有财务可以生成资金异常摘要")

    anomalies = await list_finance_anomalies(timeout_minutes=settings.finance_anomaly_timeout_minutes)
    generated_at = datetime.now(timezone.utc).isoformat()
    if not anomalies:
        return FinanceAnomalySummaryResponse(
            summary="当前扫描未发现需要财务关注的支付或退款异常。",
            anomaly_count=0,
            generated_at=generated_at,
        )

    facts = [
        {
            "anomaly_type": anomaly.anomaly_type,
            "reference_id": anomaly.reference_id,
            "order_no": anomaly.order_no,
            "status": anomaly.status,
            "amount_cents": anomaly.amount_cents,
            "currency": anomaly.currency,
            "reason": anomaly.reason,
            "occurred_at": anomaly.occurred_at,
            "age_seconds": anomaly.age_seconds,
        }
        for anomaly in anomalies
    ]
    try:
        llm_response = await request.app.state.llm_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是企业财务核查助手。只能根据给定的本地支付/退款扫描事实，"
                        "用中文输出简洁的核查摘要和建议的核查顺序。必须保留订单号、异常类型、"
                        "状态、金额和持续时间等事实；不得新增、改写、估算或合并任何金额、订单号、"
                        "时间或渠道结果。不得批准、驳回、重试、退款或修改任何状态。"
                        "如果事实不足以判断原因，明确写出需要人工核查外部渠道。只输出摘要正文。"
                    ),
                },
                {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=800,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Agent 摘要暂时不可用，请直接按异常队列核查") from exc

    summary = (llm_response.content or "").strip()
    if not summary:
        raise HTTPException(status_code=503, detail="Agent 摘要暂时不可用，请直接按异常队列核查")
    return FinanceAnomalySummaryResponse(
        summary=summary,
        anomaly_count=len(anomalies),
        generated_at=generated_at,
    )


@checkout_router.post("/finance/refunds/{refund_id}/approve", response_model=CheckoutRefundResponse)
async def approve_finance_refund_route(
    refund_id: str,
    body: FinanceRefundDecisionRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=80),
) -> CheckoutRefundResponse:
    """财务批准退款；审批和资金提交仍由确定性服务串联完成。"""
    from uuid import UUID

    if request.state.user["role"] != "finance":
        raise HTTPException(status_code=403, detail="只有财务可以审批退款")
    try:
        result = await approve_finance_refund(
            finance_user_id=int(request.state.user["id"]),
            refund_id=UUID(refund_id),
            decision_idempotency_key=idempotency_key,
            decision_note=body.decision_note.strip(),
        )
    except (ValueError, FinanceRefundDecisionUnavailableError) as exc:
        raise HTTPException(status_code=409, detail="该退款当前不能审批") from exc
    except RefundGatewayUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝退款结果暂时无法确认，请稍后查看退款状态") from exc
    return _refund_response(result)


@checkout_router.post("/finance/refunds/{refund_id}/reject", response_model=CheckoutRefundResponse)
async def reject_finance_refund_route(
    refund_id: str,
    body: FinanceRefundDecisionRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=80),
) -> CheckoutRefundResponse:
    """财务驳回退款；不调用支付宝，订单仍保持已支付。"""
    from uuid import UUID

    if request.state.user["role"] != "finance":
        raise HTTPException(status_code=403, detail="只有财务可以驳回退款")
    try:
        result = await reject_finance_refund_request(
            finance_user_id=int(request.state.user["id"]),
            refund_id=UUID(refund_id),
            decision_idempotency_key=idempotency_key,
            decision_note=body.decision_note.strip(),
        )
    except (ValueError, FinanceRefundDecisionUnavailableError) as exc:
        raise HTTPException(status_code=409, detail="该退款当前不能驳回") from exc
    return _refund_response(result)


@checkout_router.post("/orders/{order_no}/refresh-payment", response_model=CheckoutOrderItem)
async def refresh_payment(order_no: str, request: Request) -> CheckoutOrderItem:
    """从支付宝查询当前客户订单的支付状态；不信任浏览器回跳参数。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查询支付订单")
    try:
        await refresh_customer_payment_status(customer_user_id=int(user["id"]), order_no=order_no)
    except PaymentNotCreatedError as exc:
        raise HTTPException(status_code=409, detail="该订单未在支付宝侧创建交易，请重新下单") from exc
    except PaymentStatusUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝暂时无法确认支付状态") from exc
    orders = await list_customer_checkout_orders(int(user["id"]))
    order = next((item for item in orders if item.order_no == order_no), None)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不可用或无法核验")
    return CheckoutOrderItem(**order.__dict__)


@checkout_router.post("/orders/{order_no}/refunds", response_model=CheckoutRefundResponse, status_code=201)
async def request_refund(
    order_no: str,
    body: CreateCheckoutRefundRequest,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=80),
) -> CheckoutRefundResponse:
    """创建客户确认前的退款申请，不会立即向支付宝发起资金动作。"""
    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以申请退款")
    try:
        result = await request_customer_refund(
            customer_user_id=int(user["id"]),
            order_no=order_no,
            reason=body.reason.strip(),
            request_idempotency_key=idempotency_key,
        )
    except RefundNotEligibleError as exc:
        raise HTTPException(status_code=409, detail="该订单当前不满足退款条件") from exc
    return _refund_response(result)


@checkout_router.post("/refunds/{refund_id}/confirm", response_model=CheckoutRefundResponse)
async def confirm_refund(
    refund_id: str,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=80),
) -> CheckoutRefundResponse:
    """在客户最后确认后提交一次全额支付宝沙箱退款。"""
    from uuid import UUID

    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以确认退款")
    try:
        result = await confirm_customer_refund(
            customer_user_id=int(user["id"]),
            refund_id=UUID(refund_id),
            confirmation_idempotency_key=idempotency_key,
        )
    except (ValueError, RefundConfirmationUnavailableError) as exc:
        raise HTTPException(status_code=409, detail="该退款当前不能确认") from exc
    except RefundGatewayUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝退款结果暂时无法确认，请稍后查看订单状态") from exc
    return _refund_response(result)


@checkout_router.post("/refunds/{refund_id}/refresh", response_model=CheckoutRefundResponse)
async def refresh_refund(refund_id: str, request: Request) -> CheckoutRefundResponse:
    """主动查询支付宝已提交退款的当前状态，不会再次发起退款。"""
    from uuid import UUID

    user = request.state.user
    if user["role"] != "customer":
        raise HTTPException(status_code=403, detail="只有客户可以查询退款状态")
    try:
        result = await refresh_customer_refund_status(
            customer_user_id=int(user["id"]),
            refund_id=UUID(refund_id),
        )
    except (ValueError, RefundConfirmationUnavailableError) as exc:
        raise HTTPException(status_code=404, detail="退款不可用或无法核验") from exc
    except RefundGatewayUnavailableError as exc:
        raise HTTPException(status_code=503, detail="支付宝退款状态暂时无法确认，请稍后重试") from exc
    return _refund_response(result)
