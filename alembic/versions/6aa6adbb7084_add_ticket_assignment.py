"""add ticket assignment

Revision ID: 6aa6adbb7084
Revises: c1e9f2a7b4d6
Create Date: 2026-08-17 23:53:30.617297

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "6aa6adbb7084"
down_revision: Union[str, Sequence[str], None] = "c1e9f2a7b4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        alter table public.tickets
        add column assigned_agent_id integer null
        """)

    op.execute("""
        alter table public.tickets
        add constraint tickets_assigned_agent_id_fkey
        foreign key (assigned_agent_id)
        references public.users(id)
        """)

    op.execute("""
        create index idx_tickets_assigned_agent_id
        on public.tickets(assigned_agent_id)
        """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        DROP INDEX IF EXISTS public.idx_tickets_assigned_agent_id
    """)

    op.execute("""
        ALTER TABLE public.tickets
        DROP CONSTRAINT tickets_assigned_agent_id_fkey
    """)

    op.execute("""
        ALTER TABLE public.tickets
        DROP COLUMN assigned_agent_id
    """)
