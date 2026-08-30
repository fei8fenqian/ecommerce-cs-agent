"""持久化客户支持 Case；不读取或修改 legacy 订单事实。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from infra.db_pool import get_connection, put_connection

SupportCaseStatus = Literal[
    "ACTIVE",
    "AWAITING_CUSTOMER",
    "AWAITING_STAFF",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
]
SupportCaseEventType = Literal[
    "CASE_CREATED",
    "REQUESTS_UPDATED",
    "FACTS_READ",
    "AWAITING_CUSTOMER",
    "CUSTOMER_RESPONSE",
    "COMMAND_PROPOSED",
    "COMMAND_COMPLETED",
    "ESCALATED",
    "CASE_COMPLETED",
    "CASE_FAILED",
    "CASE_CANCELLED",
]

OPEN_CASE_STATUSES = {"ACTIVE", "AWAITING_CUSTOMER", "AWAITING_STAFF"}
TERMINAL_CASE_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
_ALL_CASE_STATUSES = OPEN_CASE_STATUSES | TERMINAL_CASE_STATUSES
_ALL_EVENT_TYPES = {
    "CASE_CREATED",
    "REQUESTS_UPDATED",
    "FACTS_READ",
    "AWAITING_CUSTOMER",
    "CUSTOMER_RESPONSE",
    "COMMAND_PROPOSED",
    "COMMAND_COMPLETED",
    "ESCALATED",
    "CASE_COMPLETED",
    "CASE_FAILED",
    "CASE_CANCELLED",
}


@dataclass(frozen=True)
class SupportCase:
    """当前客户请求的可恢复业务状态。"""

    case_id: UUID
    session_id: UUID
    customer_user_id: int
    status: SupportCaseStatus
    request_stack: list[dict[str, Any]]
    selected_subjects: dict[str, Any]
    verified_facts: dict[str, Any]
    pending: dict[str, Any]
    pending_command: dict[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class SupportCaseEvent:
    """Case 的追加式审计事件，不含模型推理文本。"""

    event_id: int
    case_id: UUID
    event_type: SupportCaseEventType
    payload: dict[str, Any]
    created_at: datetime


def _parse_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _case_from_row(row: tuple[Any, ...]) -> SupportCase:
    return SupportCase(
        case_id=_parse_uuid(row[0]),
        session_id=_parse_uuid(row[1]),
        customer_user_id=int(row[2]),
        status=str(row[3]),  # type: ignore[arg-type]
        request_stack=list(row[4] or []),
        selected_subjects=dict(row[5] or {}),
        verified_facts=dict(row[6] or {}),
        pending=dict(row[7] or {}),
        pending_command=dict(row[8] or {}),
        version=int(row[9]),
        created_at=row[10],  # type: ignore[arg-type]
        updated_at=row[11],  # type: ignore[arg-type]
        completed_at=row[12],  # type: ignore[arg-type]
    )


_CASE_COLUMNS = """
    id, session_id, customer_user_id, status, request_stack, selected_subjects,
    verified_facts, pending, pending_command, version, created_at, updated_at, completed_at
"""


async def get_open_case(*, session_id: UUID, customer_user_id: int) -> SupportCase | None:
    """读取当前客户在该聊天会话的唯一活动 Case。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT {_CASE_COLUMNS}
            FROM public.support_cases
            WHERE session_id = %s
              AND customer_user_id = %s
              AND status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (session_id, customer_user_id),
        )
        row = await cursor.fetchone()
        return _case_from_row(row) if row is not None else None
    finally:
        await put_connection(connection)


async def get_latest_case(*, session_id: UUID, customer_user_id: int) -> SupportCase | None:
    """读取该客户会话最近更新的 Case，包括已完成 Case。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT {_CASE_COLUMNS}
            FROM public.support_cases
            WHERE session_id = %s
              AND customer_user_id = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (session_id, customer_user_id),
        )
        row = await cursor.fetchone()
        return _case_from_row(row) if row is not None else None
    finally:
        await put_connection(connection)


async def get_open_case_by_ticket_id(ticket_id: str) -> SupportCase | None:
    """读取等待人工工单对应的活动 Case。"""
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            f"""
            SELECT {_CASE_COLUMNS}
            FROM public.support_cases
            WHERE status = 'AWAITING_STAFF'
              AND pending #>> '{{summary,ticket_id}}' = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (ticket_id,),
        )
        row = await cursor.fetchone()
        return _case_from_row(row) if row is not None else None
    finally:
        await put_connection(connection)


