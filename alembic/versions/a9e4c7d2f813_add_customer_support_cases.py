"""add persistent customer-support cases.

Revision ID: a9e4c7d2f813
Revises: m8c5d2e9f701
Create Date: 2026-08-27 15:20:00.000000

Customer support cases are application-owned workflow state.  The migration
creates empty tables only; it never reads, backfills, updates, or deletes
legacy orders, checkout facts, sessions, users, tickets, payments, or chat
messages.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "a9e4c7d2f813"
down_revision: Union[str, Sequence[str], None] = "m8c5d2e9f701"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create state and append-only audit records for customer support flows."""
    op.execute(
        """
        CREATE TABLE public.support_cases (
            id UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
            customer_user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE RESTRICT,
            status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
            request_stack JSONB NOT NULL DEFAULT '[]'::jsonb,
            selected_subjects JSONB NOT NULL DEFAULT '{}'::jsonb,
            verified_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
            pending JSONB NOT NULL DEFAULT '{}'::jsonb,
            pending_command JSONB NOT NULL DEFAULT '{}'::jsonb,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT support_cases_status_check
                CHECK (status IN (
                    'ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF',
                    'COMPLETED', 'FAILED', 'CANCELLED'
                )),
            CONSTRAINT support_cases_request_stack_check
                CHECK (jsonb_typeof(request_stack) = 'array'),
            CONSTRAINT support_cases_subjects_check
                CHECK (jsonb_typeof(selected_subjects) = 'object'),
            CONSTRAINT support_cases_verified_facts_check
                CHECK (jsonb_typeof(verified_facts) = 'object'),
            CONSTRAINT support_cases_pending_check
                CHECK (jsonb_typeof(pending) = 'object'),
            CONSTRAINT support_cases_pending_command_check
                CHECK (jsonb_typeof(pending_command) = 'object'),
            CONSTRAINT support_cases_version_check CHECK (version >= 1),
            CONSTRAINT support_cases_completion_check
                CHECK (
                    (status IN ('COMPLETED', 'FAILED', 'CANCELLED') AND completed_at IS NOT NULL)
                    OR (status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF') AND completed_at IS NULL)
                )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX support_cases_one_open_case_per_session
        ON public.support_cases(session_id)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
        """
    )
    op.execute(
        """
        CREATE INDEX idx_support_cases_customer_updated
        ON public.support_cases(customer_user_id, updated_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_support_cases_open_queue
        ON public.support_cases(status, updated_at ASC)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
        """
    )
    op.execute(
        """
        CREATE TABLE public.support_case_events (
            id BIGSERIAL PRIMARY KEY,
            case_id UUID NOT NULL REFERENCES public.support_cases(id) ON DELETE CASCADE,
            event_type VARCHAR(48) NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT support_case_events_type_check
                CHECK (event_type IN (
                    'CASE_CREATED', 'REQUESTS_UPDATED', 'FACTS_READ',
                    'AWAITING_CUSTOMER', 'CUSTOMER_RESPONSE', 'COMMAND_PROPOSED',
                    'COMMAND_COMPLETED', 'ESCALATED', 'CASE_COMPLETED',
                    'CASE_FAILED', 'CASE_CANCELLED'
                )),
            CONSTRAINT support_case_events_payload_check CHECK (jsonb_typeof(payload) = 'object')
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_support_case_events_case_created ON public.support_case_events(case_id, created_at, id)"
    )


def downgrade() -> None:
    """Remove only empty-or-disposable customer-support workflow records."""
    op.execute("DROP TABLE public.support_case_events")
    op.execute("DROP TABLE public.support_cases")
