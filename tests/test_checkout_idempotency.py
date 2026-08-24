"""Checkout 重试必须复用待支付订单，不能重复写本地订单。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from service.checkout_service import (
    CheckoutCancellationUnavailableError,
    cancel_checkout_session,
    create_checkout_session,
)
from store.checkout_store import (
    CheckoutProduct,
    CustomerPendingCheckout,
    ReusablePendingCheckout,
    cancel_customer_pending_checkout,
)


@pytest.mark.asyncio
async def test_retrying_the_same_purchase_reuses_pending_checkout():
    """支付页跳转失败后再次购买仍使用同一商户订单号。"""
    product = CheckoutProduct(
        category="laptops",
        product_id="laptop-1",
        product_name="测试笔记本",
        brand="测试品牌",
        unit_amount_cents=299900,
        stock=3,
    )
    pending = ReusablePendingCheckout(
        order_no="SOEXISTING",
        merchant_payment_no="PMEXISTING",
        amount_cents=299900,
        subject="测试笔记本",
    )
    client = MagicMock()
    client.build_page_pay_url.return_value = "https://sandbox.example/pay-existing"
    with (
        patch("service.checkout_service.AlipaySandboxClient.from_settings", return_value=client),
        patch("service.checkout_service.get_checkout_product", new=AsyncMock(return_value=product)),
        patch("service.checkout_service.find_reusable_pending_checkout", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.create_checkout_order", new=AsyncMock()) as create_order,
    ):
        session = await create_checkout_session(
            customer_user_id=101,
            category="laptops",
            product_id="laptop-1",
            quantity=1,
            return_origin="http://127.0.0.1:5173",
        )

    assert session.order_no == "SOEXISTING"
    assert session.payment_url == "https://sandbox.example/pay-existing"
    create_order.assert_not_awaited()
    client.build_page_pay_url.assert_called_once_with(
        merchant_payment_no="PMEXISTING",
        amount_cents=299900,
        subject="测试笔记本",
        return_url="http://127.0.0.1:5173/?page=orders&payment_return=1&checkout_order=SOEXISTING",
    )


@pytest.mark.asyncio
async def test_cancelling_checkout_returns_the_target_payment_order_id():
    """取消 SQL 只从被更新的支付记录返回订单 ID，避免依赖 FROM 表别名。"""
    row_cursor = MagicMock()
    row_cursor.fetchone = AsyncMock(return_value=("order-uuid",))
    connection = MagicMock()
    connection.execute = AsyncMock(side_effect=[None, row_cursor, None])
    connection.commit = AsyncMock()
    connection.rollback = AsyncMock()

    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()) as put_connection,
    ):
        cancelled = await cancel_customer_pending_checkout(101, "SO202608240001")

    assert cancelled is True
    update_sql = connection.execute.await_args_list[1].args[0]
    assert "RETURNING p.sales_order_id" in update_sql
    assert "RETURNING o.id" not in update_sql
    assert "version = p.version + 1" in update_sql
    connection.commit.assert_awaited_once()
    put_connection.assert_awaited_once_with(connection)


@pytest.mark.asyncio
async def test_cancelling_first_reconciles_a_payment_that_already_succeeded():
    """本地待支付但支付宝已成功时，不再尝试关闭已付款交易。"""
    pending = CustomerPendingCheckout("SO202608240001", "PM202608240001")
    with (
        patch("service.checkout_service.get_customer_pending_checkout", new=AsyncMock(return_value=pending)),
        patch("service.checkout_service.refresh_customer_payment_status", new=AsyncMock(return_value=True)),
        patch("service.checkout_service.AlipaySandboxClient.from_settings") as client_factory,
    ):
        with pytest.raises(CheckoutCancellationUnavailableError):
            await cancel_checkout_session(customer_user_id=101, order_no="SO202608240001")

    client_factory.assert_not_called()
