"""应用自有 checkout 数据访问层；绝不读取或写入 legacy orders。"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

from infra.db_pool import get_connection, put_connection
from store.refund_store_types import AsyncConnection

CheckoutCategory = Literal["laptops", "phones", "components"]
PaymentProviderName = Literal["alipay_sandbox", "unionpay_test"]
CURRENT_PAYMENT_NO_PREFIX = "PMV2"
_PAYMENT_PROVIDER_NAMES = {"alipay_sandbox", "unionpay_test"}
_PROVIDER_TXN_TIME_PATTERN = re.compile(r"^\d{14}$")


def _validate_payment_provider(provider: str) -> PaymentProviderName:
    if provider not in _PAYMENT_PROVIDER_NAMES:
        raise ValueError("unsupported payment provider")
    return cast(PaymentProviderName, provider)


def _payment_provider_from_row(value: object) -> PaymentProviderName:
    """将数据库中的渠道枚举转换为当前已实现的 provider 类型。"""
    return _validate_payment_provider(str(value))


_PRODUCT_TABLES: dict[CheckoutCategory, str] = {
    "laptops": "laptop_products",
    "phones": "phone_products",
    "components": "component_products",
}


@dataclass(frozen=True)
class CheckoutProduct:
    """创建订单前从当前目录读取的一次性商品事实。"""

    category: CheckoutCategory
    product_id: str
    product_name: str
    brand: str
    unit_amount_cents: int
    stock: int


@dataclass(frozen=True)
class CreatedCheckoutOrder:
    """一笔待支付的新订单与其对应支付单。"""

    sales_order_id: UUID
    order_no: str
    merchant_payment_no: str
    total_amount_cents: int
    provider: PaymentProviderName = "alipay_sandbox"
    provider_txn_time: str | None = None


@dataclass(frozen=True)
class CheckoutLine:
    """创建结算订单时的一条商品快照与数量。"""

    product: CheckoutProduct
    quantity: int


@dataclass(frozen=True)
class CartCheckoutLine:
    """一次购物车结算时需要在付款成功后消费的商品数量快照。

    这不是购物车行的外键：客户在付款前仍可调整或删除购物车，因此只保留当时的
    商品标识和数量。``unit_amount_cents`` 只用于判断待支付订单是否仍与当前购物车
    价格一致，不写入消费快照。付款成功时再在同一事务内做一次保守扣减。
    """

    category: CheckoutCategory
    product_id: str
    quantity: int
    unit_amount_cents: int | None = None


@dataclass(frozen=True)
class CallbackPayment:
    """回调核验和条件更新所需的本地支付事实。"""

    payment_id: UUID
    sales_order_id: UUID
    amount_cents: int
    payment_status: str


@dataclass(frozen=True)
class CustomerCheckoutOrder:
    """客户订单页展示的新结算订单摘要。"""

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
    refund_amount_cents: int | None = None
    provider: PaymentProviderName = "alipay_sandbox"
    refund_eligible: bool = False


@dataclass(frozen=True)
class CustomerPendingPayment:
    """客户本人可主动同步的待支付交易。"""

    merchant_payment_no: str
    amount_cents: int
    subject: str
    provider: PaymentProviderName = "alipay_sandbox"
    provider_txn_time: str | None = None


@dataclass(frozen=True)
class UnionPayFrontReturnPayment:
    """从已验证银联商户交易号定位本地订单所需的内部支付事实。"""

    order_no: str
    customer_user_id: int
    merchant_payment_no: str
    amount_cents: int
    provider: PaymentProviderName
    provider_txn_time: str | None
    payment_status: str
    order_status: str


@dataclass(frozen=True)
class CustomerPendingCheckout:
    """客户可取消的本地待支付订单及其支付宝商户交易号。"""

    order_no: str
    merchant_payment_no: str
    provider: PaymentProviderName = "alipay_sandbox"
    provider_txn_time: str | None = None


@dataclass(frozen=True)
class ReusablePendingCheckout:
    """同一客户可继续支付的同商品待支付订单。"""

    order_no: str
    merchant_payment_no: str
    amount_cents: int
    subject: str
    provider: PaymentProviderName = "alipay_sandbox"
    provider_txn_time: str | None = None


@dataclass(frozen=True)
class OperatorFulfillment:
    """运营台处理发货所需的最小应用订单摘要。"""

    order_no: str
    product_name: str
    quantity: int
    status: str
    carrier: str | None
    tracking_number: str | None
    created_at: str


async def get_checkout_product(category: CheckoutCategory, product_id: str) -> CheckoutProduct | None:
    """读取可购买商品的当前价格和库存，不暴露任意表名入口。"""
    table = _PRODUCT_TABLES[category]
    brand_column = "COALESCE(metadata->>'brand', '')" if category == "components" else "brand"
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT id, product_name, {brand_column} AS brand, price, stock
            FROM {table}
            WHERE id = %s
            """,
            (product_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[3] is None:
            return None
        amount_cents = int((Decimal(str(row[3])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return CheckoutProduct(
            category=category,
            product_id=str(row[0]),
            product_name=str(row[1] or "未命名商品"),
            brand=str(row[2] or ""),
            unit_amount_cents=amount_cents,
            stock=int(row[4] or 0),
        )
    finally:
        await put_connection(connection)


async def create_checkout_order(
    *,
    sales_order_id: UUID,
    order_no: str,
    payment_id: UUID,
    merchant_payment_no: str,
    customer_user_id: int,
    product: CheckoutProduct,
    quantity: int,
    provider: PaymentProviderName = "alipay_sandbox",
    provider_txn_time: str | None = None,
) -> CreatedCheckoutOrder:
    """创建一笔单商品结算订单，兼容商品详情页的立即购买入口。"""
    return await create_checkout_order_from_lines(
        sales_order_id=sales_order_id,
        order_no=order_no,
        payment_id=payment_id,
        merchant_payment_no=merchant_payment_no,
        customer_user_id=customer_user_id,
        lines=[CheckoutLine(product=product, quantity=quantity)],
        cart_lines=None,
        provider=provider,
        provider_txn_time=provider_txn_time,
    )


async def create_checkout_order_from_lines(
    *,
    sales_order_id: UUID,
    order_no: str,
    payment_id: UUID,
    merchant_payment_no: str,
    customer_user_id: int,
    lines: list[CheckoutLine],
    cart_lines: list[CartCheckoutLine] | None = None,
    provider: PaymentProviderName = "alipay_sandbox",
    provider_txn_time: str | None = None,
) -> CreatedCheckoutOrder:
    """在一个事务中写入多商品订单和待支付交易。

    Args:
        lines: 已在服务层按当前价格和库存校验的商品快照。
        购物车条目在支付宝确认付款前必须保留。否则网络或客户扫码失败都会导致
        用户无故丢失购物车。传入 ``cart_lines`` 时会持久化消费快照；只有付款
        真正成功后，才由状态收敛事务按该快照扣减购物车。

    Raises:
        ValueError: 商品缺货、金额异常、数量不合法或购物车为空。
    """
    _validate_payment_provider(provider)
    if provider == "unionpay_test":
        if provider_txn_time is None or _PROVIDER_TXN_TIME_PATTERN.fullmatch(provider_txn_time) is None:
            raise ValueError("invalid UnionPay provider transaction time")
    elif provider_txn_time is not None:
        raise ValueError("Alipay orders cannot carry provider transaction time")
    if not lines or len(lines) > 10:
        raise ValueError("invalid line count")
    if any(line.quantity < 1 or line.quantity > 5 for line in lines):
        raise ValueError("invalid quantity")
    if any(line.product.stock < line.quantity or line.product.unit_amount_cents <= 0 for line in lines):
        raise ValueError("product unavailable")
    if cart_lines is not None and (
        not cart_lines
        or any(line.quantity < 1 or line.quantity > 5 for line in cart_lines)
        or {(line.product.category, line.product.product_id, line.quantity) for line in lines}
        != {(line.category, line.product_id, line.quantity) for line in cart_lines}
    ):
        raise ValueError("cart checkout lines do not match order lines")
    total_amount_cents = sum(line.product.unit_amount_cents * line.quantity for line in lines)
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        await connection.execute(
            """
            INSERT INTO sales_orders (
                id, order_no, customer_user_id, status, total_amount_cents, currency
            ) VALUES (%s, %s, %s, 'PENDING_PAYMENT', %s, 'CNY')
            """,
            (sales_order_id, order_no, customer_user_id, total_amount_cents),
        )
        for line in lines:
            await connection.execute(
                """
                INSERT INTO sales_order_items (
                    sales_order_id, catalog_category, catalog_product_id, product_name,
                    brand, unit_amount_cents, quantity
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sales_order_id,
                    line.product.category,
                    line.product.product_id,
                    line.product.product_name,
                    line.product.brand,
                    line.product.unit_amount_cents,
                    line.quantity,
                ),
            )
        if cart_lines is not None:
            for cart_line in cart_lines:
                await connection.execute(
                    """
                    INSERT INTO checkout_cart_lines (
                        sales_order_id, catalog_category, catalog_product_id, quantity
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (sales_order_id, cart_line.category, cart_line.product_id, cart_line.quantity),
                )
        await connection.execute(
            """
            INSERT INTO payment_transactions (
                id, sales_order_id, provider, provider_txn_time, merchant_payment_no,
                status, amount_cents, currency
            ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, 'CNY')
            """,
            (payment_id, sales_order_id, provider, provider_txn_time, merchant_payment_no, total_amount_cents),
        )
        await connection.commit()
        return CreatedCheckoutOrder(
            sales_order_id=sales_order_id,
            order_no=order_no,
            merchant_payment_no=merchant_payment_no,
            total_amount_cents=total_amount_cents,
            provider=provider,
            provider_txn_time=provider_txn_time,
        )
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def apply_alipay_callback(
    *,
    merchant_payment_no: str,
    provider_trade_no: str,
    provider_callback_id: str,
    amount_cents: int,
    succeeded: bool,
) -> bool:
    """在同一事务内幂等收敛支付宝回调和本地订单状态。

    Returns:
        回调与本地交易匹配时为 True；金额或订单不匹配时为 False。
    """
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            SELECT id, sales_order_id, amount_cents, status
            FROM payment_transactions
            WHERE merchant_payment_no = %s AND provider = 'alipay_sandbox'
            FOR UPDATE
            """,
            (merchant_payment_no,),
        )
        row = await cursor.fetchone()
        if row is None or int(row[2]) != amount_cents:
            await connection.rollback()
            return False
        payment = CallbackPayment(
            payment_id=row[0],
            sales_order_id=row[1],
            amount_cents=int(row[2]),
            payment_status=str(row[3]),
        )
        if payment.payment_status == "SUCCEEDED":
            await _consume_paid_cart_lines(connection, payment.sales_order_id)
            await _ensure_pending_fulfillment(connection, payment.sales_order_id)
            await connection.commit()
            return True
        if succeeded:
            await connection.execute(
                """
                UPDATE payment_transactions
                SET status = 'SUCCEEDED', provider_trade_no = %s, provider_callback_id = %s,
                    callback_received_at = NOW(), succeeded_at = NOW(), updated_at = NOW(), version = version + 1
                WHERE id = %s AND status IN ('PENDING', 'PROCESSING')
                """,
                (provider_trade_no, provider_callback_id, payment.payment_id),
            )
            await connection.execute(
                """
                UPDATE sales_orders
                SET status = 'PAID', updated_at = NOW(), version = version + 1
                WHERE id = %s AND status = 'PENDING_PAYMENT'
                """,
                (payment.sales_order_id,),
            )
            await _consume_paid_cart_lines(connection, payment.sales_order_id)
            await _ensure_pending_fulfillment(connection, payment.sales_order_id)
        else:
            await connection.execute(
                """
                UPDATE payment_transactions
                SET status = 'FAILED', provider_trade_no = %s, provider_callback_id = %s,
                    callback_received_at = NOW(), failed_at = NOW(), updated_at = NOW(), version = version + 1
                WHERE id = %s AND status IN ('PENDING', 'PROCESSING')
                """,
                (provider_trade_no, provider_callback_id, payment.payment_id),
            )
            await connection.execute(
                """
                UPDATE sales_orders
                SET status = 'PAYMENT_FAILED', updated_at = NOW(), version = version + 1
                WHERE id = %s AND status = 'PENDING_PAYMENT'
                """,
                (payment.sales_order_id,),
            )
        await connection.commit()
        return True
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def get_customer_pending_payment(
    customer_user_id: int,
    order_no: str,
) -> CustomerPendingPayment | None:
    """读取客户本人一笔仍待确认的沙箱支付，避免查询他人订单。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT p.merchant_payment_no, p.amount_cents, MIN(i.product_name),
                   p.provider, p.provider_txn_time
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            JOIN sales_order_items AS i ON i.sales_order_id = o.id
            WHERE o.customer_user_id = %s
              AND o.order_no = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
            GROUP BY p.merchant_payment_no, p.amount_cents, p.provider, p.provider_txn_time
            """,
            (customer_user_id, order_no),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return CustomerPendingPayment(
            merchant_payment_no=str(row[0]),
            amount_cents=int(row[1]),
            subject=str(row[2]),
            provider=_payment_provider_from_row(row[3]),
            provider_txn_time=str(row[4]) if row[4] is not None else None,
        )
    finally:
        await put_connection(connection)


async def get_unionpay_front_return_payment(
    merchant_payment_no: str,
) -> UnionPayFrontReturnPayment | None:
    """仅按银联 provider 和已签名商户交易号读取本地支付事实。

    该入口不接受客户身份，也不直接推进状态；调用方必须先完成银联回报验签，
    随后仍需用 queryTrans 的已验真结果调用通用成功收敛函数。
    """
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, o.customer_user_id, p.merchant_payment_no,
                   p.amount_cents, p.provider, p.provider_txn_time,
                   p.status, o.status
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            WHERE p.merchant_payment_no = %s
              AND p.provider = 'unionpay_test'
            """,
            (merchant_payment_no,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return UnionPayFrontReturnPayment(
            order_no=str(row[0]),
            customer_user_id=int(row[1]),
            merchant_payment_no=str(row[2]),
            amount_cents=int(row[3]),
            provider=_payment_provider_from_row(row[4]),
            provider_txn_time=str(row[5]) if row[5] is not None else None,
            payment_status=str(row[6]),
            order_status=str(row[7]),
        )
    finally:
        await put_connection(connection)


async def find_reusable_pending_checkout(
    customer_user_id: int,
    product: CheckoutProduct,
    quantity: int,
    provider: PaymentProviderName = "alipay_sandbox",
) -> ReusablePendingCheckout | None:
    """返回同一商品、数量和价格的最近待支付订单，避免重试生成废单。

    价格变化时不复用旧快照，确保客户重新发起时看到的是当前商品价格。
    """
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, p.merchant_payment_no, p.amount_cents, i.product_name,
                   p.provider, p.provider_txn_time
            FROM sales_orders AS o
            JOIN sales_order_items AS i ON i.sales_order_id = o.id
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            WHERE o.customer_user_id = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
              AND p.provider = %s
              AND p.merchant_payment_no LIKE %s
              AND i.catalog_category = %s
              AND i.catalog_product_id = %s
              AND i.quantity = %s
              AND i.unit_amount_cents = %s
            ORDER BY o.created_at DESC
            LIMIT 1
            """,
            (
                customer_user_id,
                provider,
                f"{CURRENT_PAYMENT_NO_PREFIX}%",
                product.category,
                product.product_id,
                quantity,
                product.unit_amount_cents,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return ReusablePendingCheckout(
            order_no=str(row[0]),
            merchant_payment_no=str(row[1]),
            amount_cents=int(row[2]),
            subject=str(row[3]),
            provider=_payment_provider_from_row(row[4]),
            provider_txn_time=str(row[5]) if row[5] is not None else None,
        )
    finally:
        await put_connection(connection)


async def get_customer_latest_pending_checkout(
    customer_user_id: int,
    provider: PaymentProviderName = "alipay_sandbox",
) -> ReusablePendingCheckout | None:
    """返回客户最近一笔待支付订单，防止并行结算产生多笔悬挂支付。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, p.merchant_payment_no, p.amount_cents, MIN(i.product_name),
                   p.provider, p.provider_txn_time
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            JOIN sales_order_items AS i ON i.sales_order_id = o.id
            WHERE o.customer_user_id = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
              AND p.provider = %s
            GROUP BY o.order_no, p.merchant_payment_no, p.amount_cents, o.created_at,
                     p.provider, p.provider_txn_time
            ORDER BY o.created_at DESC
            LIMIT 1
            """,
            (customer_user_id, provider),
        )
        row = await cursor.fetchone()
        return (
            None
            if row is None
            else ReusablePendingCheckout(
                str(row[0]),
                str(row[1]),
                int(row[2]),
                str(row[3]),
                _payment_provider_from_row(row[4]),
                str(row[5]) if row[5] is not None else None,
            )
        )
    finally:
        await put_connection(connection)


async def find_reusable_pending_cart_checkout(
    customer_user_id: int,
    cart_lines: list[CartCheckoutLine],
    total_amount_cents: int,
    provider: PaymentProviderName = "alipay_sandbox",
) -> ReusablePendingCheckout | None:
    """只复用与当前购物车快照完全一致的待支付订单。

    当前购物车发生增删或数量变化时，旧订单的金额和商品快照不能继续使用；否则
    浏览器会把新购物车错误地带到旧的支付宝交易。
    """
    if any(line.unit_amount_cents is None for line in cart_lines):
        return None
    expected_lines = sorted(
        (line.category, line.product_id, line.quantity, line.unit_amount_cents) for line in cart_lines
    )
    if not expected_lines:
        return None

    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, p.merchant_payment_no, p.amount_cents,
                   c.catalog_category, c.catalog_product_id, c.quantity, i.unit_amount_cents,
                   i.product_name, p.provider, p.provider_txn_time
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            -- checkout_cart_lines is the cart-origin marker.  A direct-buy order
            -- can have identical sales_order_items, but must never be reused by
            -- cart checkout because it may carry an unrelated payment attempt.
            JOIN checkout_cart_lines AS c
              ON c.sales_order_id = o.id AND c.consumed_at IS NULL
            JOIN sales_order_items AS i
              ON i.sales_order_id = o.id
             AND i.catalog_category = c.catalog_category
             AND i.catalog_product_id = c.catalog_product_id
            WHERE o.customer_user_id = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
              AND p.provider = %s
              AND p.merchant_payment_no LIKE %s
            ORDER BY o.created_at DESC
            """,
            (customer_user_id, provider, f"{CURRENT_PAYMENT_NO_PREFIX}%"),
        )
        rows = await cursor.fetchall()
        candidates: dict[tuple[str, str, int], tuple[list[tuple[str, str, int, int]], str, str, str | None]] = {}
        for row in rows:
            key = (str(row[0]), str(row[1]), int(row[2]))
            lines, subject, stored_provider, provider_txn_time = candidates.setdefault(
                key,
                (
                    [],
                    str(row[7]),
                    str(row[8]) if len(row) > 8 and row[8] is not None else provider,
                    str(row[9]) if len(row) > 9 and row[9] is not None else None,
                ),
            )
            lines.append((str(row[3]), str(row[4]), int(row[5]), int(row[6])))

        for (order_no, merchant_payment_no, amount_cents), (
            existing_lines,
            subject,
            stored_provider,
            provider_txn_time,
        ) in candidates.items():
            if amount_cents == total_amount_cents and sorted(existing_lines) == expected_lines:
                return ReusablePendingCheckout(
                    order_no=order_no,
                    merchant_payment_no=merchant_payment_no,
                    amount_cents=amount_cents,
                    subject=subject,
                    provider=_payment_provider_from_row(stored_provider),
                    provider_txn_time=provider_txn_time,
                )
        return None
    finally:
        await put_connection(connection)


async def apply_alipay_trade_query(
    *,
    merchant_payment_no: str,
    provider_trade_no: str,
    amount_cents: int,
    trade_status: str,
) -> bool:
    """依据支付宝主动查询结果幂等推进本地订单，不伪造异步回调标识。"""
    if trade_status not in {"TRADE_SUCCESS", "TRADE_CLOSED"}:
        return True
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            SELECT id, sales_order_id, amount_cents, status
            FROM payment_transactions
            WHERE merchant_payment_no = %s AND provider = 'alipay_sandbox'
            FOR UPDATE
            """,
            (merchant_payment_no,),
        )
        row = await cursor.fetchone()
        if row is None or int(row[2]) != amount_cents:
            await connection.rollback()
            return False
        if str(row[3]) == "SUCCEEDED":
            await _consume_paid_cart_lines(connection, row[1])
            await _ensure_pending_fulfillment(connection, row[1])
            await connection.commit()
            return True
        if trade_status == "TRADE_SUCCESS":
            await connection.execute(
                """
                UPDATE payment_transactions
                SET status = 'SUCCEEDED', provider_trade_no = %s,
                    succeeded_at = NOW(), updated_at = NOW(), version = version + 1
                WHERE id = %s AND status IN ('PENDING', 'PROCESSING')
                """,
                (provider_trade_no, row[0]),
            )
            await connection.execute(
                """
                UPDATE sales_orders SET status = 'PAID', updated_at = NOW(), version = version + 1
                WHERE id = %s AND status = 'PENDING_PAYMENT'
                """,
                (row[1],),
            )
            await _consume_paid_cart_lines(connection, row[1])
            await _ensure_pending_fulfillment(connection, row[1])
        else:
            await connection.execute(
                """
                UPDATE payment_transactions
                SET status = 'CLOSED', provider_trade_no = %s,
                    failed_at = NOW(), updated_at = NOW(), version = version + 1
                WHERE id = %s AND status IN ('PENDING', 'PROCESSING')
                """,
                (provider_trade_no, row[0]),
            )
            await connection.execute(
                """
                UPDATE sales_orders SET status = 'PAYMENT_FAILED', updated_at = NOW(), version = version + 1
                WHERE id = %s AND status = 'PENDING_PAYMENT'
                """,
                (row[1],),
            )
        await connection.commit()
        return True
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def apply_verified_payment_success(
    *,
    provider: PaymentProviderName,
    merchant_payment_no: str,
    provider_trade_no: str,
    amount_cents: int,
) -> bool:
    """在单一事务中收敛已完成验真的支付，并幂等消费订单后置动作。

    调用方必须在进入本函数前完成供应商签名、订单号、交易时间和金额等外部报文
    校验。本函数只接受非空供应商交易号，并再次按 provider 锁定本地支付记录，
    防止不同支付渠道的交易结果交叉收敛。
    """
    _validate_payment_provider(provider)
    if not provider_trade_no:
        return False
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            SELECT id, sales_order_id, amount_cents, status
            FROM payment_transactions
            WHERE merchant_payment_no = %s AND provider = %s
            FOR UPDATE
            """,
            (merchant_payment_no, provider),
        )
        row = await cursor.fetchone()
        if row is None or int(row[2]) != amount_cents:
            await connection.rollback()
            return False
        status = str(row[3])
        if status == "SUCCEEDED":
            await _consume_paid_cart_lines(connection, row[1])
            await _ensure_pending_fulfillment(connection, row[1])
            await connection.commit()
            return True
        if status not in {"PENDING", "PROCESSING"}:
            await connection.rollback()
            return False

        await connection.execute(
            """
            UPDATE payment_transactions
            SET status = 'SUCCEEDED', provider_trade_no = %s,
                succeeded_at = NOW(), updated_at = NOW(), version = version + 1
            WHERE id = %s AND provider = %s AND status IN ('PENDING', 'PROCESSING')
            """,
            (provider_trade_no, row[0], provider),
        )
        await connection.execute(
            """
            UPDATE sales_orders
            SET status = 'PAID', updated_at = NOW(), version = version + 1
            WHERE id = %s AND status = 'PENDING_PAYMENT'
            """,
            (row[1],),
        )
        await _consume_paid_cart_lines(connection, row[1])
        await _ensure_pending_fulfillment(connection, row[1])
        await connection.commit()
        return True
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def _consume_paid_cart_lines(connection: AsyncConnection, sales_order_id: UUID) -> None:
    """保守消费一笔已付款购物车订单对应的购物车快照。

    ``consumed_at`` 是幂等闸门：支付宝回调和主动查询即使同时或重复确认成功，
    也只有第一笔事务能取得待消费行。消费时还会比较购物车行的 ``updated_at``
    与快照时间；客户付款前调数量、删除后重加或加入同款时，不强行覆盖其新选择。
    """
    cursor = await connection.execute(
        """
        UPDATE checkout_cart_lines
        SET consumed_at = NOW()
        WHERE sales_order_id = %s AND consumed_at IS NULL
        RETURNING catalog_category, catalog_product_id, quantity, created_at
        """,
        (sales_order_id,),
    )
    for category, product_id, quantity, snapshot_created_at in await cursor.fetchall():
        await connection.execute(
            """
            DELETE FROM cart_items AS i
            USING carts AS c
            WHERE i.cart_id = c.id
              AND c.customer_user_id = (
                  SELECT customer_user_id FROM sales_orders WHERE id = %s
              )
              AND i.catalog_category = %s
              AND i.catalog_product_id = %s
              AND i.quantity = %s
              AND i.updated_at <= %s
            """,
            (sales_order_id, category, product_id, quantity, snapshot_created_at),
        )
        await connection.execute(
            """
            UPDATE cart_items AS i
            SET quantity = i.quantity - %s, updated_at = NOW()
            FROM carts AS c
            WHERE i.cart_id = c.id
              AND c.customer_user_id = (
                  SELECT customer_user_id FROM sales_orders WHERE id = %s
              )
              AND i.catalog_category = %s
              AND i.catalog_product_id = %s
              AND i.quantity > %s
              AND i.updated_at <= %s
            """,
            (quantity, sales_order_id, category, product_id, quantity, snapshot_created_at),
        )


async def list_customer_checkout_orders(customer_user_id: int, limit: int = 30) -> list[CustomerCheckoutOrder]:
    """列出当前客户创建的新结算订单，不与 legacy orders 混写。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, o.status, o.total_amount_cents, MIN(i.product_name), SUM(i.quantity),
                   p.status, f.status, f.carrier, f.tracking_number, o.created_at,
                   r.id, r.status, r.amount_cents, p.provider,
                   CASE
                       WHEN o.status = 'PAID'
                        AND p.status = 'SUCCEEDED'
                        AND COALESCE(f.status, 'PENDING_FULFILLMENT') = 'PENDING_FULFILLMENT'
                        AND p.succeeded_at >= NOW() - INTERVAL '7 days'
                        AND r.id IS NULL
                        AND p.provider IN ('alipay_sandbox', 'unionpay_test')
                       THEN TRUE ELSE FALSE
                   END
            FROM sales_orders AS o
            JOIN sales_order_items AS i ON i.sales_order_id = o.id
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            LEFT JOIN fulfillments AS f ON f.sales_order_id = o.id
            LEFT JOIN checkout_refunds AS r ON r.sales_order_id = o.id
            WHERE o.customer_user_id = %s
            GROUP BY o.order_no, o.status, o.total_amount_cents, p.status,
                     f.status, f.carrier, f.tracking_number, o.created_at, r.id, r.status,
                     r.amount_cents, p.provider, p.succeeded_at
            ORDER BY o.created_at DESC
            LIMIT %s
            """,
            (customer_user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            CustomerCheckoutOrder(
                order_no=str(row[0]),
                status=str(row[1]),
                total_amount_cents=int(row[2]),
                product_name=str(row[3]),
                quantity=int(row[4]),
                payment_status=str(row[5]),
                # The first paid order may predate this table. It is still
                # accurately awaiting fulfillment, even before a later
                # backfill creates its physical fulfillment row.
                fulfillment_status=str(row[6])
                if row[6] is not None
                else ("PENDING_FULFILLMENT" if str(row[0]) and str(row[1]) == "PAID" else None),
                tracking_company=str(row[7]) if row[7] is not None else None,
                tracking_number=str(row[8]) if row[8] is not None else None,
                created_at=str(row[9]),
                refund_id=str(row[10]) if row[10] is not None else None,
                refund_status=str(row[11]) if row[11] is not None else None,
                refund_amount_cents=int(row[12]) if row[12] is not None else None,
                provider=(
                    _payment_provider_from_row(row[13]) if len(row) > 13 and row[13] is not None else "alipay_sandbox"
                ),
                refund_eligible=bool(row[14]) if len(row) > 14 else False,
            )
            for row in rows
        ]
    finally:
        await put_connection(connection)


async def get_customer_pending_checkout(
    customer_user_id: int,
    order_no: str,
) -> CustomerPendingCheckout | None:
    """读取客户本人可取消的待支付订单，不把订单事实暴露给其他用户。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, p.merchant_payment_no, p.provider, p.provider_txn_time
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            WHERE o.customer_user_id = %s
              AND o.order_no = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
            """,
            (customer_user_id, order_no),
        )
        row = await cursor.fetchone()
        return (
            None
            if row is None
            else CustomerPendingCheckout(
                str(row[0]),
                str(row[1]),
                (_payment_provider_from_row(row[2]) if len(row) > 2 and row[2] is not None else "alipay_sandbox"),
                str(row[3]) if len(row) > 3 and row[3] is not None else None,
            )
        )
    finally:
        await put_connection(connection)


async def cancel_customer_pending_checkout(
    customer_user_id: int,
    order_no: str,
    provider: PaymentProviderName = "alipay_sandbox",
) -> bool:
    """条件取消客户自己的本地待支付订单，返回是否由本次调用成功推进。"""
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            UPDATE payment_transactions AS p
            SET status = 'CLOSED', failed_at = NOW(), updated_at = NOW(), version = p.version + 1
            FROM sales_orders AS o
            WHERE p.sales_order_id = o.id
              AND o.customer_user_id = %s
              AND o.order_no = %s
              AND o.status = 'PENDING_PAYMENT'
              AND p.status IN ('PENDING', 'PROCESSING')
              AND p.provider = %s
            RETURNING p.sales_order_id
            """,
            (customer_user_id, order_no, provider),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return False
        await connection.execute(
            """
            UPDATE sales_orders
            SET status = 'CANCELLED', updated_at = NOW(), version = version + 1
            WHERE id = %s AND status = 'PENDING_PAYMENT'
            """,
            (row[0],),
        )
        await connection.commit()
        return True
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def list_operator_fulfillments(limit: int = 50) -> list[OperatorFulfillment]:
    """读取应用自有订单的履约队列，供运营发货工作台使用。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT o.order_no, i.product_name, i.quantity,
                   COALESCE(f.status, 'PENDING_FULFILLMENT'),
                   f.carrier, f.tracking_number, f.created_at
            FROM sales_orders AS o
            JOIN sales_order_items AS i ON i.sales_order_id = o.id
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            LEFT JOIN fulfillments AS f ON f.sales_order_id = o.id
            WHERE o.status = 'PAID' AND p.status = 'SUCCEEDED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM checkout_refunds AS r
                  WHERE r.sales_order_id = o.id
                    AND r.status IN ('PENDING_CONFIRMATION', 'PENDING_FINANCE_APPROVAL', 'PROCESSING')
              )
            ORDER BY CASE COALESCE(f.status, 'PENDING_FULFILLMENT')
                         WHEN 'PENDING_FULFILLMENT' THEN 0
                         WHEN 'EXCEPTION' THEN 1
                         WHEN 'SHIPPED' THEN 2
                         ELSE 3
                     END,
                     f.created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [
            OperatorFulfillment(
                order_no=str(row[0]),
                product_name=str(row[1]),
                quantity=int(row[2]),
                status=str(row[3]),
                carrier=str(row[4]) if row[4] is not None else None,
                tracking_number=str(row[5]) if row[5] is not None else None,
                created_at=str(row[6]),
            )
            for row in await cursor.fetchall()
        ]
    finally:
        await put_connection(connection)


async def mark_fulfillment_shipped(
    *,
    order_no: str,
    carrier: str,
    tracking_number: str,
) -> OperatorFulfillment | None:
    """原子将一笔待发货订单推进到已发货。

    Returns:
        更新后的履约摘要；订单不存在、已处理或不属于应用订单时返回 None。
    """
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        # 迁移前已支付的 application 订单没有履约行。只在运营明确登记
        # 发货时补建该行；绝不读取或修改 legacy orders。
        # Lock the same mutable order/payment/fulfillment facts used by the
        # refund submission boundary before deciding whether shipping may win.
        await connection.execute(
            """
            SELECT o.id
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            LEFT JOIN fulfillments AS f ON f.sales_order_id = o.id
            WHERE o.order_no = %s
            FOR UPDATE OF o, p
            """,
            (order_no,),
        )
        await connection.execute(
            """
            INSERT INTO fulfillments (id, sales_order_id, status)
            SELECT %s, o.id, 'PENDING_FULFILLMENT'
            FROM sales_orders AS o
            JOIN payment_transactions AS p ON p.sales_order_id = o.id
            WHERE o.order_no = %s AND o.status = 'PAID' AND p.status = 'SUCCEEDED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM checkout_refunds AS r
                  WHERE r.sales_order_id = o.id
                    AND r.status IN ('PENDING_CONFIRMATION', 'PENDING_FINANCE_APPROVAL', 'PROCESSING')
              )
            ON CONFLICT (sales_order_id) DO NOTHING
            """,
            (uuid4(), order_no),
        )
        cursor = await connection.execute(
            """
            WITH candidate AS (
                SELECT f.id
                FROM fulfillments AS f
                JOIN sales_orders AS o ON o.id = f.sales_order_id
                JOIN payment_transactions AS p ON p.sales_order_id = o.id
                WHERE o.order_no = %s
                  AND o.status = 'PAID'
                  AND p.status = 'SUCCEEDED'
                  AND f.status = 'PENDING_FULFILLMENT'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM checkout_refunds AS r
                      WHERE r.sales_order_id = o.id
                        AND r.status IN ('PENDING_CONFIRMATION', 'PENDING_FINANCE_APPROVAL', 'PROCESSING')
                  )
                FOR UPDATE OF f, o, p
            )
            UPDATE fulfillments AS f
            SET status = 'SHIPPED', carrier = %s, tracking_number = %s,
                shipped_at = NOW(), updated_at = NOW(), version = version + 1
            FROM candidate
            WHERE f.id = candidate.id
            RETURNING (
                SELECT o.order_no FROM sales_orders AS o WHERE o.id = f.sales_order_id
            ),
            (
                SELECT i.product_name FROM sales_order_items AS i WHERE i.sales_order_id = f.sales_order_id
            ),
            (
                SELECT i.quantity FROM sales_order_items AS i WHERE i.sales_order_id = f.sales_order_id
            ),
            f.status, f.carrier, f.tracking_number, f.created_at
            """,
            (order_no, carrier, tracking_number),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return None
        await connection.commit()
        return OperatorFulfillment(
            order_no=str(row[0]),
            product_name=str(row[1]),
            quantity=int(row[2]),
            status=str(row[3]),
            carrier=str(row[4]),
            tracking_number=str(row[5]),
            created_at=str(row[6]),
        )
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def _ensure_pending_fulfillment(connection: AsyncConnection, sales_order_id: UUID) -> None:
    """为已成功支付的应用订单创建唯一的初始履约记录。

    Args:
        connection: 当前支付收敛事务使用的异步数据库连接。
        sales_order_id: 已成功付款的应用自有订单 ID。

    Returns:
        None。重复回调不会创建第二条履约记录。
    """
    await connection.execute(
        """
        INSERT INTO fulfillments (id, sales_order_id, status)
        VALUES (%s, %s, 'PENDING_FULFILLMENT')
        ON CONFLICT (sales_order_id) DO NOTHING
        """,
        (uuid4(), sales_order_id),
    )
