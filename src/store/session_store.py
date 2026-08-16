import logging
import uuid
from datetime import datetime
from typing import Any, Literal, TypedDict

from psycopg.types.json import Jsonb

from infra.db_pool import get_connection, put_connection

logger = logging.getLogger(__name__)

Role = Literal["system", "user", "assistant", "tool"]


class SessionRecord(TypedDict):
    id: str
    owner_user_id: int
    title: str
    created_at: datetime
    last_active_at: datetime
    message_count: int


class SessionMessage(TypedDict):
    id: int
    session_id: str
    sequence_no: int
    role: Role
    payload: dict[str, Any]
    created_at: datetime


ALLOWED_ROLES = {"system", "user", "assistant", "tool"}


def _parse_session_id(session_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(session_id)
    except (ValueError, AttributeError):
        return None


async def create_session(owner_user_id: int, title: str = "") -> SessionRecord:
    sid = uuid.uuid4()
    conn = None

    try:
        conn = await get_connection()
        async with conn.transaction():
            cur = await conn.execute(
                """
                insert into public.sessions
                (id, owner_user_id, title)
                values (%s, %s, %s)
                returning id, owner_user_id, title, created_at, last_active_at
                """,
                (sid, owner_user_id, (title or "")),
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("session creation failed")
            return SessionRecord(
                id=str(row[0]),
                owner_user_id=row[1],
                title=row[2],
                created_at=row[3],
                last_active_at=row[4],
                message_count=0,
            )
    finally:
        if conn is not None:
            await put_connection(conn)


async def get_session(session_id: str, owner_user_id: int) -> SessionRecord | None:
    session_uuid = _parse_session_id(session_id)
    if session_uuid is None:
        return None

    conn = None
    try:
        conn = await get_connection()
        cur = await conn.execute(
            """
            select s.id, s.owner_user_id, s.title, s.created_at, s.last_active_at,
            count(m.id)::int as message_count
            from public.sessions s left join public.session_messages m
            on s.id=m.session_id
            where s.owner_user_id = %s and s.id = %s
            group by s.id, s.owner_user_id, s.title, s.created_at, s.last_active_at
            """,
            (owner_user_id, session_uuid),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return SessionRecord(
            id=str(row[0]),
            owner_user_id=row[1],
            title=row[2],
            created_at=row[3],
            last_active_at=row[4],
            message_count=row[5],
        )
    finally:
        if conn is not None:
            await put_connection(conn)


async def list_sessions(owner_user_id: int) -> list[SessionRecord]:
    conn = None
    session_records: list[SessionRecord] = []
    try:
        conn = await get_connection()
        async for row in await conn.execute(
            """
            select s.id, s.owner_user_id, s.title, s.created_at, s.last_active_at,
            count(m.id)::int as message_count
            from public.sessions s left join public.session_messages m
            on s.id = m.session_id
            where s.owner_user_id = %s
            group by s.id, s.owner_user_id, s.title, s.created_at, s.last_active_at
            order by s.last_active_at desc
            """,
            (owner_user_id,),
        ):
            if not row:
                continue
            session_records.append(
                SessionRecord(
                    id=str(row[0]),
                    owner_user_id=row[1],
                    title=row[2],
                    created_at=row[3],
                    last_active_at=row[4],
                    message_count=row[5],
                )
            )
        return session_records
    finally:
        if conn is not None:
            await put_connection(conn)


async def delete_session(session_id: str, owner_user_id: int) -> bool:
    """删除当前用户的会话及其消息。"""
    session_uuid = _parse_session_id(session_id)
    if session_uuid is None:
        return False

    conn = None
    try:
        conn = await get_connection()
        async with conn.transaction():
            cur = await conn.execute(
                """
                DELETE FROM public.sessions
                WHERE id = %s AND owner_user_id = %s
                RETURNING id
                """,
                (session_uuid, owner_user_id),
            )
            return await cur.fetchone() is not None
    finally:
        if conn is not None:
            await put_connection(conn)


async def append_messages(
    session_id: str,
    owner_user_id: int,
    messages: list[dict[str, Any]],
) -> None:
    """向当前用户的会话追加消息，并自动分配 sequence_no。"""
    if not messages:
        return

    session_uuid = _parse_session_id(session_id)
    if session_uuid is None:
        raise ValueError("invalid session id")

    for message in messages:
        if message.get("role") not in ALLOWED_ROLES:
            raise ValueError(f"invalid message role: {message.get('role')!r}")

    conn = None
    try:
        conn = await get_connection()
        async with conn.transaction():
            # 锁住会话行，避免并发追加时拿到相同的最大序号。
            cur = await conn.execute(
                """
                SELECT id
                FROM public.sessions
                WHERE id = %s AND owner_user_id = %s
                FOR UPDATE
                """,
                (session_uuid, owner_user_id),
            )
            if await cur.fetchone() is None:
                raise ValueError("session not found")

            # 取当前最大的 sequence_no；如果没有任何消息，就使用 -1
            cur = await conn.execute(
                """
                SELECT COALESCE(MAX(sequence_no), -1)
                FROM public.session_messages
                WHERE session_id = %s
                """,
                (session_uuid,),
            )
            next_sequence_no = (await cur.fetchone())[0] + 1

            async with conn.cursor() as message_cursor:
                await message_cursor.executemany(
                    """
                    INSERT INTO public.session_messages
                        (session_id, sequence_no, role, payload)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            session_uuid,
                            next_sequence_no + index,
                            message["role"],
                            Jsonb(message),
                        )
                        for index, message in enumerate(messages)
                    ],
                )

            await conn.execute(
                """
                UPDATE public.sessions
                SET last_active_at = now()
                WHERE id = %s
                """,
                (session_uuid,),
            )
    finally:
        if conn is not None:
            await put_connection(conn)


async def get_messages(
    session_id: str,
    owner_user_id: int,
    limit: int = 50,
) -> list[SessionMessage] | None:
    """读取当前用户会话的最近消息，并按 sequence_no 正序返回。"""
    session_uuid = _parse_session_id(session_id)
    if session_uuid is None:
        return None
    limit = min(max(limit, 1), 200)

    conn = None
    try:
        conn = await get_connection()
        cur = await conn.execute(
            """
            select 1
            from public.sessions
            where id = %s and owner_user_id = %s
            """,
            (session_uuid, owner_user_id),
        )
        if await cur.fetchone() is None:
            return None

        cur = await conn.execute(
            """
            SELECT id, session_id, sequence_no, role, payload, created_at
            FROM (
                SELECT
                    m.id,
                    m.session_id,
                    m.sequence_no,
                    m.role,
                    m.payload,
                    m.created_at
                FROM public.session_messages AS m
                JOIN public.sessions AS s ON s.id = m.session_id
                WHERE m.session_id = %s
                  AND s.owner_user_id = %s
                ORDER BY m.sequence_no DESC
                LIMIT %s
            ) AS recent_messages
            ORDER BY sequence_no ASC
            """,
            (session_uuid, owner_user_id, limit),
        )
        rows = await cur.fetchall()

        return [
            SessionMessage(
                id=row[0],
                session_id=str(row[1]),
                sequence_no=row[2],
                role=row[3],
                payload=row[4],
                created_at=row[5],
            )
            for row in rows
        ]
    finally:
        if conn is not None:
            await put_connection(conn)
