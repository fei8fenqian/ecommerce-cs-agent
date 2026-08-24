"""独立测试库中 AI 工单领取、结案和人工接管的真实 PostgreSQL 验证。"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio

from infra.db_pool import close_pool, get_connection, init_pool, put_connection
from store.ticket_message_store import create_customer_ticket_message, list_customer_ticket_messages
from store.ticket_store import (
    claim_next_ticket_for_ai,
    claim_ticket,
    complete_ai_ticket,
    create_ticket,
    send_ticket_to_human_queue,
)


@pytest_asyncio.fixture
async def ai_ticket_data() -> AsyncGenerator[dict[str, int | str], None]:
    """创建并清理只属于本测试的一组客户、客服和工单。"""
    await init_pool(minconn=1, maxconn=4)
    conn = await get_connection()
    await conn.set_autocommit(True)
    suffix = uuid.uuid4().hex[:10]
    users: dict[str, int] = {}
    for label, role in (("customer", "customer"), ("agent", "agent")):
        cursor = await conn.execute(
            """
            INSERT INTO public.users (username, password_hash, role)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (f"ticket-ai-{label}-{suffix}", "test-hash", role),
        )
        users[label] = (await cursor.fetchone())[0]
    await put_connection(conn)

    ticket_id = f"TA-{suffix}"
    await create_ticket(
        ticket_id=ticket_id,
        issue="合成工单：设备无法开机",
        customer_user_id=users["customer"],
        status="AI待处理",
    )
    yield {"ticket_id": ticket_id, **users}

    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM public.ticket_messages WHERE ticket_id = %s", (ticket_id,))
    await conn.execute("DELETE FROM public.tickets WHERE ticket_id = %s", (ticket_id,))
    await conn.execute("DELETE FROM public.users WHERE id = ANY(%s)", (list(users.values()),))
    await put_connection(conn)
    await close_pool()


@pytest.mark.asyncio
async def test_ai_claims_answers_and_customer_can_read_reply(ai_ticket_data: dict[str, int | str]) -> None:
    """AI 领取期间人工不能抢占；完成后客户能看到 AI 消息。"""
    ticket_id = str(ai_ticket_data["ticket_id"])
    customer_id = int(ai_ticket_data["customer"])
    human_agent_id = int(ai_ticket_data["agent"])

    claimed = await claim_next_ticket_for_ai(claim_timeout_seconds=120)
    assert claimed is not None
    assert claimed["ticket_id"] == ticket_id
    assert await claim_ticket(ticket_id, human_agent_id) is None

    assert await complete_ai_ticket(ticket_id, "请先检查电源连接后再试一次。") is True
    messages = await list_customer_ticket_messages(ticket_id, customer_id)
    assert messages is not None
    assert [(message["author_role"], message["content"]) for message in messages] == [
        ("customer", "合成工单：设备无法开机"),
        ("ai", "请先检查电源连接后再试一次。"),
    ]


@pytest.mark.asyncio
async def test_ai_can_return_unresolved_ticket_to_human_queue(ai_ticket_data: dict[str, int | str]) -> None:
    """知识不足后，客户补充信息会让未认领工单重新进入 AI 队列。"""
    ticket_id = str(ai_ticket_data["ticket_id"])
    customer_id = int(ai_ticket_data["customer"])

    claimed = await claim_next_ticket_for_ai(claim_timeout_seconds=120)
    assert claimed is not None
    assert await send_ticket_to_human_queue(ticket_id) is True

    follow_up = "设备型号是合成测试机，电源灯会亮但屏幕无显示。"
    assert await create_customer_ticket_message(ticket_id, customer_id, follow_up) is not None

    reclaimed = await claim_next_ticket_for_ai(claim_timeout_seconds=120)
    assert reclaimed is not None
    assert reclaimed["ticket_id"] == ticket_id
    assert reclaimed["issue"] == follow_up


@pytest.mark.asyncio
async def test_customer_follow_up_reopens_resolved_ticket_for_ai(ai_ticket_data: dict[str, int | str]) -> None:
    """客户追问后，下一次 AI 领取应读取新问题而不是旧 issue。"""
    ticket_id = str(ai_ticket_data["ticket_id"])
    customer_id = int(ai_ticket_data["customer"])

    assert await claim_next_ticket_for_ai(claim_timeout_seconds=120) is not None
    assert await complete_ai_ticket(ticket_id, "请检查电源连接。") is True

    follow_up = "我已经检查过电源，还是无法开机。"
    created = await create_customer_ticket_message(ticket_id, customer_id, follow_up)
    assert created is not None

    claimed_again = await claim_next_ticket_for_ai(claim_timeout_seconds=120)
    assert claimed_again is not None
    assert claimed_again["ticket_id"] == ticket_id
    assert claimed_again["issue"] == follow_up
