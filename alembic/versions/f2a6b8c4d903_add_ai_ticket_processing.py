"""add autonomous ticket processing fields.

Revision ID: f2a6b8c4d903
Revises: e4f1c9d2a7b3
Create Date: 2026-08-24 00:00:00.000000

This migration extends only the ticket-message branch. It must be upgraded
explicitly while the deferred Harness branch remains a separate Alembic head.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "f2a6b8c4d903"
down_revision: Union[str, Sequence[str], None] = "e4f1c9d2a7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow AI-authored replies and persist bounded AI work ownership."""
    op.execute("ALTER TABLE public.ticket_messages DROP CONSTRAINT ticket_messages_author_role_check")
    op.execute("ALTER TABLE public.ticket_messages DROP CONSTRAINT ticket_messages_author_user_check")
    op.execute(
        """
        ALTER TABLE public.ticket_messages
        ADD CONSTRAINT ticket_messages_author_role_check
            CHECK (author_role IN ('customer', 'agent', 'ai')),
        ADD CONSTRAINT ticket_messages_author_user_check
            CHECK (
                (author_role = 'ai' AND author_user_id IS NULL)
                OR (author_role IN ('customer', 'agent') AND author_user_id IS NOT NULL)
            )
        """
    )
    op.execute(
        """
        ALTER TABLE public.tickets
        ADD COLUMN ai_claimed_at TIMESTAMPTZ,
        ADD COLUMN ai_processed_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        CREATE INDEX idx_tickets_ai_queue
        ON public.tickets(status, created_at)
        WHERE assigned_agent_id IS NULL
        """
    )


def downgrade() -> None:
    """Remove autonomous-processing fields; historical AI messages block downgrade."""
    op.execute("DROP INDEX public.idx_tickets_ai_queue")
    op.execute("ALTER TABLE public.tickets DROP COLUMN ai_processed_at, DROP COLUMN ai_claimed_at")
    op.execute("ALTER TABLE public.ticket_messages DROP CONSTRAINT ticket_messages_author_role_check")
    op.execute("ALTER TABLE public.ticket_messages DROP CONSTRAINT ticket_messages_author_user_check")
    op.execute(
        """
        ALTER TABLE public.ticket_messages
        ADD CONSTRAINT ticket_messages_author_role_check
            CHECK (author_role IN ('customer', 'agent')),
        ADD CONSTRAINT ticket_messages_author_user_check
            CHECK (author_user_id IS NOT NULL)
        """
    )
