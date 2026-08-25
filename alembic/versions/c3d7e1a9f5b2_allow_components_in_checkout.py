"""allow components in carts and checkout order snapshots.

Revision ID: c3d7e1a9f5b2
Revises: e9c4b7d2a618
Create Date: 2026-08-25 12:25:00.000000

This only broadens category CHECK constraints for application-owned checkout
tables.  It does not touch legacy orders, users, inventory, or payment facts.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "c3d7e1a9f5b2"
down_revision: Union[str, Sequence[str], None] = "e9c4b7d2a618"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow catalog components in the existing cart and checkout tables."""
    op.execute("ALTER TABLE public.cart_items DROP CONSTRAINT cart_items_category_check")
    op.execute(
        "ALTER TABLE public.cart_items ADD CONSTRAINT cart_items_category_check "
        "CHECK (catalog_category IN ('laptops', 'phones', 'components'))"
    )
    op.execute("ALTER TABLE public.sales_order_items DROP CONSTRAINT sales_order_items_category_check")
    op.execute(
        "ALTER TABLE public.sales_order_items ADD CONSTRAINT sales_order_items_category_check "
        "CHECK (catalog_category IN ('laptops', 'phones', 'components'))"
    )


def downgrade() -> None:
    """Restore the prior constraints when no component rows exist.

    PostgreSQL will reject this downgrade if component cart or order rows are
    present.  Use a forward fix in an environment containing real checkout data.
    """
    op.execute("ALTER TABLE public.cart_items DROP CONSTRAINT cart_items_category_check")
    op.execute(
        "ALTER TABLE public.cart_items ADD CONSTRAINT cart_items_category_check "
        "CHECK (catalog_category IN ('laptops', 'phones'))"
    )
    op.execute("ALTER TABLE public.sales_order_items DROP CONSTRAINT sales_order_items_category_check")
    op.execute(
        "ALTER TABLE public.sales_order_items ADD CONSTRAINT sales_order_items_category_check "
        "CHECK (catalog_category IN ('laptops', 'phones'))"
    )
