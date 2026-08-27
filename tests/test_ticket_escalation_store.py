"""人工升级记录 Store 的事务和投递状态测试。"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from service.ticket_escalation import TicketEscalationReason
from store.ticket_store import (
    claim_human_escalation,
    enqueue_human_ticket,
    escalate_ai_ticket,
    mark_human_escalation_delivered,
    mark_human_escalation_retry,
)


def _cursor(*, row: tuple[object, ...] | None = None, rowcount: int = 0) -> SimpleNamespace:
    """创建一个足够覆盖 Store 查询的异步游标替身。"""
    return SimpleNamespace(
        fetchone=AsyncMock(return_value=row),
        rowcount=rowcount,
    )


@pytest.mark.asyncio
async def test_escalation_status_message_and_delivery_record_commit_together() -> None:
    """工单状态、客户可见说明和升级记录必须共用一次提交。"""
    update_cursor = _cursor(row=("ticket-1",))
    message_cursor = _cursor()
    generation_cursor = _cursor(row=(1,))
    escalation_cursor = _cursor()
    connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(side_effect=[update_cursor, message_cursor, generation_cursor, escalation_cursor]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        assert (
            await escalate_ai_ticket(
                "ticket-1",
                "已转人工客服继续处理。",
                escalation_reason=TicketEscalationReason.ORDER_OR_PAYMENT_ACTION,
            )
            is True
        )

    connection.commit.assert_awaited_once()
    connection.rollback.assert_not_awaited()
    statements = [str(call.args[0]) for call in connection.execute.await_args_list]
    assert "UPDATE public.tickets" in statements[0]
    assert "INSERT INTO public.ticket_messages" in statements[1]
    assert "ticket_human_escalations" in statements[2]
    assert "INSERT INTO public.ticket_human_escalations" in statements[3]
    assert connection.execute.await_args_list[3].args[1] == (
        "ticket-1",
        1,
        "ORDER_OR_PAYMENT_ACTION",
    )


@pytest.mark.asyncio
async def test_escalation_record_failure_rolls_back_customer_handoff() -> None:
    """升级记录写入失败时不能留下已转人工但无法通知的半成品。"""
    update_cursor = _cursor(row=("ticket-1",))
    message_cursor = _cursor()
    generation_cursor = _cursor(row=(1,))
    connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(
            side_effect=[
                update_cursor,
                message_cursor,
                generation_cursor,
                RuntimeError("database detail must not escape"),
            ]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        with pytest.raises(RuntimeError, match="database detail must not escape"):
            await escalate_ai_ticket("ticket-1", "已转人工客服继续处理。")

    connection.rollback.assert_awaited_once()
    connection.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_enqueue_human_ticket_moves_chat_ticket_and_creates_delivery_record() -> None:
    """聊天入口的工单必须在 AI worker 领取前一次性进入人工通知队列。"""
    status_cursor = _cursor(row=("AI待处理",))
    update_cursor = _cursor(rowcount=1)
    existing_cursor = _cursor(row=None)
    generation_cursor = _cursor(row=(1,))
    insert_cursor = _cursor()
    connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(
            side_effect=[status_cursor, update_cursor, existing_cursor, generation_cursor, insert_cursor]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        assert (
            await enqueue_human_ticket(
                "ticket-1",
                escalation_reason=TicketEscalationReason.EXPLICIT_HUMAN_REQUEST,
            )
            is True
        )

    connection.commit.assert_awaited_once()
    assert "FOR UPDATE" in str(connection.execute.await_args_list[0].args[0])
    assert "SET status = '待人工处理'" in str(connection.execute.await_args_list[1].args[0])
    assert connection.execute.await_args_list[-1].args[1] == ("ticket-1", 1, "EXPLICIT_HUMAN_REQUEST")


@pytest.mark.asyncio
async def test_claim_human_escalation_is_atomic_and_returns_safe_fields() -> None:
    """通知 worker 只能原子领取，并只得到通知所需的非敏感字段。"""
    cursor = _cursor(row=(7, "ticket-1", 1, "KNOWLEDGE_UNAVAILABLE", 1, datetime.now(timezone.utc)))
    connection = SimpleNamespace(set_autocommit=AsyncMock(), execute=AsyncMock(return_value=cursor))

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        record = await claim_human_escalation(7)

    assert record is not None
    assert set(record) == {"id", "ticket_id", "escalation_generation", "reason_code", "attempts", "created_at"}
    sql = str(connection.execute.await_args.args[0])
    assert "status IN ('PENDING', 'RETRY_WAIT')" in sql
    assert "status = 'DELIVERING'" in sql
    assert "attempts = attempts + 1" in sql


@pytest.mark.asyncio
async def test_delivery_success_and_retry_only_update_owned_delivery() -> None:
    """送达和失败回退都必须限定在当前 DELIVERING 记录。"""
    delivered_cursor = _cursor(rowcount=1)
    retry_cursor = _cursor(rowcount=1)
    delivered_connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(return_value=delivered_cursor),
    )
    retry_connection = SimpleNamespace(
        set_autocommit=AsyncMock(),
        execute=AsyncMock(return_value=retry_cursor),
    )

    with (
        patch("store.ticket_store.get_connection", new=AsyncMock(side_effect=[delivered_connection, retry_connection])),
        patch("store.ticket_store.put_connection", new=AsyncMock()),
    ):
        assert await mark_human_escalation_delivered(7) is True
        assert (
            await mark_human_escalation_retry(
                7,
                "FEISHU_TIMEOUT",
                datetime.now(timezone.utc),
            )
            is True
        )

    assert "status = 'DELIVERED'" in str(delivered_connection.execute.await_args.args[0])
    retry_call = retry_connection.execute.await_args
    assert retry_call.args[1][0] == "RETRY_WAIT"
    assert "status = 'DELIVERING'" in str(retry_call.args[0])
