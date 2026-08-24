"""ticket_store.py — 工单数据访问层。

所有 tickets 表的 SQL 集中在此，API 层和 Tool 层不直接碰 SQL。
"""

import logging
from typing import Any

from infra.db_pool import get_connection, put_connection

logger = logging.getLogger(__name__)


async def init_ticket_table() -> None:
    """建表（幂等），在 lifespan startup 中调用一次。"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                ticket_id VARCHAR(20) UNIQUE NOT NULL,
                customer_user_id INTEGER,
                assigned_agent_id INTEGER,
                customer_name VARCHAR(50) DEFAULT '',
                phone VARCHAR(20) DEFAULT '',
                issue TEXT NOT NULL,
                urgency VARCHAR(10) DEFAULT 'medium',
                status VARCHAR(10) DEFAULT '待处理',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

    finally:
        await put_connection(conn)


async def create_ticket(
    ticket_id: str,
    issue: str,
    customer_name: str = "",
    phone: str = "",
    urgency: str = "medium",
    customer_user_id: int | None = None,
    status: str = "待处理",
) -> None:
    """插入一条新工单。

    ``AI待处理`` 仅由服务端 Agent 工具使用；普通 API 与兼容调用保持人工队列。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(False)
        await conn.execute(
            """
            INSERT INTO tickets
                (ticket_id, customer_user_id, customer_name, phone, issue, urgency, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (ticket_id, customer_user_id, customer_name, phone, issue, urgency, status),
        )
        # 新工单的 issue 是客户对话的首句。与工单一起提交，避免出现孤立工单。
        if customer_user_id is not None:
            await conn.execute(
                """
                INSERT INTO public.ticket_messages
                    (ticket_id, author_role, author_user_id, content, ai_assisted)
                VALUES (%s, 'customer', %s, %s, false)
                """,
                (ticket_id, customer_user_id, issue),
            )
        await conn.commit()
        logger.info("工单创建成功: urgency=%s", urgency)
    except Exception:
        if conn is not None:
            await conn.rollback()
        raise
    finally:
        if conn is not None:
            await put_connection(conn)


def _as_iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def get_ticket(ticket_id: str) -> dict[str, Any] | None:
    """查询完整工单，供旧的 store 测试和内部代码使用。

    API 不应直接调用此函数，API 必须使用带用户范围的查询函数。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            """
            SELECT ticket_id, customer_user_id, assigned_agent_id,
                   customer_name, phone, issue, urgency, status, created_at
            FROM public.tickets
            WHERE ticket_id = %s
            """,
            (ticket_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "ticket_id": row[0],
            "customer_user_id": row[1],
            "assigned_agent_id": row[2],
            "customer_name": row[3],
            "phone": row[4],
            "issue": row[5],
            "urgency": row[6],
            "status": row[7],
            "created_at": _as_iso(row[8]),
        }
    finally:
        if conn is not None:
            await put_connection(conn)


async def list_tickets(status: str | None = None) -> list[dict[str, Any]]:
    """查工单列表，可按 status 过滤，按 created_at 倒序。"""

    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        if status:
            cur = await conn.execute(
                "SELECT ticket_id, customer_name, urgency, status, created_at "
                "FROM tickets WHERE status = %s ORDER BY created_at DESC",
                (status,),
            )
        else:
            cur = await conn.execute(
                "SELECT ticket_id, customer_name, urgency, status, created_at FROM tickets ORDER BY created_at DESC"
            )
        rows = await cur.fetchall()
        return [
            {
                "ticket_id": row[0],
                "customer_name": row[1],
                "urgency": row[2],
                "status": row[3],
                "created_at": (row[4].isoformat() if hasattr(row[4], "isoformat") else str(row[4])),
            }
            for row in rows
        ]
    finally:
        await put_connection(conn)


async def list_customer_tickets(
    user_id: int,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """只列出当前客户自己的工单。"""
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        query = """
            SELECT ticket_id, customer_name, urgency, status, created_at
            FROM public.tickets
            WHERE customer_user_id = %s
        """
        params: tuple[Any, ...] = (user_id,)
        if status:
            query += " AND status = %s"
            params += (status,)
        query += " ORDER BY created_at DESC"
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        return [
            {
                "ticket_id": row[0],
                "customer_name": row[1],
                "urgency": row[2],
                "status": row[3],
                "created_at": _as_iso(row[4]),
            }
            for row in rows
        ]
    finally:
        if conn is not None:
            await put_connection(conn)


async def list_agent_tickets(
    agent_id: int,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """列出未认领工单和当前客服自己认领的工单。

    未认领工单只返回脱敏摘要；自己认领的工单返回完整字段。
    其他客服已认领的工单不会出现在结果中。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        query = """
            SELECT ticket_id, assigned_agent_id, customer_name, phone, issue,
                   urgency, status, created_at
            FROM public.tickets
            WHERE (assigned_agent_id IS NULL OR assigned_agent_id = %s)
        """
        params: tuple[Any, ...] = (agent_id,)
        if status:
            query += " AND status = %s"
            params += (status,)
        query += " ORDER BY created_at DESC"
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item: dict[str, Any] = {
                "ticket_id": row[0],
                "assigned_agent_id": row[1],
                "urgency": row[5],
                "status": row[6],
                "created_at": _as_iso(row[7]),
            }
            if row[1] == agent_id:
                item.update(
                    {
                        "customer_name": row[2],
                        "phone": row[3],
                        "issue": row[4],
                    }
                )
            result.append(item)
        return result
    finally:
        if conn is not None:
            await put_connection(conn)


async def get_customer_ticket(ticket_id: str, user_id: int) -> dict[str, Any] | None:
    """客户查单条工单详情。"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            """
            SELECT ticket_id, customer_user_id, customer_name, phone, issue,
                   urgency, status, created_at
            FROM public.tickets
            WHERE ticket_id = %s AND customer_user_id = %s
            """,
            (ticket_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "ticket_id": row[0],
            "customer_user_id": row[1],
            "customer_name": row[2],
            "phone": row[3],
            "issue": row[4],
            "urgency": row[5],
            "status": row[6],
            "created_at": _as_iso(row[7]),
        }
    finally:
        await put_connection(conn)


async def get_agent_ticket(ticket_id: str, user_id: int) -> dict[str, Any] | None:
    """客服查询自己的工单或未认领工单。

    未认领工单只返回脱敏摘要；其他客服已认领的工单返回 None。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            """
            SELECT ticket_id, assigned_agent_id, customer_name, phone, issue,
                   urgency, status, created_at
            FROM public.tickets
            WHERE ticket_id = %s
              AND (assigned_agent_id IS NULL OR assigned_agent_id = %s)
            """,
            (ticket_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        result: dict[str, Any] = {
            "ticket_id": row[0],
            "assigned_agent_id": row[1],
            "urgency": row[5],
            "status": row[6],
            "created_at": _as_iso(row[7]),
        }
        if row[1] == user_id:
            result.update(
                {
                    "customer_name": row[2],
                    "phone": row[3],
                    "issue": row[4],
                }
            )
        return result
    finally:
        if conn is not None:
            await put_connection(conn)


async def _update_ticket_in_scope(
    ticket_id: str,
    scope_sql: str,
    scope_params: tuple[Any, ...],
    kwargs: Any,
) -> bool:
    updates = {key: value for key, value in kwargs.items() if value is not None}
    allowed = {"status", "urgency"}
    updates = {key: value for key, value in updates.items() if key in allowed}
    if not updates:
        conn = None
        try:
            conn = await get_connection()
            await conn.set_autocommit(True)
            cur = await conn.execute(
                f"SELECT 1 FROM public.tickets WHERE ticket_id = %s AND {scope_sql}",
                (ticket_id,) + scope_params,
            )
            return await cur.fetchone() is not None
        finally:
            if conn is not None:
                await put_connection(conn)

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        set_clause = ", ".join(f"{key} = %s" for key in updates)
        values = tuple(updates.values()) + (ticket_id,) + scope_params
        cur = await conn.execute(
            f"UPDATE public.tickets SET {set_clause} WHERE ticket_id = %s AND {scope_sql}",
            values,
        )
        return cur.rowcount > 0
    finally:
        if conn is not None:
            await put_connection(conn)


async def update_customer_ticket(ticket_id: str, user_id: int, **kwargs: Any) -> bool:
    """只更新当前客户自己的工单。"""
    return await _update_ticket_in_scope(
        ticket_id,
        "customer_user_id = %s",
        (user_id,),
        kwargs,
    )


async def update_agent_ticket(ticket_id: str, agent_id: int, **kwargs: Any) -> bool:
    """只更新当前客服自己认领的工单。"""
    return await _update_ticket_in_scope(
        ticket_id,
        "assigned_agent_id = %s",
        (agent_id,),
        kwargs,
    )


async def update_ticket(ticket_id: str, **kwargs: Any) -> bool:
    """更新工单字段（status / urgency），只更新非 None 的字段。

    Returns:
        True 表示更新成功，False 表示工单不存在。
    """
    # 过滤掉 None 值
    updates = {k: v for k, v in kwargs.items() if v is not None}
    if not updates:
        return True  # 没啥要改的

    # 白名单防注入：只允许更新这两个字段
    allowed = {"status", "urgency"}
    updates = {k: v for k, v in updates.items() if k in allowed}
    if not updates:
        return True

    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [ticket_id]
        cur = await conn.execute(
            f"UPDATE tickets SET {set_clause} WHERE ticket_id = %s",
            values,
        )
        affected = cur.rowcount
        return affected > 0
    finally:
        await put_connection(conn)


async def claim_ticket(ticket_id: str, agent_id: int) -> dict | None:
    """客服认领待处理或 AI 已转人工的工单。"""
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            """
            UPDATE public.tickets SET assigned_agent_id = %s
            WHERE ticket_id = %s
              AND assigned_agent_id IS NULL
              AND status IN ('待处理', '待人工处理')
            returning ticket_id, assigned_agent_id, status, created_at
            """,
            (agent_id, ticket_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return {
            "ticket_id": row[0],
            "assigned_agent_id": row[1],
            "status": row[2],
            "created_at": _as_iso(row[3]),
        }
    finally:
        if conn is not None:
            await put_connection(conn)


async def claim_next_ticket_for_ai(claim_timeout_seconds: int) -> dict[str, Any] | None:
    """原子领取一张可由 AI 处理的客户工单。

    Args:
        claim_timeout_seconds: AI 进程崩溃后，允许重新领取旧租约的等待时间。

    Returns:
        脱离数据库连接后的工单事实；当前没有可处理任务时返回 None。

    Raises:
        Exception: 数据库不可用时由调用方决定本轮 worker 如何退避。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            """
            WITH candidate AS (
                SELECT ticket_id
                FROM public.tickets
                WHERE assigned_agent_id IS NULL
                  AND customer_user_id IS NOT NULL
                  AND (
                      status = 'AI待处理'
                      OR (
                          status = 'AI处理中'
                          AND ai_claimed_at < NOW() - (%s * INTERVAL '1 second')
                      )
                  )
                ORDER BY created_at, ticket_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE public.tickets AS ticket
            SET status = 'AI处理中', ai_claimed_at = NOW()
            FROM candidate
            WHERE ticket.ticket_id = candidate.ticket_id
            RETURNING ticket.ticket_id,
                      ticket.customer_user_id,
                      (
                          SELECT message.content
                          FROM public.ticket_messages AS message
                          WHERE message.ticket_id = ticket.ticket_id
                            AND message.author_role = 'customer'
                          ORDER BY message.created_at DESC, message.id DESC
                          LIMIT 1
                      ),
                      ticket.urgency
            """,
            (claim_timeout_seconds,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "ticket_id": row[0],
            "customer_user_id": row[1],
            "issue": row[2],
            "urgency": row[3],
        }
    finally:
        if conn is not None:
            await put_connection(conn)


async def complete_ai_ticket(ticket_id: str, content: str) -> bool:
    """保存 AI 回复并将其领取中的工单标记为已处理。

    Args:
        ticket_id: 已由 AI 原子领取的工单编号。
        content: 已基于知识资料生成、可展示给客户的回复正文。

    Returns:
        True 表示消息和工单状态在同一事务写入；False 表示租约已不属于 AI。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(False)
        cursor = await conn.execute(
            """
            UPDATE public.tickets
            SET status = '已处理', ai_processed_at = NOW()
            WHERE ticket_id = %s AND status = 'AI处理中'
            RETURNING ticket_id
            """,
            (ticket_id,),
        )
        if await cursor.fetchone() is None:
            await conn.rollback()
            return False
        await conn.execute(
            """
            INSERT INTO public.ticket_messages
                (ticket_id, author_role, author_user_id, content, ai_assisted)
            VALUES (%s, 'ai', NULL, %s, true)
            """,
            (ticket_id, content),
        )
        await conn.commit()
        return True
    except Exception:
        if conn is not None:
            await conn.rollback()
        raise
    finally:
        if conn is not None:
            await put_connection(conn)


async def send_ticket_to_human_queue(ticket_id: str) -> bool:
    """将 AI 无法可靠处理的已领取工单交回人工队列。"""
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cursor = await conn.execute(
            """
            UPDATE public.tickets
            SET status = '待人工处理', ai_processed_at = NOW()
            WHERE ticket_id = %s AND status = 'AI处理中'
            RETURNING ticket_id
            """,
            (ticket_id,),
        )
        return await cursor.fetchone() is not None
    finally:
        if conn is not None:
            await put_connection(conn)


async def escalate_ai_ticket(ticket_id: str, content: str) -> bool:
    """写入 Agent 的升级说明，并将已领取工单交回人工队列。

    Args:
        ticket_id: 当前由 AI 租约领取的工单编号。
        content: 面向客户的简短说明；只描述已知事实和下一步，不包含内部异常。

    Returns:
        True 表示消息和状态已在同一事务内写入；False 表示 AI 已不再拥有该工单。
    """
    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(False)
        cursor = await conn.execute(
            """
            UPDATE public.tickets
            SET status = '待人工处理', ai_processed_at = NOW()
            WHERE ticket_id = %s AND status = 'AI处理中'
            RETURNING ticket_id
            """,
            (ticket_id,),
        )
        if await cursor.fetchone() is None:
            await conn.rollback()
            return False
        await conn.execute(
            """
            INSERT INTO public.ticket_messages
                (ticket_id, author_role, author_user_id, content, ai_assisted)
            VALUES (%s, 'ai', NULL, %s, true)
            """,
            (ticket_id, content),
        )
        await conn.commit()
        return True
    except Exception:
        if conn is not None:
            await conn.rollback()
        raise
    finally:
        if conn is not None:
            await put_connection(conn)
