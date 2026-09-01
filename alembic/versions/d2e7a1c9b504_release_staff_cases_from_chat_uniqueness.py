"""release staff-owned cases from the customer-chat active-case slot.

Revision ID: d2e7a1c9b504
Revises: c6f4a9e2b817
Create Date: 2026-09-01 13:10:00.000000

An AWAITING_STAFF case is linked to a real ticket and remains open for staff
audit.  It must not prevent the same customer session from opening a separate
automatic workflow for a later, independent request.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "d2e7a1c9b504"
down_revision: Union[str, Sequence[str], None] = "c6f4a9e2b817"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DROP INDEX public.support_cases_one_open_case_per_session")
    op.execute(
        """
        CREATE UNIQUE INDEX support_cases_one_active_chat_case_per_session
        ON public.support_cases(session_id)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER')
        """
    )
    op.execute("DROP INDEX public.idx_support_cases_open_queue")
    op.execute(
        """
        CREATE INDEX idx_support_cases_active_chat_queue
        ON public.support_cases(status, updated_at ASC)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX public.idx_support_cases_active_chat_queue")
    op.execute(
        """
        CREATE INDEX idx_support_cases_open_queue
        ON public.support_cases(status, updated_at ASC)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
        """
    )
    op.execute("DROP INDEX public.support_cases_one_active_chat_case_per_session")
    op.execute(
        """
        CREATE UNIQUE INDEX support_cases_one_open_case_per_session
        ON public.support_cases(session_id)
        WHERE status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
        """
    )
