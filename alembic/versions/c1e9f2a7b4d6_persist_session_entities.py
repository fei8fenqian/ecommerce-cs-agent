"""persist session entities

Revision ID: c1e9f2a7b4d6
Revises: 944b3ab15bb7
Create Date: 2026-08-17 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "c1e9f2a7b4d6"
down_revision: Union[str, Sequence[str], None] = "944b3ab15bb7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store the latest entities used for pronoun resolution."""
    op.execute("""
        alter table public.sessions
        add column last_entities jsonb not null default '{}'::jsonb
        """)


def downgrade() -> None:
    """Remove persisted conversation entities."""
    op.execute("""
        alter table public.sessions
        drop column last_entities
        """)
