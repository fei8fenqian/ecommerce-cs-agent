"""独立测试库中的 ticket_messages Store 验证。"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from psycopg.errors import CheckViolation

from infra.db_pool import close_pool, get_connection, init_pool, put_connection
from store.ticket_message_store import (
    create_agent_ticket_message,
    list_agent_ticket_messages,
    list_customer_ticket_messages,
)
from store.ticket_store import create_ticket


@pytest_asyncio.fixture
async def ticket_message_data() -> AsyncGenerator[dict[str, int | str], None]:
    """创建并在测试后清理仅属于本测试的一组用户和工单。"""
    await init_pool(minconn=1, maxconn=4)
    conn = await get_connection()
    await conn.set_autocommit(True)
    suffix = uuid.uuid4().hex[:10]

    users: dict[str, int] = {}
    for label, role in (("customer", "customer"), ("agent-a", "agent"), ("agent-b", "agent")):
        cursor = await conn.execute(
            """
            INSERT INTO public.users (username, password_hash, role)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (f"ticket-message-{label}-{suffix}", "test-hash", role),
        )
        users[label] = (await cursor.fetchone())[0]

    ticket_id = f"TM-{suffix}"
    await put_connection(conn)

    await create_ticket(
        ticket_id=ticket_id,
        issue="合成测试工单",
        customer_user_id=users["customer"],
        urgency="medium",
    )

    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute(
        "UPDATE public.tickets SET assigned_agent_id = %s WHERE ticket_id = %s",
        (users["agent-a"], ticket_id),
    )
    await put_connection(conn)

    yield {"ticket_id": ticket_id, **users}

    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM public.ticket_messages WHERE ticket_id = %s", (ticket_id,))
    await conn.execute("DELETE FROM public.tickets WHERE ticket_id = %s", (ticket_id,))
    await conn.execute("DELETE FROM public.users WHERE id = ANY(%s)", (list(users.values()),))
    await put_connection(conn)
    await close_pool()


@pytest.mark.asyncio
async def test_real_ticket_message_store_scopes_and_persists(ticket_message_data: dict[str, int | str]) -> None:
    """验证真实表的客服写入、客户读取和其他客服拒绝。"""
    ticket_id = str(ticket_message_data["ticket_id"])
    customer_id = int(ticket_message_data["customer"])
    agent_a_id = int(ticket_message_data["agent-a"])
    agent_b_id = int(ticket_message_data["agent-b"])

    initial_messages = await list_customer_ticket_messages(ticket_id, customer_id)
    assert initial_messages is not None
    assert [(message["author_role"], message["content"]) for message in initial_messages] == [
        ("customer", "合成测试工单")
    ]
    assert await list_agent_ticket_messages(ticket_id, agent_a_id) == initial_messages
    assert await list_agent_ticket_messages(ticket_id, agent_b_id) is None

    created = await create_agent_ticket_message(ticket_id, agent_a_id, "这是合成客服回复", True)
    assert created is not None
    assert created["author_role"] == "agent"
    assert created["ai_assisted"] is True

    customer_messages = await list_customer_ticket_messages(ticket_id, customer_id)
    assert customer_messages is not None
    assert [message["content"] for message in customer_messages] == ["合成测试工单", "这是合成客服回复"]
    assert await create_agent_ticket_message(ticket_id, agent_b_id, "越权消息", False) is None


@pytest.mark.asyncio
async def test_message_insert_failure_rolls_back_new_ticket(ticket_message_data: dict[str, int | str]) -> None:
    """首条消息违反约束时，工单插入必须和它一起回滚。"""
    failed_ticket_id = f"TM-rollback-{uuid.uuid4().hex[:8]}"
    customer_id = int(ticket_message_data["customer"])

    with pytest.raises(CheckViolation):
        await create_ticket(
            ticket_id=failed_ticket_id,
            issue="   ",
            customer_user_id=customer_id,
        )

    conn = await get_connection()
    await conn.set_autocommit(True)
    cursor = await conn.execute("SELECT 1 FROM public.tickets WHERE ticket_id = %s", (failed_ticket_id,))
    assert await cursor.fetchone() is None
    await put_connection(conn)
