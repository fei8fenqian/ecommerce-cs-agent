"""add checkout fulfillment records.

Revision ID: c8e4f1a2b735
Revises: b9f3c2d6e714
Create Date: 2026-08-24 12:30:00.000000

The fulfillment branch belongs only to application-owned sales_orders. It does
not read, backfill, or modify legacy public.orders.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "c8e4f1a2b735"
down_revision: Union[str, Sequence[str], None] = "b9f3c2d6e714"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create one fulfillment lifecycle record for each paid checkout order."""
    op.execute(
        """
        CREATE TABLE public.fulfillments (
            id UUID PRIMARY KEY,
            sales_order_id UUID NOT NULL UNIQUE REFERENCES public.sales_orders(id) ON DELETE RESTRICT,
            status VARCHAR(32) NOT NULL DEFAULT 'PENDING_FULFILLMENT',
            carrier VARCHAR(64),
            tracking_number VARCHAR(128),
            shipped_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT fulfillments_status_check
                CHECK (status IN ('PENDING_FULFILLMENT', 'SHIPPED', 'DELIVERED', 'EXCEPTION')),
            CONSTRAINT fulfillments_version_check CHECK (version >= 1),
            CONSTRAINT fulfillments_tracking_check
                CHECK (
                    (status = 'PENDING_FULFILLMENT' AND carrier IS NULL AND tracking_number IS NULL)
                    OR status IN ('SHIPPED', 'DELIVERED', 'EXCEPTION')
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_fulfillments_status_created
        ON public.fulfillments(status, created_at DESC)
        """
    )


def downgrade() -> None:
    """Remove the application-owned fulfillment table only."""
    op.execute("DROP TABLE public.fulfillments")
