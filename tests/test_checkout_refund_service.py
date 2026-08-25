"""全额退款应用服务测试；所有外部支付和数据库调用均为 mock。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from exceptions import DependencyUnavailableError
from infra.alipay_sandbox import AlipayRefundRejectedError
from service.checkout_refund_service import (
    FinanceRefundDecisionUnavailableError,
    RefundGatewayUnavailableError,
    approve_finance_refund,
    confirm_customer_refund,
    refresh_customer_refund_status,
    reject_finance_refund_request,
    request_customer_refund,
)
from store.checkout_refund_store import CheckoutRefund, FinanceDecisionStart, RefundConfirmationStart


def _refund(*, status: str = "PENDING_CONFIRMATION") -> CheckoutRefund:
    """构造一笔不包含真实支付信息的退款测试记录。"""
    return CheckoutRefund(
        refund_id=uuid4(),
        order_no="SO202608250001",
        merchant_payment_no="PM202608250001",
        merchant_refund_no="RF202608250001",
        status=status,
        amount_cents=529900,
        currency="CNY",
        reason="不需要了",
        requested_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_request_refund_creates_only_a_pending_confirmation_record() -> None:
    """申请阶段不得调用支付宝；客户确认前只能写本地退款记录。"""
    refund = _refund()
    with patch(
        "service.checkout_refund_service.create_customer_refund_request",
        new=AsyncMock(return_value=refund),
    ) as created:
        result = await request_customer_refund(
            customer_user_id=101,
            order_no=refund.order_no,
            reason=refund.reason,
            request_idempotency_key="request-key-0001",
        )

    assert result.status == "PENDING_CONFIRMATION"
    assert result.idempotent_replay is True  # mock returned a historical refund number
    assert created.await_count == 1
    assert created.await_args is not None
    assert created.await_args.kwargs["status"] == "AUTO"


@pytest.mark.asyncio
async def test_confirmation_calls_alipay_once_then_marks_checkout_refunded() -> None:
    """确认后的明确成功结果才允许订单进入 REFUNDED。"""
    processing = _refund(status="PROCESSING")
    succeeded = _refund(status="SUCCEEDED")
    succeeded = CheckoutRefund(**{**succeeded.__dict__, "refund_id": processing.refund_id})
    gateway = AsyncMock()
    gateway.refund_trade.return_value = {
        "out_trade_no": processing.merchant_payment_no,
        "out_request_no": processing.merchant_refund_no,
        "refund_fee": "5299.00",
        "trade_no": "2026082500001",
    }
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings", return_value=gateway),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ) as marked,
    ):
        result = await confirm_customer_refund(
            customer_user_id=101,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="confirm-key-0001",
        )

    assert result.status == "SUCCEEDED"
    gateway.refund_trade.assert_awaited_once_with(
        merchant_payment_no=processing.merchant_payment_no,
        merchant_refund_no=processing.merchant_refund_no,
        amount_cents=processing.amount_cents,
    )
    marked.assert_awaited_once_with(refund_id=processing.refund_id, provider_refund_reference="2026082500001")


@pytest.mark.asyncio
async def test_confirmation_replay_does_not_make_a_second_gateway_call() -> None:
    """相同确认键重放只返回处理中状态，避免重复退款。"""
    processing = _refund(status="PROCESSING")
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, False)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings") as client,
    ):
        result = await confirm_customer_refund(
            customer_user_id=101,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="confirm-key-0001",
        )

    assert result.status == "PROCESSING"
    assert result.idempotent_replay is True
    client.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_rejection_marks_refund_failed_but_network_unknown_keeps_processing() -> None:
    """明确拒绝可失败收敛；网络未知不能回退后再次发起退款。"""
    processing = _refund(status="PROCESSING")
    failed = CheckoutRefund(**{**processing.__dict__, "status": "FAILED"})
    rejected_gateway = AsyncMock()
    rejected_gateway.refund_trade.side_effect = AlipayRefundRejectedError()
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings", return_value=rejected_gateway),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_failed",
            new=AsyncMock(return_value=failed),
        ) as marked_failed,
    ):
        result = await confirm_customer_refund(
            customer_user_id=101,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="confirm-key-0001",
        )
    assert result.status == "FAILED"
    marked_failed.assert_awaited_once_with(processing.refund_id)

    unavailable_gateway = AsyncMock()
    unavailable_gateway.refund_trade.side_effect = DependencyUnavailableError("sandbox unavailable")
    with (
        patch(
            "service.checkout_refund_service.start_customer_refund_confirmation",
            new=AsyncMock(return_value=RefundConfirmationStart(processing, True)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings", return_value=unavailable_gateway),
        patch("service.checkout_refund_service.mark_checkout_refund_failed", new=AsyncMock()) as should_not_fail,
        pytest.raises(RefundGatewayUnavailableError),
    ):
        await confirm_customer_refund(
            customer_user_id=101,
            refund_id=processing.refund_id,
            confirmation_idempotency_key="confirm-key-0002",
        )
    should_not_fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_processing_refund_converges_only_after_matching_query_result() -> None:
    """查询成功会完成退款；查询响应不匹配本地交易时绝不改订单状态。"""
    processing = _refund(status="PROCESSING")
    succeeded = CheckoutRefund(**{**processing.__dict__, "status": "SUCCEEDED"})
    gateway = AsyncMock()
    gateway.query_refund.return_value = {
        "out_trade_no": processing.merchant_payment_no,
        "out_request_no": processing.merchant_refund_no,
        "refund_amount": "5299.00",
        "refund_status": "REFUND_SUCCESS",
        "trade_no": "ALIPAY-REFUND-1",
    }
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=processing)),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings", return_value=gateway),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ) as marked,
    ):
        result = await refresh_customer_refund_status(customer_user_id=101, refund_id=processing.refund_id)

    assert result.status == "SUCCEEDED"
    gateway.query_refund.assert_awaited_once_with(
        merchant_payment_no=processing.merchant_payment_no,
        merchant_refund_no=processing.merchant_refund_no,
    )
    marked.assert_awaited_once_with(
        refund_id=processing.refund_id,
        provider_refund_reference="ALIPAY-REFUND-1",
    )


@pytest.mark.asyncio
async def test_refresh_non_processing_refund_never_calls_gateway() -> None:
    """已完成退款的刷新只是读取本地状态，避免无意义的外部调用。"""
    succeeded = _refund(status="SUCCEEDED")
    with (
        patch("service.checkout_refund_service.get_customer_checkout_refund", new=AsyncMock(return_value=succeeded)),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings") as client,
    ):
        result = await refresh_customer_refund_status(customer_user_id=101, refund_id=succeeded.refund_id)

    assert result.status == "SUCCEEDED"
    assert result.idempotent_replay is True
    client.assert_not_called()


@pytest.mark.asyncio
async def test_finance_approval_submits_high_value_refund_once() -> None:
    """财务批准取得唯一提交资格后，仍复用同一个支付宝退款服务。"""
    pending = _refund(status="PENDING_FINANCE_APPROVAL")
    processing = _refund(status="PROCESSING")
    processing = CheckoutRefund(**{**processing.__dict__, "refund_id": pending.refund_id})
    succeeded = _refund(status="SUCCEEDED")
    succeeded = CheckoutRefund(**{**succeeded.__dict__, "refund_id": pending.refund_id})
    gateway = AsyncMock()
    gateway.refund_trade.return_value = {
        "out_trade_no": pending.merchant_payment_no,
        "refund_fee": "5299.00",
        "trade_no": "FINANCE-REFUND-1",
    }
    with (
        patch(
            "service.checkout_refund_service.start_finance_refund_approval",
            new=AsyncMock(return_value=FinanceDecisionStart(processing, True, False)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings", return_value=gateway),
        patch(
            "service.checkout_refund_service.mark_checkout_refund_succeeded",
            new=AsyncMock(return_value=succeeded),
        ) as marked,
    ):
        result = await approve_finance_refund(
            finance_user_id=202,
            refund_id=pending.refund_id,
            decision_idempotency_key="finance-approve-0001",
            decision_note="金额和支付事实已核对",
        )

    assert result.status == "SUCCEEDED"
    gateway.refund_trade.assert_awaited_once()
    marked.assert_awaited_once_with(refund_id=pending.refund_id, provider_refund_reference="FINANCE-REFUND-1")


@pytest.mark.asyncio
async def test_finance_approval_replay_does_not_call_gateway_again() -> None:
    """财务批准命令重放只返回处理中，不产生第二笔退款。"""
    processing = _refund(status="PROCESSING")
    with (
        patch(
            "service.checkout_refund_service.start_finance_refund_approval",
            new=AsyncMock(return_value=FinanceDecisionStart(processing, False, True)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings") as client,
    ):
        result = await approve_finance_refund(
            finance_user_id=202,
            refund_id=processing.refund_id,
            decision_idempotency_key="finance-approve-0001",
            decision_note="重放",
        )

    assert result.status == "PROCESSING"
    assert result.idempotent_replay is True
    client.assert_not_called()


@pytest.mark.asyncio
async def test_finance_rejection_never_calls_gateway() -> None:
    """财务驳回只收敛本地状态，不触碰支付宝。"""
    rejected = _refund(status="REJECTED")
    with (
        patch(
            "service.checkout_refund_service.reject_finance_refund",
            new=AsyncMock(return_value=(rejected, False)),
        ),
        patch("service.checkout_refund_service.AlipaySandboxClient.from_settings") as client,
    ):
        result = await reject_finance_refund_request(
            finance_user_id=202,
            refund_id=rejected.refund_id,
            decision_idempotency_key="finance-reject-0001",
            decision_note="订单已进入履约，不符合首版退款条件",
        )

    assert result.status == "REJECTED"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_finance_command_rejects_already_handled_refund() -> None:
    """已处理退款不能被第二次财务决策覆盖。"""
    with patch(
        "service.checkout_refund_service.start_finance_refund_approval",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(FinanceRefundDecisionUnavailableError):
            await approve_finance_refund(
                finance_user_id=202,
                refund_id=uuid4(),
                decision_idempotency_key="finance-approve-0002",
                decision_note="重复审批",
            )
