"""persist autonomous ticket handoff records.

Revision ID: a6b4c8d2e7f1
Revises: f2a6b8c4d903
Create Date: 2026-08-26 00:00:00.000000

This migration belongs to the autonomous ticket-processing branch.  It adds
only delivery records for human escalation; it does not change legacy orders,
payments, refunds, Harness tables, or the ticket status vocabulary.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "a6b4c8d2e7f1"
down_revision: Union[str, Sequence[str], None] = "f2a6b8c4d903"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the idempotent queue used to deliver human escalations."""
    op.execute(
        """
        CREATE TABLE public.ticket_human_escalations (
            id BIGSERIAL PRIMARY KEY,
            ticket_id VARCHAR(20) NOT NULL,
            escalation_generation INTEGER NOT NULL,
            reason_code VARCHAR(64) NOT NULL,
            status VARCHAR(24) NOT NULL DEFAULT 'PENDING',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ,
            delivered_at TIMESTAMPTZ,
            dead_lettered_at TIMESTAMPTZ,
            last_error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT ticket_human_escalations_ticket_fkey
                FOREIGN KEY (ticket_id) REFERENCES public.tickets(ticket_id)
                ON DELETE CASCADE,
            CONSTRAINT ticket_human_escalations_generation_key
                UNIQUE (ticket_id, escalation_generation),
            CONSTRAINT ticket_human_escalations_generation_check
                CHECK (escalation_generation >= 1),
            CONSTRAINT ticket_human_escalations_reason_check
                CHECK (reason_code IN (
                    'EXPLICIT_HUMAN_REQUEST',
                    'COMPLAINT_OR_DISPUTE',
                    'ORDER_OR_PAYMENT_ACTION',
                    'KNOWLEDGE_UNAVAILABLE',
                    'MODEL_ESCALATION',
                    'REPEATED_UNRESOLVED_FOLLOW_UP',
                    'AGENT_UNAVAILABLE'
                )),
            CONSTRAINT ticket_human_escalations_status_check
                CHECK (status IN ('PENDING', 'DELIVERING', 'DELIVERED', 'RETRY_WAIT', 'DLQ')),
            CONSTRAINT ticket_human_escalations_attempts_check
                CHECK (attempts >= 0),
            CONSTRAINT ticket_human_escalations_delivery_fields_check
                CHECK (
                    (status = 'DELIVERED' AND delivered_at IS NOT NULL)
                    OR (status <> 'DELIVERED' AND delivered_at IS NULL)
                ),
            CONSTRAINT ticket_human_escalations_dead_letter_fields_check
                CHECK (
                    (status = 'DLQ' AND dead_lettered_at IS NOT NULL)
                    OR (status <> 'DLQ' AND dead_lettered_at IS NULL)
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_ticket_human_escalations_delivery_queue
        ON public.ticket_human_escalations(status, next_attempt_at, created_at)
        WHERE status IN ('PENDING', 'RETRY_WAIT')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_ticket_human_escalations_ticket_created
        ON public.ticket_human_escalations(ticket_id, created_at DESC)
        """
    )


def downgrade() -> None:
    """Drop escalation delivery records in a disposable environment."""
    op.execute("DROP TABLE public.ticket_human_escalations")
