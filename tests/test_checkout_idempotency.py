"""Checkout 重试必须复用待支付订单，不能重复写本地订单。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from service.cart_service import create_cart_checkout_session
from service.checkout_service import (
    CheckoutCancellationUnavailableError,
    cancel_checkout_session,
    create_checkout_session,
)
from store.cart_store import StoredCartItem
from store.checkout_store import (
    CartCheckoutLine,
    CheckoutProduct,
    CustomerPendingCheckout,
    ReusablePendingCheckout,
    cancel_customer_pending_checkout,
    find_reusable_pending_cart_checkout,
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
    client.build_page_pay_form.return_value = MagicMock(
        url="https://sandbox.example/pay-existing",
        action="https://sandbox.example/gateway.do?charset=utf-8",
        fields={"method": "alipay.trade.page.pay"},
    )
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
    assert session.payment_form_action == "https://sandbox.example/gateway.do?charset=utf-8"
    assert session.payment_form_fields == {"method": "alipay.trade.page.pay"}
    assert session.payment_qr_code is None
    create_order.assert_not_awaited()
    client.build_page_pay_form.assert_called_once_with(
        merchant_payment_no="PMEXISTING",
        amount_cents=299900,
        subject="测试笔记本",
        return_url="http://127.0.0.1:5173/?page=orders&payment_return=1&checkout_order=SOEXISTING",
    )


@pytest.mark.asyncio
async def test_cart_checkout_keeps_items_until_payment_is_confirmed():
    """创建待支付单时保存消费快照，但绝不能提前清空购物车。"""
    product = CheckoutProduct(
        category="components",
        product_id="memory-1",
        product_name="测试内存",
        brand="测试品牌",
        unit_amount_cents=66900,
        stock=8,
    )
    cart_item = StoredCartItem(item_id=12, category="components", product_id="memory-1", quantity=2)
    client = MagicMock()
    client.build_page_pay_form.return_value = MagicMock(
        url="https://sandbox.example/pay",
        action="https://sandbox.example/gateway.do?charset=utf-8",
        fields={"method": "alipay.trade.page.pay"},
    )
    with (
        patch("service.cart_service.AlipaySandboxClient.from_settings", return_value=client),
        patch(
            "service.cart_service.find_reusable_pending_cart_checkout",
            new=AsyncMock(return_value=None),
        ) as find_reusable,
        patch("service.cart_service.list_cart_items", new=AsyncMock(return_value=[cart_item])),
        patch("service.cart_service.get_checkout_product", new=AsyncMock(return_value=product)),
        patch("service.cart_service.create_checkout_order_from_lines", new=AsyncMock()) as create_order,
    ):
        session = await create_cart_checkout_session(101, "http://127.0.0.1:5173")

    assert session.payment_qr_code is None
    assert session.payment_form_action == "https://sandbox.example/gateway.do?charset=utf-8"
    assert session.payment_form_fields == {"method": "alipay.trade.page.pay"}
    find_reusable.assert_awaited_once_with(
        101,
        [CartCheckoutLine(category="components", product_id="memory-1", quantity=2, unit_amount_cents=66900)],
        133800,
    )
    assert create_order.await_args.kwargs["cart_lines"] == [
        CartCheckoutLine(category="components", product_id="memory-1", quantity=2, unit_amount_cents=66900)
    ]
    assert create_order.await_args.kwargs["merchant_payment_no"].startswith("PMV2")
    client.build_page_pay_form.assert_called_once_with(
        merchant_payment_no=create_order.await_args.kwargs["merchant_payment_no"],
        amount_cents=133800,
        subject="Geex Digital 商品订单（2 件）",
        return_url="http://127.0.0.1:5173/?page=orders&payment_return=1&checkout_order=" + session.order_no,
    )


@pytest.mark.asyncio
async def test_cart_reuse_does_not_use_an_old_amount_or_product_snapshot():
    """购物车改动后不能复用旧的待支付订单。"""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(
        return_value=[
            ("SO-OLD", "PM-OLD", 512800, "laptops", "laptop-old", 1, 512800, "旧商品"),
        ]
    )
    connection = MagicMock()
    connection.execute = AsyncMock(return_value=cursor)
    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()),
    ):
        reusable = await find_reusable_pending_cart_checkout(
            101,
            [CartCheckoutLine(category="components", product_id="memory-new", quantity=1)],
            230000,
        )

    assert reusable is None


@pytest.mark.asyncio
async def test_cart_reuse_only_considers_orders_created_from_cart_snapshots():
    """单件购买的待支付单即使商品相同，也不能被购物车结算复用。"""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    connection = MagicMock()
    connection.execute = AsyncMock(return_value=cursor)
    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()),
    ):
        await find_reusable_pending_cart_checkout(
            101,
            [CartCheckoutLine(category="components", product_id="memory-1", quantity=1, unit_amount_cents=66900)],
            66900,
        )

    reuse_sql = connection.execute.await_args.args[0]
    assert "JOIN checkout_cart_lines AS c" in reuse_sql
    assert "c.consumed_at IS NULL" in reuse_sql


@pytest.mark.asyncio
async def test_cart_reuse_checks_each_line_price_not_only_total():
    """多商品价格涨跌抵消时，也不能复用旧的逐行价格快照。"""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(
        return_value=[
            ("SO-OLD", "PM-OLD", 30000, "components", "memory-1", 1, 10000, "内存"),
            ("SO-OLD", "PM-OLD", 30000, "components", "ssd-1", 1, 20000, "硬盘"),
        ]
    )
    connection = MagicMock()
    connection.execute = AsyncMock(return_value=cursor)
    with (
        patch("store.checkout_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_store.put_connection", new=AsyncMock()),
    ):
        reusable = await find_reusable_pending_cart_checkout(
            101,
            [
                CartCheckoutLine("components", "memory-1", 1, 11000),
                CartCheckoutLine("components", "ssd-1", 1, 19000),
            ],
            30000,
        )

    assert reusable is None


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
