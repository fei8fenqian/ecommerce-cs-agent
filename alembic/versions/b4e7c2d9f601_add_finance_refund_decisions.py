"""add deterministic finance decisions for checkout refunds.

Revision ID: b4e7c2d9f601
Revises: f7b2d6e1a904
Create Date: 2026-08-26 10:00:00.000000

This migration only extends the application-owned checkout refund branch.  It
does not read, backfill, or modify legacy public.orders.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "b4e7c2d9f601"
down_revision: Union[str, Sequence[str], None] = "f7b2d6e1a904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add finance decision state and idempotent decision metadata."""
    op.execute("ALTER TABLE public.checkout_refunds DROP CONSTRAINT checkout_refunds_status_check")
    op.execute(
        """
        ALTER TABLE public.checkout_refunds
        ADD CONSTRAINT checkout_refunds_status_check
        CHECK (status IN (
            'PENDING_CONFIRMATION', 'PENDING_FINANCE_APPROVAL',
            'PROCESSING', 'SUCCEEDED', 'FAILED', 'REJECTED'
        ))
        """
    )
    op.execute(
        """
        ALTER TABLE public.checkout_refunds
        ADD COLUMN finance_decision_idempotency_key VARCHAR(80) UNIQUE,
        ADD COLUMN finance_decided_by INTEGER REFERENCES public.users(id) ON DELETE RESTRICT,
        ADD COLUMN finance_decided_at TIMESTAMPTZ,
        ADD COLUMN finance_decision_note VARCHAR(500) NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        CREATE INDEX idx_checkout_refunds_finance_queue
        ON public.checkout_refunds(status, requested_at DESC)
        WHERE status IN ('PENDING_FINANCE_APPROVAL', 'PROCESSING')
        """
    )


def downgrade() -> None:
    """Remove finance decision metadata in a disposable environment only."""
    op.execute("DROP INDEX public.idx_checkout_refunds_finance_queue")
    op.execute(
        """
        ALTER TABLE public.checkout_refunds
        DROP COLUMN finance_decision_idempotency_key,
        DROP COLUMN finance_decided_by,
        DROP COLUMN finance_decided_at,
        DROP COLUMN finance_decision_note
        """
    )
    op.execute("ALTER TABLE public.checkout_refunds DROP CONSTRAINT checkout_refunds_status_check")
    op.execute(
        """
        ALTER TABLE public.checkout_refunds
        ADD CONSTRAINT checkout_refunds_status_check
        CHECK (status IN ('PENDING_CONFIRMATION', 'PROCESSING', 'SUCCEEDED', 'FAILED'))
        """
    )
