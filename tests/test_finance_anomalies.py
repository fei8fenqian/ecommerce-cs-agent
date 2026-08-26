"""财务资金异常扫描的只读查询和权限测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from api.checkout import finance_anomalies
from store.checkout_refund_store import FinanceAnomaly, list_finance_anomalies


def _request(role: str) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user={"id": 202, "role": role}))


@pytest.mark.asyncio
async def test_finance_anomaly_scan_maps_read_only_facts() -> None:
    row = (
        "REFUND_FAILED",
        "refund-1",
        "SO-001",
        "FAILED",
        12500,
        "CNY",
        "渠道拒绝",
        "2026-08-27T10:00:00+00:00",
        3600,
    )
    cursor = SimpleNamespace(fetchall=AsyncMock(return_value=[row]))
    connection = SimpleNamespace(execute=AsyncMock(return_value=cursor))

    with (
        patch("store.checkout_refund_store.get_connection", new=AsyncMock(return_value=connection)),
        patch("store.checkout_refund_store.put_connection", new=AsyncMock()),
    ):
        result = await list_finance_anomalies(timeout_minutes=30, limit=100)

    assert result == [
        FinanceAnomaly(
            anomaly_type="REFUND_FAILED",
            reference_id="refund-1",
            order_no="SO-001",
            status="FAILED",
            amount_cents=12500,
            currency="CNY",
            reason="渠道拒绝",
            occurred_at="2026-08-27T10:00:00+00:00",
            age_seconds=3600,
        )
    ]
    query = str(connection.execute.await_args.args[0])
    assert "REFUND_PENDING_APPROVAL" in query
    assert "REFUND_PROCESSING_TIMEOUT" in query
    assert "PAYMENT_PENDING_TIMEOUT" in query
    assert connection.execute.await_args.args[1] == (30, 30, 100)


@pytest.mark.asyncio
async def test_only_finance_can_read_anomalies() -> None:
    anomaly = FinanceAnomaly(
        anomaly_type="PAYMENT_PENDING_TIMEOUT",
        reference_id="payment-1",
        order_no="SO-001",
        status="PENDING",
        amount_cents=100,
        currency="CNY",
        reason="支付状态长时间未确认",
        occurred_at="2026-08-27T10:00:00+00:00",
        age_seconds=1800,
    )
    with patch("api.checkout.list_finance_anomalies", new=AsyncMock(return_value=[anomaly])) as scan:
        result = await finance_anomalies(_request("finance"))

    assert result.anomalies[0].anomaly_type == "PAYMENT_PENDING_TIMEOUT"
    scan.assert_awaited_once()

    with pytest.raises(HTTPException) as error:
        await finance_anomalies(_request("operator"))
    assert error.value.status_code == 403
