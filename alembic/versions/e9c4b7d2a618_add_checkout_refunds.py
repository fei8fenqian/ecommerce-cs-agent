"""add application checkout refund records.

Revision ID: e9c4b7d2a618
Revises: f5c2a6d9b841
Create Date: 2026-08-25 10:15:00.000000

Refunds belong to application-owned checkout facts only.  This migration never
reads, backfills, or changes legacy public.orders.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "e9c4b7d2a618"
down_revision: Union[str, Sequence[str], None] = "f5c2a6d9b841"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create full-refund records and extend only checkout order states."""
    op.execute("ALTER TABLE public.sales_orders DROP CONSTRAINT sales_orders_status_check")
    op.execute(
        """
        ALTER TABLE public.sales_orders
        ADD CONSTRAINT sales_orders_status_check
        CHECK (status IN (
            'PENDING_PAYMENT', 'PAID', 'PAYMENT_FAILED', 'CANCELLED',
            'REFUND_PROCESSING', 'REFUNDED'
        ))
        """
    )
    op.execute(
        """
        CREATE TABLE public.checkout_refunds (
            id UUID PRIMARY KEY,
            sales_order_id UUID NOT NULL UNIQUE REFERENCES public.sales_orders(id) ON DELETE RESTRICT,
            payment_transaction_id UUID NOT NULL UNIQUE REFERENCES public.payment_transactions(id) ON DELETE RESTRICT,
            customer_user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            merchant_refund_no VARCHAR(64) NOT NULL UNIQUE,
            provider_refund_reference VARCHAR(128),
            request_idempotency_key VARCHAR(80) NOT NULL,
            confirmation_idempotency_key VARCHAR(80) UNIQUE,
            status VARCHAR(32) NOT NULL DEFAULT 'PENDING_CONFIRMATION',
            amount_cents BIGINT NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'CNY',
            reason VARCHAR(500) NOT NULL DEFAULT '',
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            processing_at TIMESTAMPTZ,
            succeeded_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            version INTEGER NOT NULL DEFAULT 1,
            CONSTRAINT checkout_refunds_customer_request_key_unique
                UNIQUE (customer_user_id, request_idempotency_key),
            CONSTRAINT checkout_refunds_status_check
                CHECK (status IN ('PENDING_CONFIRMATION', 'PROCESSING', 'SUCCEEDED', 'FAILED')),
            CONSTRAINT checkout_refunds_amount_check CHECK (amount_cents > 0),
            CONSTRAINT checkout_refunds_currency_check CHECK (currency = 'CNY'),
            CONSTRAINT checkout_refunds_version_check CHECK (version >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_checkout_refunds_customer_requested "
        "ON public.checkout_refunds(customer_user_id, requested_at DESC)"
    )
    op.execute("CREATE INDEX idx_checkout_refunds_status_updated ON public.checkout_refunds(status, updated_at DESC)")


def downgrade() -> None:
    """Remove checkout refunds in a disposable environment only.

    Production rollback must be a forward fix: dropping refund records loses
    financial audit history.  PostgreSQL will also reject this downgrade while
    checkout orders still use one of the new refund states.
    """
    op.execute("DROP TABLE public.checkout_refunds")
    op.execute("ALTER TABLE public.sales_orders DROP CONSTRAINT sales_orders_status_check")
    op.execute(
        """
        ALTER TABLE public.sales_orders
        ADD CONSTRAINT sales_orders_status_check
        CHECK (status IN ('PENDING_PAYMENT', 'PAID', 'PAYMENT_FAILED', 'CANCELLED'))
        """
    )
