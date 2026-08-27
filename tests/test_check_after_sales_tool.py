"""售后进度工具测试，不连接数据库。"""

from unittest.mock import AsyncMock, patch

import pytest

from agent.tools.check_after_sales import CheckAfterSales
from agent.tools_registry import ToolContext


@pytest.mark.asyncio
async def test_only_customer_can_check_after_sales() -> None:
    result = await CheckAfterSales().execute(tool_context=ToolContext(user_id=7, role="agent"))

    assert result.is_success is False
    assert result.error == "只有登录客户可以查询售后进度"


@pytest.mark.asyncio
async def test_customer_can_read_latest_owned_ticket_progress() -> None:
    tickets = [
        {
            "ticket_id": "TK-1",
            "status": "AI处理中",
            "urgency": "medium",
            "created_at": "2026-08-26T10:00:00+00:00",
            "issue": "不应返回给工具调用方",
            "phone": "13800000000",
        }
    ]
    with (
        patch(
            "agent.tools.check_after_sales.list_customer_tickets",
            new=AsyncMock(return_value=tickets),
        ) as list_tickets,
        patch(
            "agent.tools.check_after_sales.list_customer_after_sale_summaries",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await CheckAfterSales().execute(tool_context=ToolContext(user_id=7, role="customer"))

    list_tickets.assert_awaited_once_with(7)
    assert result.is_success is True
    assert result.data == {
        "count": 1,
        "tickets": [
            {
                "ticket_id": "TK-1",
                "status": "AI处理中",
                "urgency": "medium",
                "created_at": "2026-08-26T10:00:00+00:00",
            }
        ],
    }
    assert "13800000000" not in str(result.data)
    assert "不应返回" not in str(result.data)


@pytest.mark.asyncio
async def test_ticket_id_is_scoped_to_current_customer() -> None:
    with patch(
        "agent.tools.check_after_sales.get_customer_ticket",
        new=AsyncMock(return_value=None),
    ) as get_ticket:
        result = await CheckAfterSales().execute(
            "TK-other",
            tool_context=ToolContext(user_id=7, role="customer"),
        )

    get_ticket.assert_awaited_once_with("TK-other", 7)
    assert result.is_success is False
    assert result.error == "当前没有可查询的售后工单"


@pytest.mark.asyncio
async def test_customer_can_read_owned_after_sale_application_when_no_ticket_exists() -> None:
    after_sales = [
        {
            "after_sale_id": "as-1",
            "order_id": "SO-1",
            "status": "UNDER_REVIEW",
            "reason_code": "QUALITY",
            "refund_amount_cents": 129900,
            "submitted_at": "2026-08-27T10:00:00+00:00",
            "updated_at": "2026-08-27T11:00:00+00:00",
        }
    ]
    with (
        patch("agent.tools.check_after_sales.list_customer_tickets", new=AsyncMock(return_value=[])),
        patch(
            "agent.tools.check_after_sales.list_customer_after_sale_summaries",
            new=AsyncMock(return_value=after_sales),
        ) as list_after_sales,
    ):
        result = await CheckAfterSales().execute(tool_context=ToolContext(user_id=7, role="customer"))

    list_after_sales.assert_awaited_once_with(7)
    assert result.is_success is True
    assert result.data["count"] == 1
    assert result.data["after_sales"][0]["status"] == "UNDER_REVIEW"
    assert result.data["after_sales"][0]["refund_amount_cents"] == 129900


@pytest.mark.asyncio
async def test_customer_sees_ticket_and_after_sale_application_together() -> None:
    with (
        patch(
            "agent.tools.check_after_sales.list_customer_tickets",
            new=AsyncMock(
                return_value=[
                    {
                        "ticket_id": "TK-1",
                        "status": "待人工处理",
                        "urgency": "medium",
                        "created_at": "2026-08-27T10:00:00+00:00",
                    }
                ]
            ),
        ),
        patch(
            "agent.tools.check_after_sales.list_customer_after_sale_summaries",
            new=AsyncMock(
                return_value=[
                    {
                        "after_sale_id": "as-1",
                        "order_id": "SO-1",
                        "status": "UNDER_REVIEW",
                        "reason_code": "QUALITY",
                        "refund_amount_cents": 129900,
                        "submitted_at": "2026-08-27T10:00:00+00:00",
                        "updated_at": "2026-08-27T11:00:00+00:00",
                    }
                ]
            ),
        ),
    ):
        result = await CheckAfterSales().execute(tool_context=ToolContext(user_id=7, role="customer"))

    assert result.is_success is True
    assert result.data["count"] == 2
    assert result.data["tickets"][0]["ticket_id"] == "TK-1"
    assert result.data["after_sales"][0]["after_sale_id"] == "as-1"
