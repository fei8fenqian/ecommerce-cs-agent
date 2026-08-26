"""merge the production checkout and ticket-escalation migration branches.

This is a topology-only revision.  It does not create, alter, backfill, or
delete any table.  The V7 Harness revision remains an explicitly labelled
experimental branch and is not part of the production target.
"""

from typing import Sequence, Union

revision: str = "m8c5d2e9f701"
down_revision: Union[str, Sequence[str], None] = (
    "a6b4c8d2e7f1",
    "b4e7c2d9f601",
)
branch_labels: Union[str, Sequence[str], None] = "production"
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge two already-applied production branches without changing data."""


def downgrade() -> None:
    """Allow a disposable test database to return to either parent head."""
