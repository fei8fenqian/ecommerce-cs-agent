"""人工升级通知状态 API 的范围和脱敏测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.tickets import ticket_escalation


def _request(user_id: int, role: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": user_id, "role": role}))


@pytest.mark.asyncio
async def test_agent_can_read_safe_escalation_delivery_state() -> None:
    record = {
        "status": "RETRY_WAIT",
        "attempts": 2,
        "next_attempt_at": "2026-08-26T10:00:00+00:00",
        "delivered_at": None,
        "last_error_code": "HTTP_ERROR",
    }
    with patch("api.tickets.get_agent_ticket_escalation", new=AsyncMock(return_value=record)) as read:
        result = await ticket_escalation("TK-001", _request(303, "agent"))

    assert result.model_dump() == record
    read.assert_awaited_once_with("TK-001", 303)


@pytest.mark.asyncio
async def test_non_agent_cannot_read_escalation_state() -> None:
    with pytest.raises(HTTPException) as error:
        await ticket_escalation("TK-001", _request(101, "customer"))

    assert error.value.status_code == 403
