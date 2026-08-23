"""受工单归属范围保护的工单消息数据访问函数。"""

from typing import Any

from infra.db_pool import get_connection, put_connection


def _as_iso(value: Any) -> str:
    """将 PostgreSQL 时间值转换为 API 可返回的 ISO 字符串。"""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _message_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """将消息查询行转换为不含工单客户资料的公开消息字段。"""
    return {
        "message_id": row[0],
        "author_role": row[1],
        "content": row[2],
        "ai_assisted": row[3],
        "created_at": _as_iso(row[4]),
    }


async def _has_ticket_scope(ticket_id: str, scope_sql: str, scope_param: int) -> bool:
    """确认调用者是否有读取指定工单消息的范围。"""
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            f"SELECT 1 FROM public.tickets WHERE ticket_id = %s AND {scope_sql}",
            (ticket_id, scope_param),
        )
        return await cursor.fetchone() is not None
    finally:
        if conn is not None:
            await put_connection(conn)


async def _list_messages_in_scope(ticket_id: str, scope_sql: str, scope_param: int) -> list[dict[str, Any]] | None:
    """返回有权访问工单的消息；无权或不存在时返回 None。"""
    if not await _has_ticket_scope(ticket_id, scope_sql, scope_param):
        return None

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            """
            SELECT message.id, message.author_role, message.content,
                   message.ai_assisted, message.created_at
            FROM public.ticket_messages AS message
            WHERE message.ticket_id = %s
            ORDER BY message.created_at, message.id
            """,
            (ticket_id,),
        )
        return [_message_from_row(row) for row in await cursor.fetchall()]
    finally:
        if conn is not None:
            await put_connection(conn)


async def list_customer_ticket_messages(ticket_id: str, customer_user_id: int) -> list[dict[str, Any]] | None:
    """返回客户自己的工单消息；越权或不存在时不暴露任何消息。"""
    return await _list_messages_in_scope(ticket_id, "customer_user_id = %s", customer_user_id)


async def list_agent_ticket_messages(ticket_id: str, agent_user_id: int) -> list[dict[str, Any]] | None:
    """返回客服自己已认领工单的消息；未认领工单不能读取对话。"""
    return await _list_messages_in_scope(ticket_id, "assigned_agent_id = %s", agent_user_id)


async def create_agent_ticket_message(
    ticket_id: str,
    agent_user_id: int,
    content: str,
    ai_assisted: bool,
) -> dict[str, Any] | None:
    """原子写入当前客服已认领工单的一条回复。

    Returns:
        新消息；若工单不存在、未认领或属于其他客服则返回 None。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            """
            INSERT INTO public.ticket_messages
                (ticket_id, author_role, author_user_id, content, ai_assisted)
            SELECT ticket_id, 'agent', %s, %s, %s
            FROM public.tickets
            WHERE ticket_id = %s AND assigned_agent_id = %s
            RETURNING id, author_role, content, ai_assisted, created_at
            """,
            (agent_user_id, content, ai_assisted, ticket_id, agent_user_id),
        )
        row = await cursor.fetchone()
        return _message_from_row(row) if row is not None else None
    finally:
        if conn is not None:
            await put_connection(conn)
