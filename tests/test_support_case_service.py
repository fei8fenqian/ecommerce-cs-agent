"""Support Case 业务层测试；store 通过 monkeypatch 替代，不依赖 PostgreSQL。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from service.support_case_service import SupportCaseService
from store.support_case_store import SupportCase


def _case(*, status: str = "ACTIVE", version: int = 1) -> SupportCase:
    return SupportCase(
        case_id=uuid4(),
        session_id=UUID("00000000-0000-0000-0000-000000000001"),
        customer_user_id=7,
        status=status,  # type: ignore[arg-type]
        request_stack=[{"domain": "delivery", "operation": "track"}],
        selected_subjects={"order_ids": ["SO-1"]},
        verified_facts={"delivery_status": "SHIPPED"},
        pending={},
        pending_command={},
        version=version,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        completed_at=None,
    )


@pytest.mark.asyncio
async def test_open_or_resume_creates_case_when_session_has_no_active_case(monkeypatch):
    created: list[dict] = []

    async def no_case(**kwargs):
        return None

    async def create_case(**kwargs):
        created.append(kwargs)
        return _case()

    monkeypatch.setattr("service.support_case_service.get_open_case", no_case)
    monkeypatch.setattr("service.support_case_service.create_open_case", create_case)

    result = await SupportCaseService().open_or_resume(
        session_id="00000000-0000-0000-0000-000000000001",
        customer_user_id=7,
        request_stack=[{"domain": "delivery", "operation": "track"}],
    )

    assert result.created is True
    assert result.case.customer_user_id == 7
    assert created[0]["session_id"] == UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_open_or_resume_preserves_pending_case_instead_of_overwriting_it(monkeypatch):
    active = _case(status="AWAITING_CUSTOMER")

    async def get_case(**kwargs):
        return active

    async def should_not_create(**kwargs):
        raise AssertionError("must resume active case")

    monkeypatch.setattr("service.support_case_service.get_open_case", get_case)
    monkeypatch.setattr("service.support_case_service.create_open_case", should_not_create)

    result = await SupportCaseService().open_or_resume(
        session_id=str(active.session_id),
        customer_user_id=7,
        request_stack=[{"domain": "refund", "operation": "request"}],
    )

    assert result.created is False
    assert result.case == active


@pytest.mark.asyncio
async def test_await_customer_persists_only_a_structured_pending_question(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = _case()
    result = await SupportCaseService().await_customer(
        case,
        pending={"kind": "choice", "options": ["等待", "取消缺货商品"]},
    )

    assert result == case
    assert captured["status"] == "AWAITING_CUSTOMER"
    assert captured["pending"]["kind"] == "choice"
    assert captured["event_type"] == "AWAITING_CUSTOMER"


@pytest.mark.asyncio
async def test_resume_customer_response_keeps_pending_and_records_event(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = _case(status="AWAITING_CUSTOMER")
    result = await SupportCaseService().resume_customer_response(case)

    assert result == case
    assert captured["status"] == "ACTIVE"
    assert captured["pending"] == case.pending
    assert captured["event_type"] == "CUSTOMER_RESPONSE"


def test_prompt_context_exposes_case_state_but_not_internal_audit_fields():
    context = SupportCaseService.to_prompt_context(_case(status="AWAITING_CUSTOMER"))

    assert '"case_status":"AWAITING_CUSTOMER"' in context
    assert '"delivery_status":"SHIPPED"' in context
    assert "case_id" not in context
    assert "customer_user_id" not in context
