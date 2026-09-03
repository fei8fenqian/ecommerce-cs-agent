"""应用自有 checkout 退款数据访问；绝不回写 legacy orders。"""

from dataclasses import dataclass
from uuid import UUID

from infra.db_pool import get_connection, put_connection


class RefundProviderUnsupportedError(ValueError):
    """当前退款适配器不支持这笔支付渠道。"""


_SUPPORTED_REFUND_PROVIDERS = frozenset({"alipay_sandbox", "unionpay_test"})
_ACTIVE_REFUND_STATUSES = ("PENDING_CONFIRMATION", "PENDING_FINANCE_APPROVAL", "PROCESSING")


class RefundIdempotencyConflictError(ValueError):
    """同一客户幂等键已被用于另一笔订单，不能回放到错误资源。"""


@dataclass(frozen=True)
class CheckoutRefund:
    """一笔客户全额退款的可展示、安全摘要。"""

    refund_id: UUID
    order_no: str
    merchant_payment_no: str
    merchant_refund_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    requested_at: str
    processing_at: str | None = None
    provider: str = "alipay_sandbox"
    provider_trade_no: str | None = None
    provider_txn_time: str | None = None


@dataclass(frozen=True)
class FinanceRefund:
    """财务工作台可读取的退款摘要，不包含客户身份信息。"""

    refund_id: UUID
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    requested_at: str
    finance_decision_note: str = ""
    finance_decided_at: str | None = None


@dataclass(frozen=True)
class FinanceAnomaly:
    """财务异常队列中的只读事实摘要。"""

    anomaly_type: str
    reference_id: str
    order_no: str
    status: str
    amount_cents: int
    currency: str
    reason: str
    occurred_at: str
    age_seconds: int


@dataclass(frozen=True)
class RefundConfirmationStart:
    """确认退款后，应用服务是否应当首次调用支付宝。"""

    refund: CheckoutRefund
    should_submit_to_provider: bool


@dataclass(frozen=True)
class FinanceDecisionStart:
    """财务审批后的退款提交资格；同一幂等键只允许一次资金提交。"""

    refund: CheckoutRefund
    should_submit_to_provider: bool
    idempotent_replay: bool


def _refund_from_row(row: tuple[object, ...]) -> CheckoutRefund:
    """将固定查询列转换为退款摘要，避免上层依赖数据库行索引。"""
    return CheckoutRefund(
        refund_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
        order_no=str(row[1]),
        merchant_payment_no=str(row[2]),
        merchant_refund_no=str(row[3]),
        status=str(row[4]),
        amount_cents=int(str(row[5])),
        currency=str(row[6]),
        reason=str(row[7]),
        requested_at=str(row[8]),
        processing_at=str(row[9]) if len(row) > 9 and row[9] is not None else None,
        provider=str(row[10]) if len(row) > 10 and row[10] is not None else "alipay_sandbox",
        provider_trade_no=str(row[11]) if len(row) > 11 and row[11] is not None else None,
        provider_txn_time=str(row[12]) if len(row) > 12 and row[12] is not None else None,
    )


_REFUND_COLUMNS = """
    r.id, o.order_no, p.merchant_payment_no, r.merchant_refund_no,
    r.status, r.amount_cents, r.currency, r.reason, r.requested_at, r.processing_at,
    p.provider, p.provider_trade_no, p.provider_txn_time
"""
_REFUND_COLUMN_COUNT = 13

_REFUND_ELIGIBILITY_QUERY = """
    SELECT o.id, p.id, p.merchant_payment_no, o.total_amount_cents,
           p.amount_cents, p.provider
    FROM sales_orders AS o
    JOIN payment_transactions AS p ON p.sales_order_id = o.id
    JOIN fulfillments AS f ON f.sales_order_id = o.id
    WHERE o.customer_user_id = %s
      AND o.order_no = %s
      AND o.status = 'PAID'
      AND p.status = 'SUCCEEDED'
      AND p.currency = 'CNY'
      AND f.status = 'PENDING_FULFILLMENT'
      AND p.succeeded_at >= NOW() - INTERVAL '7 days'
      AND NOT EXISTS (
          SELECT 1
          FROM checkout_refunds AS existing_refund
          WHERE existing_refund.sales_order_id = o.id
      )
"""


