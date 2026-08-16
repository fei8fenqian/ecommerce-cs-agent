"""add persistent chat sessions

Revision ID: 944b3ab15bb7
Revises: 6fe44ca01f9a
Create Date: 2026-08-16 20:42:13.165955

"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "944b3ab15bb7"
down_revision: Union[str, Sequence[str], None] = "6fe44ca01f9a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("""
        create table if not exists public.sessions (
        id uuid primary key,
        owner_user_id integer not null,
        title VARCHAR(200) NOT NULL DEFAULT '',
        created_at timestamptz not null default now(),
        last_active_at timestamptz not null default now()
        )
        """)

    op.execute("""
        alter table public.sessions
        add constraint sessions_owner_user_id_fkey
        foreign key (owner_user_id)
        references public.users(id)
        """)

    op.execute("""
        create index idx_sessions_owner_last_active
        on public.sessions (owner_user_id, last_active_at desc)
        """)

    op.execute("""
        create table if not exists public.session_messages (
        id bigserial primary key,
        session_id uuid not null,
        sequence_no INTEGER NOT NULL,
        role VARCHAR(20) NOT NULL,
        payload JSONB NOT NULL,
        created_at timestamptz not null default now()
        )
        """)

    op.execute("""
        alter table public.session_messages
        add constraint session_messages_session_id_fkey
        foreign key (session_id)
        references public.sessions(id)
        on delete cascade
        """)

    op.execute("""
        alter table public.session_messages
        add constraint session_messages_sequence_unique
        unique (session_id, sequence_no)
        """)

    op.execute("""
        alter table public.session_messages
        add constraint session_messages_sequence_no_check
        check (sequence_no >= 0)
        """)

    op.execute("""
        alter table public.session_messages
        add constraint session_messages_role_check
        check (role in ('system', 'user', 'assistant', 'tool'))
        """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("""
        drop table if exists public.session_messages
        """)

    op.execute("""
        drop index if exists public.idx_sessions_owner_last_active
        """)

    op.execute("""
        drop table if exists public.sessions
        """)
