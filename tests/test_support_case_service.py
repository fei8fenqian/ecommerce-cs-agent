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
        pending_command={"status": "PROPOSED_NOT_EXECUTED", "operation": "partial_fulfillment"},
    )

    assert result == case
    assert captured["status"] == "AWAITING_CUSTOMER"
    assert captured["pending"]["kind"] == "choice"
    assert captured["pending_command"]["status"] == "PROPOSED_NOT_EXECUTED"
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


@pytest.mark.asyncio
async def test_select_customer_subject_persists_choice_and_clears_pending(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = SupportCase(
        **{
            **_case(status="AWAITING_CUSTOMER").__dict__,
            "pending": {
                "kind": "customer_choice",
                "choices": [{"order_id": "SO-2", "product_name": "测试手机"}],
            },
        }
    )

    result = await SupportCaseService().select_customer_subject(
        case,
        subject={"order_id": "SO-2", "product_name": "测试手机"},
        selection_source="product",
    )

    assert result == case
    assert captured["status"] == "ACTIVE"
    assert captured["selected_subjects"]["order_id"] == "SO-2"
    assert captured["pending"] == {}
    assert captured["pending_command"] == {}
    assert captured["event_type"] == "CUSTOMER_RESPONSE"
    assert captured["event_payload"]["selection_source"] == "product"
    assert captured["event_payload"]["pending_kind"] == "customer_choice"
    assert captured["event_payload"]["selection_event"] == "subject_selected"


@pytest.mark.asyncio
async def test_complete_for_ticket_closes_linked_staff_case(monkeypatch):
    case = _case(status="AWAITING_STAFF", version=4)
    case = SupportCase(**{**case.__dict__, "pending": {"summary": {"ticket_id": "TK-1"}}})
    captured: dict = {}

    async def get_case(ticket_id: str):
        assert ticket_id == "TK-1"
        return case

    async def replace(current, **kwargs):
        captured.update(kwargs)
        return SupportCase(**{**current.__dict__, "status": "COMPLETED", "version": 5})

    monkeypatch.setattr("service.support_case_service.get_open_case_by_ticket_id", get_case)
    monkeypatch.setattr("service.support_case_service.replace_case", replace)

    result = await SupportCaseService().complete_for_ticket(
        "TK-1",
        outcome={"completion": "ticket_closed"},
    )

    assert result is True
    assert captured["status"] == "COMPLETED"
    assert captured["event_type"] == "CASE_COMPLETED"


def test_prompt_context_exposes_case_state_but_not_internal_audit_fields():
    context = SupportCaseService.to_prompt_context(_case(status="AWAITING_CUSTOMER"))

    assert '"case_status":"AWAITING_CUSTOMER"' in context
    assert '"delivery_status":"SHIPPED"' in context
    assert "case_id" not in context
    assert "customer_user_id" not in context


@pytest.mark.asyncio
async def test_record_verified_facts_preserves_subject_and_provenance_boundaries(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = SupportCase(
        **{
            **_case().__dict__,
            "verified_facts": {
                "_decision_contexts": [
                    {
                        "subject_type": "order",
                        "subject_id": "SO-A",
                        "provenance": "current",
                        "source": "query_refund_status",
                        "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                    }
                ],
                "customer_note": "保留的非交易备注",
            },
        }
    )

    await SupportCaseService().record_verified_facts(
        case,
        facts={"refund_status": "COMPLETED", "refund_amount": 220000},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-B",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED", "refund_amount": 220000},
            }
        ],
    )

    saved = captured["verified_facts"]
    assert saved["customer_note"] == "保留的非交易备注"
    assert "refund_status" not in saved
    assert "refund_amount" not in saved
    contexts = saved["_decision_contexts"]
    assert {(item["subject_id"], item["provenance"]) for item in contexts} == {
        ("SO-A", "historical"),
        ("SO-B", "current"),
    }