def _refund_row_is_eligible(row: tuple[object, ...] | None) -> bool:
    return row is not None and int(str(row[3])) == int(str(row[4])) and int(str(row[3])) > 0


def _ensure_supported_refund_provider(provider: object) -> None:
    if str(provider) not in _SUPPORTED_REFUND_PROVIDERS:
        raise RefundProviderUnsupportedError("current refund provider is not supported")


async def get_customer_refund_eligibility(*, customer_user_id: int, order_no: str) -> bool:
    """读取当前客户订单是否满足首版全额退款前置规则。

    此查询仅用于 Agent 的决策事实。真实写入仍会在同一资格条件下加锁并重新校验。
    """
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            _REFUND_ELIGIBILITY_QUERY,
            (customer_user_id, order_no),
        )
        row = await cursor.fetchone()
        if row is None or not _refund_row_is_eligible(row):
            return False
        _ensure_supported_refund_provider(row[5])
        return True
    finally:
        await put_connection(connection)


async def _select_refund(connection: object, refund_id: UUID) -> CheckoutRefund | None:
    """在状态更新后通过联表查询完整退款摘要。

    ``UPDATE ... RETURNING`` 只能直接返回被更新表的列，不能返回订单和支付表
    的联表列；统一在同一事务中重新查询，避免把联表字段塞进 UPDATE 的 RETURNING。
    """
    cursor = await connection.execute(  # type: ignore[attr-defined]
        f"""
        SELECT {_REFUND_COLUMNS}
        FROM checkout_refunds AS r
        JOIN sales_orders AS o ON o.id = r.sales_order_id
        JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
        WHERE r.id = %s
        """,
        (refund_id,),
    )
    row = await cursor.fetchone()
    return _refund_from_row(row) if row is not None else None


async def list_finance_refunds(*, limit: int = 100) -> list[FinanceRefund]:
    """列出财务可见的 checkout 退款摘要。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT r.id, o.order_no, r.status, r.amount_cents,
                   r.currency, r.reason, r.requested_at,
                   r.finance_decision_note, r.finance_decided_at
            FROM public.checkout_refunds AS r
            JOIN public.sales_orders AS o ON o.id = r.sales_order_id
            ORDER BY r.requested_at DESC, r.id DESC
            LIMIT %s
            """,
            (max(1, min(limit, 100)),),
        )
        rows = await cursor.fetchall()
        return [
            FinanceRefund(
                refund_id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
                order_no=str(row[1]),
                status=str(row[2]),
                amount_cents=int(str(row[3])),
                currency=str(row[4]),
                reason=str(row[5]),
                requested_at=str(row[6]),
                finance_decision_note=str(row[7] or ""),
                finance_decided_at=str(row[8]) if row[8] is not None else None,
            )
            for row in rows
        ]
    finally:
        await put_connection(connection)


