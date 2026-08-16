"""add resource ownership foreign keys

Revision ID: 6fe44ca01f9a
Revises: 046df10e2506
Create Date: 2026-08-16 19:34:21.811655

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "6fe44ca01f9a"
down_revision: Union[str, Sequence[str], None] = "046df10e2506"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        alter table public.orders
        add column customer_user_id integer null
        """)

    op.execute("""
        create index idx_orders_customer_user_id
        on public.orders(customer_user_id)
        """)

    op.execute("""
        alter table public.orders
        add constraint orders_customer_user_id_fkey
        foreign key (customer_user_id)
        references public.users(id)
        """)

    op.execute("""
        alter table public.tickets
        add column customer_user_id integer null
        """)

    op.execute("""
        create index idx_tickets_customer_user_id
        on public.tickets(customer_user_id)
        """)

    op.execute("""
        alter table public.tickets
        add constraint tickets_customer_user_id_fkey
        foreign key (customer_user_id)
        references public.users(id)
        """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        alter table public.tickets
        drop constraint tickets_customer_user_id_fkey
        """)

    op.execute("""
        drop index if exists public.idx_tickets_customer_user_id
        """)

    op.execute("""
        alter table public.tickets
        drop column customer_user_id
        """)

    op.execute("""
        alter table public.orders
        drop constraint orders_customer_user_id_fkey
        """)

    op.execute("""
        drop index if exists public.idx_orders_customer_user_id
        """)

    op.execute("""
        alter table public.orders
        drop column customer_user_id
        """)
