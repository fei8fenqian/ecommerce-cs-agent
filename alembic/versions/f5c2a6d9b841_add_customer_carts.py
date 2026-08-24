"""add customer shopping carts.

Revision ID: f5c2a6d9b841
Revises: c8e4f1a2b735
Create Date: 2026-08-24 16:30:00.000000

Cart data is application-owned and deliberately separate from legacy mock
orders. Upgrade only creates empty cart tables; it never backfills users or
orders.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "f5c2a6d9b841"
down_revision: Union[str, Sequence[str], None] = "c8e4f1a2b735"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create one cart per customer and its mutable item rows."""
    op.execute(
        """
        CREATE TABLE public.carts (
            id UUID PRIMARY KEY,
            customer_user_id INTEGER NOT NULL UNIQUE REFERENCES public.users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.cart_items (
            id BIGSERIAL PRIMARY KEY,
            cart_id UUID NOT NULL REFERENCES public.carts(id) ON DELETE CASCADE,
            catalog_category VARCHAR(16) NOT NULL,
            catalog_product_id VARCHAR(128) NOT NULL,
            quantity INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT cart_items_category_check CHECK (catalog_category IN ('laptops', 'phones')),
            CONSTRAINT cart_items_quantity_check CHECK (quantity BETWEEN 1 AND 5),
            CONSTRAINT cart_items_cart_product_unique UNIQUE (cart_id, catalog_category, catalog_product_id)
        )
        """
    )
    op.execute("CREATE INDEX idx_cart_items_cart_created ON public.cart_items(cart_id, created_at, id)")


def downgrade() -> None:
    """Remove only application-owned cart tables."""
    op.execute("DROP TABLE public.cart_items")
    op.execute("DROP TABLE public.carts")
