"""track cart lines until checkout payment succeeds.

Revision ID: f7b2d6e1a904
Revises: c3d7e1a9f5b2
Create Date: 2026-08-25 13:20:00.000000

The table only links newly-created application checkout orders to immutable
cart line snapshots.  It never reads or alters legacy public.orders, and it
does not modify existing cart rows during upgrade.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "f7b2d6e1a904"
down_revision: Union[str, Sequence[str], None] = "c3d7e1a9f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist cart checkout snapshots without changing carts or existing orders."""
    op.execute(
        """
        CREATE TABLE public.checkout_cart_lines (
            sales_order_id UUID NOT NULL REFERENCES public.sales_orders(id) ON DELETE RESTRICT,
            catalog_category VARCHAR(16) NOT NULL,
            catalog_product_id VARCHAR(128) NOT NULL,
            quantity INTEGER NOT NULL,
            consumed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (sales_order_id, catalog_category, catalog_product_id),
            CONSTRAINT checkout_cart_lines_category_check
                CHECK (catalog_category IN ('laptops', 'phones', 'components')),
            CONSTRAINT checkout_cart_lines_quantity_check CHECK (quantity BETWEEN 1 AND 5)
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_checkout_cart_lines_unconsumed "
        "ON public.checkout_cart_lines(sales_order_id) WHERE consumed_at IS NULL"
    )


def downgrade() -> None:
    """Remove only unneeded cart snapshots in a disposable environment."""
    op.execute("DROP TABLE public.checkout_cart_lines")
