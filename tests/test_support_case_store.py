"""Support Case 初始状态原子创建的存储 contract tests。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from store.support_case_store import OPEN_CASE_STATUSES, create_open_case


def test_staff_handoff_is_not_an_automatic_chat_open_case() -> None:
    assert OPEN_CASE_STATUSES == {"ACTIVE", "AWAITING_CUSTOMER"}


class _Cursor:
    def __init__(self, row=None):
        self.row = row

    async def fetchone(self):
        return self.row


class _Transaction:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        self.connection.transaction_entered = True
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.rolled_back = exc_type is not None
        return False


class _Connection:
    def __init__(self, row, *, fail_event=False):
        self.row = row
        self.fail_event = fail_event
        self.calls: list[tuple[str, object]] = []
        self.transaction_entered = False
        self.rolled_back = False

    def transaction(self):
        return _Transaction(self)

    async def execute(self, query, params=()):
        self.calls.append((query, params))
        if "FOR UPDATE" in query:
            return _Cursor(None)
        if "INSERT INTO public.support_cases" in query:
            return _Cursor(self.row)
        if "INSERT INTO public.support_case_events" in query:
            if self.fail_event:
                raise RuntimeError("event insert failed")
            return _Cursor(None)
        raise AssertionError(f"unexpected SQL: {query}")


def _row(status: str, pending: dict) -> tuple:
    now = datetime.now(UTC)
    return (
        UUID("00000000-0000-0000-0000-000000000071"),
        UUID("00000000-0000-0000-0000-000000000072"),
        101,
        status,
        [{"domain": "refund", "operation": "status"}],
        {"order_id": "SO-A"},
        {},
        pending,
        {},
        1,
        now,
        now,
        None,
    )


@pytest.mark.asyncio
async def test_initial_pending_and_case_event_roll_back_together_on_failure(monkeypatch):
    pending = {
        "kind": "customer_choice",
        "subject_type": "order",
        "choices": [{"order_id": "SO-B", "product_name": "Sony 耳机"}],
        "selection_event": "subject_correction",
        "transition_from_order_id": "SO-A",
    }
    connection = _Connection(_row("AWAITING_CUSTOMER", pending), fail_event=True)
    monkeypatch.setattr("store.support_case_store.get_connection", AsyncMock(return_value=connection))
    put_connection = AsyncMock()
    monkeypatch.setattr("store.support_case_store.put_connection", put_connection)

    with pytest.raises(RuntimeError, match="event insert failed"):
        await create_open_case(
            session_id=UUID("00000000-0000-0000-0000-000000000072"),
            customer_user_id=101,
            request_stack=[{"domain": "refund", "operation": "status"}],
            selected_subjects={"order_id": "SO-A"},
            initial_status="AWAITING_CUSTOMER",
            initial_pending=pending,
            initial_event_type="AWAITING_CUSTOMER",
            event_payload={"selection_event": "subject_correction"},
            require_new=True,
        )

    assert connection.transaction_entered is True
    assert connection.rolled_back is True
    assert put_connection.await_count == 1
    insert = next(params for query, params in connection.calls if "INSERT INTO public.support_cases" in query)
    assert insert[3] == "AWAITING_CUSTOMER"
    assert getattr(insert[7], "obj", None) == pending