async def list_finance_anomalies(*, timeout_minutes: int = 30, limit: int = 100) -> list[FinanceAnomaly]:
    """扫描退款和支付事实，返回需要财务关注的只读异常。

    该查询只读取应用自有 checkout 表，不改变资金状态。``timeout_minutes``
    用于识别长时间未收敛的处理中支付/退款，具体动作仍由财务页面和确定性服务完成。
    """
    bounded_timeout = max(5, min(timeout_minutes, 24 * 60))
    bounded_limit = max(1, min(limit, 200))
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            WITH anomalies AS (
                SELECT
                    'REFUND_PENDING_APPROVAL' AS anomaly_type,
                    r.id::text AS reference_id,
                    o.order_no,
                    r.status,
                    r.amount_cents,
                    r.currency,
                    r.reason,
                    r.requested_at AS occurred_at
                FROM public.checkout_refunds AS r
                JOIN public.sales_orders AS o ON o.id = r.sales_order_id
                WHERE r.status = 'PENDING_FINANCE_APPROVAL'

                UNION ALL

                SELECT
                    'REFUND_FAILED' AS anomaly_type,
                    r.id::text AS reference_id,
                    o.order_no,
                    r.status,
                    r.amount_cents,
                    r.currency,
                    r.reason,
                    COALESCE(r.failed_at, r.updated_at) AS occurred_at
                FROM public.checkout_refunds AS r
                JOIN public.sales_orders AS o ON o.id = r.sales_order_id
                WHERE r.status = 'FAILED'

                UNION ALL

                SELECT
                    'REFUND_PROCESSING_TIMEOUT' AS anomaly_type,
                    r.id::text AS reference_id,
                    o.order_no,
                    r.status,
                    r.amount_cents,
                    r.currency,
                    r.reason,
                    COALESCE(r.processing_at, r.updated_at) AS occurred_at
                FROM public.checkout_refunds AS r
                JOIN public.sales_orders AS o ON o.id = r.sales_order_id
                WHERE r.status = 'PROCESSING'
                  AND COALESCE(r.processing_at, r.updated_at)
                      < NOW() - (%s * INTERVAL '1 minute')

                UNION ALL

                SELECT
                    CASE WHEN p.status = 'PENDING'
                         THEN 'PAYMENT_PENDING_TIMEOUT'
                         ELSE 'PAYMENT_PROCESSING_TIMEOUT'
                    END AS anomaly_type,
                    p.id::text AS reference_id,
                    o.order_no,
                    p.status,
                    p.amount_cents,
                    p.currency,
                    '支付状态长时间未确认' AS reason,
                    p.created_at AS occurred_at
                FROM public.payment_transactions AS p
                JOIN public.sales_orders AS o ON o.id = p.sales_order_id
                WHERE p.status IN ('PENDING', 'PROCESSING')
                  AND o.status NOT IN ('CANCELLED', 'PAYMENT_FAILED')
                  AND p.created_at < NOW() - (%s * INTERVAL '1 minute')
            )
            SELECT anomaly_type, reference_id, order_no, status, amount_cents,
                   currency, reason, occurred_at,
                   FLOOR(EXTRACT(EPOCH FROM (NOW() - occurred_at)))::bigint AS age_seconds
            FROM anomalies
            ORDER BY occurred_at ASC, reference_id ASC
            LIMIT %s
            """,
            (bounded_timeout, bounded_timeout, bounded_limit),
        )
        rows = await cursor.fetchall()
        return [
            FinanceAnomaly(
                anomaly_type=str(row[0]),
                reference_id=str(row[1]),
                order_no=str(row[2]),
                status=str(row[3]),
                amount_cents=int(str(row[4])),
                currency=str(row[5]),
                reason=str(row[6] or ""),
                occurred_at=str(row[7]),
                age_seconds=max(0, int(row[8] or 0)),
            )
            for row in rows
        ]
    finally:
        await put_connection(connection)


async def create_customer_refund_request(
    *,
    refund_id: UUID,
    customer_user_id: int,
    order_no: str,
    merchant_refund_no: str,
    request_idempotency_key: str,
    reason: str,
    status: str = "PENDING_CONFIRMATION",
) -> CheckoutRefund | None:
    """原子创建客户本人的待确认全额退款。

    只有支付成功、仍待发货、七日内的 checkout 订单可进入该状态。相同
    Idempotency-Key 会返回既有请求，不会新增第二笔退款。
    """
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        if status not in {"AUTO", "PENDING_CONFIRMATION", "PENDING_FINANCE_APPROVAL"}:
            raise ValueError("invalid initial refund status")

        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE r.customer_user_id = %s AND r.request_idempotency_key = %s
            FOR UPDATE OF r
            """,
            (customer_user_id, request_idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            existing_refund = _refund_from_row(existing)
            if existing_refund.order_no != order_no:
                await connection.rollback()
                raise RefundIdempotencyConflictError("idempotency key is bound to another order")
            await connection.commit()
            return existing_refund

        # A lost client response may be retried with a fresh browser key.  The
        # customer/order predicate makes returning its one refund safe and
        # avoids surfacing the table unique constraint as a 500.
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE o.customer_user_id = %s AND o.order_no = %s
            FOR UPDATE OF r
            """,
            (customer_user_id, order_no),
        )
        existing_for_order = await cursor.fetchone()
        if existing_for_order is not None:
            await connection.commit()
            return _refund_from_row(existing_for_order)

        cursor = await connection.execute(
            _REFUND_ELIGIBILITY_QUERY + " FOR UPDATE OF o, p, f",
            (customer_user_id, order_no),
        )
        eligible = await cursor.fetchone()
        if not _refund_row_is_eligible(eligible):
            await connection.rollback()
            return None
        try:
            _ensure_supported_refund_provider(eligible[5])
        except RefundProviderUnsupportedError:
            await connection.rollback()
            raise
        if status == "AUTO":
            # 首版把金额门槛作为确定性分流；完整事实策略仍由后续资格服务补齐。
            status = "PENDING_CONFIRMATION" if int(eligible[3]) <= 200000 else "PENDING_FINANCE_APPROVAL"

        cursor = await connection.execute(
            """
            INSERT INTO checkout_refunds (
                id, sales_order_id, payment_transaction_id, customer_user_id,
                merchant_refund_no, request_idempotency_key, status, amount_cents, currency, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'CNY', %s)
            RETURNING id
            """,
            (
                refund_id,
                eligible[0],
                eligible[1],
                customer_user_id,
                merchant_refund_no,
                request_idempotency_key,
                status,
                int(eligible[3]),
                reason,
            ),
        )
        created_row = await cursor.fetchone()
        if created_row is None:
            await connection.rollback()
            return None
        created = await _select_refund(connection, created_row[0])
        if created is None:
            await connection.rollback()
            return None
        await connection.commit()
        return created
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def start_customer_refund_confirmation(
    *,
    customer_user_id: int,
    refund_id: UUID,
    confirmation_idempotency_key: str,
) -> RefundConfirmationStart | None:
    """以条件更新取得唯一的支付渠道退款调用权。

    同一确认键的网络重放只返回进行中的退款，不会第二次提交给支付宝。
    """
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}, r.confirmation_idempotency_key,
                   o.status, p.status, f.status
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            JOIN fulfillments AS f ON f.sales_order_id = o.id
            WHERE r.id = %s AND r.customer_user_id = %s
              AND p.provider IN ('alipay_sandbox', 'unionpay_test')
            FOR UPDATE OF o, p, f, r
            """,
            (refund_id, customer_user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return None
        refund = _refund_from_row(row[:_REFUND_COLUMN_COUNT])
        saved_confirmation_key = str(row[_REFUND_COLUMN_COUNT]) if row[_REFUND_COLUMN_COUNT] is not None else None
        if refund.status == "PROCESSING" and saved_confirmation_key == confirmation_idempotency_key:
            await connection.commit()
            return RefundConfirmationStart(refund=refund, should_submit_to_provider=False)
        if refund.status != "PENDING_CONFIRMATION":
            await connection.rollback()
            return None
        # The request-time seven-day decision remains valid, but mutable
        # payment/fulfillment facts must be current at the submission boundary.
        if (
            str(row[_REFUND_COLUMN_COUNT + 1]) != "PAID"
            or str(row[_REFUND_COLUMN_COUNT + 2]) != "SUCCEEDED"
            or str(row[_REFUND_COLUMN_COUNT + 3]) != "PENDING_FULFILLMENT"
        ):
            await connection.rollback()
            return None

        cursor = await connection.execute(
            """
            UPDATE checkout_refunds AS r
            SET status = 'PROCESSING', confirmation_idempotency_key = %s,
                processing_at = NOW(), updated_at = NOW(), version = r.version + 1
            FROM sales_orders AS o, payment_transactions AS p, fulfillments AS f
            WHERE r.id = %s AND r.sales_order_id = o.id AND r.payment_transaction_id = p.id
              AND f.sales_order_id = o.id
              AND r.status = 'PENDING_CONFIRMATION' AND o.status = 'PAID'
              AND p.status = 'SUCCEEDED' AND f.status = 'PENDING_FULFILLMENT'
            RETURNING r.id
            """,
            (confirmation_idempotency_key, refund_id),
        )
        processing_row = await cursor.fetchone()
        if processing_row is None:
            await connection.rollback()
            return None
        processing = await _select_refund(connection, processing_row[0])
        if processing is None:
            await connection.rollback()
            return None
        cursor = await connection.execute(
            """
            UPDATE sales_orders
            SET status = 'REFUND_PROCESSING', updated_at = NOW(), version = version + 1
            WHERE id = (
                SELECT sales_order_id FROM checkout_refunds WHERE id = %s
            ) AND status = 'PAID'
            RETURNING id
            """,
            (refund_id,),
        )
        if await cursor.fetchone() is None:
            await connection.rollback()
            return None
        await connection.commit()
        return RefundConfirmationStart(refund=processing, should_submit_to_provider=True)
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def start_finance_refund_approval(
    *,
    finance_user_id: int,
    refund_id: UUID,
    decision_idempotency_key: str,
    decision_note: str,
) -> FinanceDecisionStart | None:
    """原子批准待财务退款，并取得唯一的支付渠道提交资格。

    Args:
        finance_user_id: 当前财务用户 ID，写入决策审计字段。
        refund_id: 要处理的应用自有退款记录 UUID。
        decision_idempotency_key: 本次财务命令的稳定幂等键。
        decision_note: 财务的简短处理备注，不包含支付原始报文。

    Returns:
        首次批准时返回 ``should_submit_to_provider=True``；相同幂等键重放时返回
        当前处理中记录且不允许再次调用支付网关。

    Raises:
        ValueError: 幂等键或备注不符合内部边界。
    """
    if not decision_idempotency_key or len(decision_idempotency_key) > 80:
        raise ValueError("invalid finance decision idempotency key")
    if len(decision_note) > 500:
        raise ValueError("finance decision note is too long")

    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}, r.finance_decision_idempotency_key,
                   o.status, p.status, f.status
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            JOIN fulfillments AS f ON f.sales_order_id = o.id
            WHERE r.id = %s
              AND p.provider IN ('alipay_sandbox', 'unionpay_test')
            FOR UPDATE OF o, p, f, r
            """,
            (refund_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return None

        refund = _refund_from_row(row[:_REFUND_COLUMN_COUNT])
        saved_key = str(row[_REFUND_COLUMN_COUNT]) if row[_REFUND_COLUMN_COUNT] is not None else None
        if refund.status == "PROCESSING" and saved_key == decision_idempotency_key:
            await connection.commit()
            return FinanceDecisionStart(refund=refund, should_submit_to_provider=False, idempotent_replay=True)
        if refund.status != "PENDING_FINANCE_APPROVAL":
            await connection.rollback()
            return None
        if (
            str(row[_REFUND_COLUMN_COUNT + 1]) != "PAID"
            or str(row[_REFUND_COLUMN_COUNT + 2]) != "SUCCEEDED"
            or str(row[_REFUND_COLUMN_COUNT + 3]) != "PENDING_FULFILLMENT"
        ):
            await connection.rollback()
            return None

        cursor = await connection.execute(
            """
            UPDATE checkout_refunds AS r
            SET status = 'PROCESSING',
                finance_decision_idempotency_key = %s,
                finance_decided_by = %s,
                finance_decided_at = NOW(),
                finance_decision_note = %s,
                processing_at = NOW(), updated_at = NOW(), version = r.version + 1
            FROM sales_orders AS o, payment_transactions AS p, fulfillments AS f
            WHERE r.id = %s AND r.status = 'PENDING_FINANCE_APPROVAL'
              AND r.sales_order_id = o.id AND r.payment_transaction_id = p.id
              AND f.sales_order_id = o.id
              AND o.status = 'PAID' AND p.status = 'SUCCEEDED' AND f.status = 'PENDING_FULFILLMENT'
            RETURNING r.id
            """,
            (decision_idempotency_key, finance_user_id, decision_note, refund_id),
        )
        processing_row = await cursor.fetchone()
        if processing_row is None:
            await connection.rollback()
            return None
        processing = await _select_refund(connection, processing_row[0])
        if processing is None:
            await connection.rollback()
            return None
        cursor = await connection.execute(
            """
            UPDATE sales_orders
            SET status = 'REFUND_PROCESSING', updated_at = NOW(), version = version + 1
            WHERE id = (SELECT sales_order_id FROM checkout_refunds WHERE id = %s)
              AND status = 'PAID'
            RETURNING id
            """,
            (refund_id,),
        )
        if await cursor.fetchone() is None:
            await connection.rollback()
            return None
        await connection.commit()
        return FinanceDecisionStart(refund=processing, should_submit_to_provider=True, idempotent_replay=False)
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def reject_finance_refund(
    *,
    finance_user_id: int,
    refund_id: UUID,
    decision_idempotency_key: str,
    decision_note: str,
) -> tuple[CheckoutRefund, bool] | None:
    """原子驳回待财务退款；驳回不调用支付网关并保持订单为已支付。

    Args:
        finance_user_id: 当前财务用户 ID。
        refund_id: 要驳回的应用自有退款记录 UUID。
        decision_idempotency_key: 本次财务命令的稳定幂等键。
        decision_note: 驳回原因，供财务和客户后续查看。

    Returns:
        ``(退款摘要, 是否为幂等重放)``；资源不存在、状态已变化或使用了不同命令
        键时返回 None。
    """
    if not decision_idempotency_key or len(decision_idempotency_key) > 80:
        raise ValueError("invalid finance decision idempotency key")
    if len(decision_note) > 500:
        raise ValueError("finance decision note is too long")

    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}, r.finance_decision_idempotency_key
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE r.id = %s
            FOR UPDATE OF r
            """,
            (refund_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return None
        refund = _refund_from_row(row[:_REFUND_COLUMN_COUNT])
        saved_key = str(row[_REFUND_COLUMN_COUNT]) if row[_REFUND_COLUMN_COUNT] is not None else None
        if refund.status == "REJECTED" and saved_key == decision_idempotency_key:
            await connection.commit()
            return refund, True
        if refund.status != "PENDING_FINANCE_APPROVAL":
            await connection.rollback()
            return None

        cursor = await connection.execute(
            """
            UPDATE checkout_refunds
            SET status = 'REJECTED', finance_decision_idempotency_key = %s,
                finance_decided_by = %s, finance_decided_at = NOW(),
                finance_decision_note = %s, updated_at = NOW(), version = version + 1
            WHERE id = %s AND status = 'PENDING_FINANCE_APPROVAL'
            RETURNING id
            """,
            (decision_idempotency_key, finance_user_id, decision_note, refund_id),
        )
        rejected_row = await cursor.fetchone()
        if rejected_row is None:
            await connection.rollback()
            return None
        rejected = await _select_refund(connection, rejected_row[0])
        if rejected is None:
            await connection.rollback()
            return None
        await connection.commit()
        return rejected, False
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def mark_checkout_refund_succeeded(
    *,
    refund_id: UUID,
    provider_refund_reference: str | None,
) -> CheckoutRefund | None:
    """将已被支付宝明确接受的退款收敛为成功，并同步订单终态。"""
    return await _finish_checkout_refund(
        refund_id=refund_id,
        next_refund_status="SUCCEEDED",
        next_order_status="REFUNDED",
        provider_refund_reference=provider_refund_reference,
    )


async def mark_checkout_refund_failed(refund_id: UUID) -> CheckoutRefund | None:
    """记录支付宝明确拒绝的退款，订单恢复为已付款而非伪装成功。"""
    return await _finish_checkout_refund(
        refund_id=refund_id,
        next_refund_status="FAILED",
        next_order_status="PAID",
        provider_refund_reference=None,
    )


async def _finish_checkout_refund(
    *,
    refund_id: UUID,
    next_refund_status: str,
    next_order_status: str,
    provider_refund_reference: str | None,
) -> CheckoutRefund | None:
    """在一个事务内收敛已开始的退款及其 checkout 订单状态。"""
    connection = await get_connection()
    try:
        await connection.execute("BEGIN")
        cursor = await connection.execute(
            """
            SELECT r.id, r.sales_order_id
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE r.id = %s AND r.status = 'PROCESSING'
              AND o.status = 'REFUND_PROCESSING' AND p.status = 'SUCCEEDED'
            FOR UPDATE OF o, p, r
            """,
            (refund_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            await connection.rollback()
            return None

        cursor = await connection.execute(
            """
            UPDATE sales_orders
            SET status = %s, updated_at = NOW(), version = version + 1
            WHERE id = %s AND status = 'REFUND_PROCESSING'
            RETURNING id
            """,
            (next_order_status, row[1]),
        )
        if await cursor.fetchone() is None:
            await connection.rollback()
            return None
        cursor = await connection.execute(
            """
            UPDATE checkout_refunds AS r
            SET status = %s, provider_refund_reference = COALESCE(%s, r.provider_refund_reference),
                succeeded_at = CASE WHEN %s = 'SUCCEEDED' THEN NOW() ELSE r.succeeded_at END,
                failed_at = CASE WHEN %s = 'FAILED' THEN NOW() ELSE r.failed_at END,
                updated_at = NOW(), version = r.version + 1
            WHERE r.id = %s AND r.status = 'PROCESSING'
            RETURNING r.id
            """,
            (next_refund_status, provider_refund_reference, next_refund_status, next_refund_status, refund_id),
        )
        refund_row = await cursor.fetchone()
        if refund_row is None:
            await connection.rollback()
            return None
        updated = await _select_refund(connection, refund_row[0])
        if updated is None:
            await connection.rollback()
            return None
        await connection.commit()
        return updated
    except Exception:
        await connection.rollback()
        raise
    finally:
        await put_connection(connection)


async def get_customer_checkout_refund(customer_user_id: int, refund_id: UUID) -> CheckoutRefund | None:
    """读取客户本人的退款状态，不泄露其他客户的资金记录。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE r.id = %s AND r.customer_user_id = %s
            """,
            (refund_id, customer_user_id),
        )
        row = await cursor.fetchone()
        return _refund_from_row(row) if row is not None else None
    finally:
        await put_connection(connection)


async def get_checkout_refund(refund_id: UUID) -> CheckoutRefund | None:
    """读取一笔退款用于受权的财务只读 reconciliation。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT {_REFUND_COLUMNS}
            FROM checkout_refunds AS r
            JOIN sales_orders AS o ON o.id = r.sales_order_id
            JOIN payment_transactions AS p ON p.id = r.payment_transaction_id
            WHERE r.id = %s
            """,
            (refund_id,),
        )
        row = await cursor.fetchone()
        return _refund_from_row(row) if row is not None else None
    finally:
        await put_connection(connection)