async def create_open_case(
    *,
    session_id: UUID,
    customer_user_id: int,
    request_stack: list[dict[str, Any]],
) -> SupportCase:
    """创建或返回同一会话已有的活动 Case。

    部分唯一索引确保并发的两次聊天请求不会生成两条同时活动的 Case。
    """
    connection = await get_connection()
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                f"""
                SELECT {_CASE_COLUMNS}
                FROM public.support_cases
                WHERE session_id = %s
                  AND customer_user_id = %s
                  AND status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
                FOR UPDATE
                """,
                (session_id, customer_user_id),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                return _case_from_row(existing)

            case_id = uuid4()
            cursor = await connection.execute(
                f"""
                INSERT INTO public.support_cases (
                    id, session_id, customer_user_id, request_stack
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING {_CASE_COLUMNS}
                """,
                (case_id, session_id, customer_user_id, Jsonb(request_stack)),
            )
            created = await cursor.fetchone()
            if created is None:
                # 另一请求可能刚在“查无 Case”后创建成功。ON CONFLICT DO NOTHING
                # 保住当前事务，再读取那个 Case，而不是因唯一索引竞争抛 500。
                cursor = await connection.execute(
                    f"""
                    SELECT {_CASE_COLUMNS}
                    FROM public.support_cases
                    WHERE session_id = %s
                      AND customer_user_id = %s
                      AND status IN ('ACTIVE', 'AWAITING_CUSTOMER', 'AWAITING_STAFF')
                    ORDER BY updated_at DESC, id DESC
                    LIMIT 1
                    """,
                    (session_id, customer_user_id),
                )
                existing_after_conflict = await cursor.fetchone()
                if existing_after_conflict is None:
                    raise RuntimeError("support case creation failed")
                return _case_from_row(existing_after_conflict)
            case = _case_from_row(created)
            await connection.execute(
                """
                INSERT INTO public.support_case_events(case_id, event_type, payload)
                VALUES (%s, 'CASE_CREATED', %s)
                """,
                (case.case_id, Jsonb({"request_count": len(request_stack)})),
            )
            return case
    finally:
        await put_connection(connection)


async def replace_case(
    case: SupportCase,
    *,
    status: SupportCaseStatus,
    request_stack: list[dict[str, Any]],
    selected_subjects: dict[str, Any],
    verified_facts: dict[str, Any],
    pending: dict[str, Any],
    pending_command: dict[str, Any],
    event_type: SupportCaseEventType,
    event_payload: dict[str, Any],
) -> SupportCase | None:
    """以乐观锁完整替换 Case 状态并原子追加一个审计事件。

    返回 ``None`` 表示此 Case 已被另一请求更新，调用方必须重新读取后再决定，不能覆盖
    客户最新选择或员工审批结果。
    """
    if status not in _ALL_CASE_STATUSES:
        raise ValueError(f"invalid support case status: {status}")
    if event_type not in _ALL_EVENT_TYPES:
        raise ValueError(f"invalid support case event type: {event_type}")
    if status in TERMINAL_CASE_STATUSES and event_type not in {
        "CASE_COMPLETED",
        "CASE_FAILED",
        "CASE_CANCELLED",
    }:
        raise ValueError("terminal support case status requires terminal event")

    connection = await get_connection()
    try:
        async with connection.transaction():
            cursor = await connection.execute(
                f"""
                UPDATE public.support_cases
                SET status = %s,
                    request_stack = %s,
                    selected_subjects = %s,
                    verified_facts = %s,
                    pending = %s,
                    pending_command = %s,
                    version = version + 1,
                    updated_at = NOW(),
                    completed_at = CASE
                        WHEN %s IN ('COMPLETED', 'FAILED', 'CANCELLED') THEN NOW()
                        ELSE NULL
                    END
                WHERE id = %s
                  AND customer_user_id = %s
                  AND version = %s
                RETURNING {_CASE_COLUMNS}
                """,
                (
                    status,
                    Jsonb(request_stack),
                    Jsonb(selected_subjects),
                    Jsonb(verified_facts),
                    Jsonb(pending),
                    Jsonb(pending_command),
                    status,
                    case.case_id,
                    case.customer_user_id,
                    case.version,
                ),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            updated = _case_from_row(row)
            await connection.execute(
                """
                INSERT INTO public.support_case_events(case_id, event_type, payload)
                VALUES (%s, %s, %s)
                """,
                (updated.case_id, event_type, Jsonb(event_payload)),
            )
            return updated
    finally:
        await put_connection(connection)


async def list_case_events(
    *,
    case_id: UUID,
    customer_user_id: int,
    limit: int = 100,
) -> list[SupportCaseEvent]:
    """读取客户本人 Case 的审计轨迹，供客服摘要与评测使用。"""
    bounded_limit = max(1, min(limit, 200))
    connection = await get_connection()
    try:
        cursor = await connection.execute(
            """
            SELECT e.id, e.case_id, e.event_type, e.payload, e.created_at
            FROM public.support_case_events AS e
            JOIN public.support_cases AS c ON c.id = e.case_id
            WHERE e.case_id = %s AND c.customer_user_id = %s
            ORDER BY e.created_at ASC, e.id ASC
            LIMIT %s
            """,
            (case_id, customer_user_id, bounded_limit),
        )
        rows = await cursor.fetchall()
        return [
            SupportCaseEvent(
                event_id=int(row[0]),
                case_id=_parse_uuid(row[1]),
                event_type=str(row[2]),  # type: ignore[arg-type]
                payload=dict(row[3] or {}),
                created_at=row[4],  # type: ignore[arg-type]
            )
            for row in rows
        ]
    finally:
        await put_connection(connection)
