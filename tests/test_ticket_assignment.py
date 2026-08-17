"""工单归属、脱敏和原子认领测试。"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException

from api.tickets import (
    AgentTicketDetailResponse,
    AgentTicketSummaryResponse,
    ClaimResponse,
    CustomerTicketDetailResponse,
)
from api.tickets import (
    claim as claim_endpoint,
)
from api.tickets import (
    ticket as ticket_endpoint,
)
from infra.db_pool import close_pool, get_connection, init_pool, put_connection


def _request(user_id: int, role: str) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(user={"id": user_id, "role": role}),
    )


@pytest_asyncio.fixture
async def _assignment_data():
    await init_pool(minconn=1, maxconn=4)
    conn = await get_connection()
    await conn.set_autocommit(True)

    suffix = uuid.uuid4().hex[:8]
    users: dict[str, int] = {}
    for name, role in (
        ("customer-a", "customer"),
        ("customer-b", "customer"),
        ("agent-a", "agent"),
        ("agent-b", "agent"),
    ):
        cursor = await conn.execute(
            """
            INSERT INTO public.users (username, password_hash, role)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (f"ticket-assignment-{name}-{suffix}", "test-hash", role),
        )
        users[name] = (await cursor.fetchone())[0]

    ticket_a = f"TKT-A-{suffix}"
    ticket_b = f"TKT-B-{suffix}"
    await conn.execute(
        """
        INSERT INTO public.tickets
            (ticket_id, customer_user_id, customer_name, phone, issue, urgency)
        VALUES
            (%s, %s, '客户A', '13800000001', '客户A的问题全文', 'high'),
            (%s, %s, '客户B', '13800000002', '客户B的问题全文', 'medium')
        """,
        (ticket_a, users["customer-a"], ticket_b, users["customer-b"]),
    )
    await put_connection(conn)

    yield {"users": users, "ticket_a": ticket_a, "ticket_b": ticket_b}

    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute(
        "DELETE FROM public.tickets WHERE ticket_id IN (%s, %s)",
        (ticket_a, ticket_b),
    )
    await conn.execute(
        "DELETE FROM public.users WHERE id IN (%s, %s, %s, %s)",
        tuple(users.values()),
    )
    await put_connection(conn)
    await close_pool()


@pytest.mark.asyncio
async def test_unclaimed_agent_gets_redacted_summary(_assignment_data):
    data = _assignment_data
    result = await ticket_endpoint(
        data["ticket_a"],
        _request(data["users"]["agent-a"], "agent"),
    )

    assert isinstance(result, AgentTicketSummaryResponse)
    assert result.assigned_agent_id is None
    assert "customer_name" not in result.model_dump()
    assert "phone" not in result.model_dump()
    assert "issue" not in result.model_dump()


@pytest.mark.asyncio
async def test_customer_can_only_view_own_ticket(_assignment_data):
    data = _assignment_data
    own = await ticket_endpoint(
        data["ticket_a"],
        _request(data["users"]["customer-a"], "customer"),
    )
    assert isinstance(own, CustomerTicketDetailResponse)
    assert own.issue == "客户A的问题全文"

    with pytest.raises(HTTPException) as exc_info:
        await ticket_endpoint(
            data["ticket_b"],
            _request(data["users"]["customer-a"], "customer"),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_only_one_agent_can_claim(_assignment_data):
    data = _assignment_data
    results = await asyncio.gather(
        claim_endpoint(
            data["ticket_a"],
            _request(data["users"]["agent-a"], "agent"),
        ),
        claim_endpoint(
            data["ticket_a"],
            _request(data["users"]["agent-b"], "agent"),
        ),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, ClaimResponse)]
    failures = [result for result in results if isinstance(result, HTTPException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].status_code == 409


@pytest.mark.asyncio
async def test_owner_agent_sees_full_and_other_agent_gets_404(_assignment_data):
    data = _assignment_data
    claimed = await claim_endpoint(
        data["ticket_a"],
        _request(data["users"]["agent-a"], "agent"),
    )
    assert isinstance(claimed, ClaimResponse)

    detail = await ticket_endpoint(
        data["ticket_a"],
        _request(data["users"]["agent-a"], "agent"),
    )
    assert isinstance(detail, AgentTicketDetailResponse)
    assert detail.customer_name == "客户A"
    assert detail.phone == "13800000001"
    assert detail.issue == "客户A的问题全文"

    with pytest.raises(HTTPException) as exc_info:
        await ticket_endpoint(
            data["ticket_a"],
            _request(data["users"]["agent-b"], "agent"),
        )
    assert exc_info.value.status_code == 404
