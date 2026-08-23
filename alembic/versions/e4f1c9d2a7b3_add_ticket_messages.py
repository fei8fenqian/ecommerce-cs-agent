"""add ticket message history.

Revision ID: e4f1c9d2a7b3
Revises: a7d1e8f4c902
Create Date: 2026-08-24 00:00:00.000000

This is intentionally a separate branch from the saved V7 Harness migration.
It adds only the ticket conversation table and does not create Harness tables.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "e4f1c9d2a7b3"
down_revision: Union[str, Sequence[str], None] = "a7d1e8f4c902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the append-only message history for existing tickets."""
    op.execute(
        """
        CREATE TABLE public.ticket_messages (
            id BIGSERIAL PRIMARY KEY,
            ticket_id VARCHAR(20) NOT NULL,
            author_role VARCHAR(16) NOT NULL,
            author_user_id INTEGER,
            content TEXT NOT NULL,
            ai_assisted BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ticket_messages_ticket_id_fkey
                FOREIGN KEY (ticket_id) REFERENCES public.tickets(ticket_id)
                ON DELETE CASCADE,
            CONSTRAINT ticket_messages_author_user_id_fkey
                FOREIGN KEY (author_user_id) REFERENCES public.users(id)
                ON DELETE SET NULL,
            CONSTRAINT ticket_messages_author_role_check
                CHECK (author_role IN ('customer', 'agent')),
            CONSTRAINT ticket_messages_author_user_check
                CHECK (author_user_id IS NOT NULL),
            CONSTRAINT ticket_messages_content_check
                CHECK (length(btrim(content)) > 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_ticket_messages_ticket_created
        ON public.ticket_messages(ticket_id, created_at, id)
        """
    )


def downgrade() -> None:
    """Remove ticket message history when explicitly rolling back this branch."""
    op.execute("DROP TABLE public.ticket_messages")
