"""add checkout order and payment tables.

Revision ID: b9f3c2d6e714
Revises: f2a6b8c4d903
Create Date: 2026-08-24 09:40:00.000000

This starts a new, application-owned transaction branch.  It deliberately does
not reuse or backfill legacy public.orders: those rows are historical mock data
and cannot become a source of truth for sandbox or production payments.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "b9f3c2d6e714"
down_revision: Union[str, Sequence[str], None] = "f2a6b8c4d903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create application-owned checkout, line-item, and payment facts."""
    op.execute(
        """
        CREATE TABLE public.sales_orders (
            id UUID PRIMARY KEY,
            order_no VARCHAR(32) NOT NULL UNIQUE,
            customer_user_id INTEGER NOT NULL REFERENCES public.users(id),
            status VARCHAR(32) NOT NULL,
            total_amount_cents BIGINT NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'CNY',
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT sales_orders_status_check
                CHECK (status IN ('PENDING_PAYMENT', 'PAID', 'PAYMENT_FAILED', 'CANCELLED')),
            CONSTRAINT sales_orders_amount_check CHECK (total_amount_cents >= 0),
            CONSTRAINT sales_orders_currency_check CHECK (currency = 'CNY'),
            CONSTRAINT sales_orders_version_check CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.sales_order_items (
            id BIGSERIAL PRIMARY KEY,
            sales_order_id UUID NOT NULL REFERENCES public.sales_orders(id) ON DELETE RESTRICT,
            catalog_category VARCHAR(16) NOT NULL,
            catalog_product_id VARCHAR(128) NOT NULL,
            product_name VARCHAR(512) NOT NULL,
            brand VARCHAR(64) NOT NULL DEFAULT '',
            unit_amount_cents BIGINT NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT sales_order_items_category_check
                CHECK (catalog_category IN ('laptops', 'phones')),
            CONSTRAINT sales_order_items_amount_check CHECK (unit_amount_cents >= 0),
            CONSTRAINT sales_order_items_quantity_check CHECK (quantity > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.payment_transactions (
            id UUID PRIMARY KEY,
            sales_order_id UUID NOT NULL UNIQUE REFERENCES public.sales_orders(id) ON DELETE RESTRICT,
            provider VARCHAR(32) NOT NULL,
            merchant_payment_no VARCHAR(64) NOT NULL UNIQUE,
            provider_trade_no VARCHAR(128) UNIQUE,
            provider_callback_id VARCHAR(128) UNIQUE,
            status VARCHAR(32) NOT NULL,
            amount_cents BIGINT NOT NULL,
            currency CHAR(3) NOT NULL DEFAULT 'CNY',
            callback_received_at TIMESTAMPTZ,
            succeeded_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT payment_transactions_provider_check
                CHECK (provider IN ('alipay_sandbox', 'wechat_sandbox')),
            CONSTRAINT payment_transactions_status_check
                CHECK (status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'CLOSED')),
            CONSTRAINT payment_transactions_amount_check CHECK (amount_cents >= 0),
            CONSTRAINT payment_transactions_currency_check CHECK (currency = 'CNY'),
            CONSTRAINT payment_transactions_version_check CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_sales_orders_customer_created
        ON public.sales_orders(customer_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_sales_orders_status_created
        ON public.sales_orders(status, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_sales_order_items_order
        ON public.sales_order_items(sales_order_id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_payment_transactions_status_created
        ON public.payment_transactions(status, created_at DESC)
        """
    )


def downgrade() -> None:
    """Remove only the new checkout branch tables; legacy orders remain intact."""
    op.execute("DROP TABLE public.payment_transactions")
    op.execute("DROP TABLE public.sales_order_items")
    op.execute("DROP TABLE public.sales_orders")
