"""ticket_store.py — 工单数据访问层。

所有 tickets 表的 SQL 集中在此，API 层和 Tool 层不直接碰 SQL。
"""

import logging
from typing import Any

from infra.db_pool import get_connection, put_connection

logger = logging.getLogger(__name__)


async def init_table() -> None:
    """建表（幂等），在 lifespan startup 中调用一次。"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id SERIAL PRIMARY KEY,
                ticket_id VARCHAR(20) UNIQUE NOT NULL,
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
) -> None:
    """插入一条新工单。"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        await conn.execute(
            "INSERT INTO tickets (ticket_id, customer_name, phone, issue, urgency) VALUES (%s, %s, %s, %s, %s)",
            (ticket_id, customer_name, phone, issue, urgency),
        )
        logger.info("工单创建: %s urgency=%s", ticket_id, urgency)
    finally:
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


async def get_ticket(ticket_id: str) -> dict[str, Any] | None:
    """查单条工单详情。"""
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(
            "SELECT ticket_id, customer_name, phone, issue, urgency, status, created_at "
            "FROM tickets WHERE ticket_id = %s",
            (ticket_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "ticket_id": row[0],
            "customer_name": row[1],
            "phone": row[2],
            "issue": row[3],
            "urgency": row[4],
            "status": row[5],
            "created_at": (row[6].isoformat() if hasattr(row[6], "isoformat") else str(row[6])),
        }
    finally:
        await put_connection(conn)


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
