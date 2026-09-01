"""Support Case 业务层测试；store 通过 monkeypatch 替代，不依赖 PostgreSQL。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from agent.decision_context import SUBJECT_CONTEXT_RESET_MARKER
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
    monkeypatch.setattr("service.support_case_service.get_latest_case", no_case)
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
async def test_open_or_resume_carries_subject_reset_barrier_across_case_rollover(monkeypatch):
    latest = SupportCase(
        **{
            **_case(status="COMPLETED").__dict__,
            "selected_subjects": {},
            "verified_facts": {
                SUBJECT_CONTEXT_RESET_MARKER: {
                    "from_subject_id": "SO-A",
                    "reason": "customer_disputed_subject",
                }
            },
        }
    )
    created: dict[str, object] = {}

    async def no_active(**kwargs):
        return None

    async def create_case(**kwargs):
        created.update(kwargs)
        return _case()

    monkeypatch.setattr("service.support_case_service.get_open_case", no_active)

    async def latest_case(**kwargs):
        return latest

    monkeypatch.setattr("service.support_case_service.get_latest_case", latest_case)
    monkeypatch.setattr("service.support_case_service.create_open_case", create_case)

    result = await SupportCaseService().open_or_resume(
        session_id=str(latest.session_id),
        customer_user_id=latest.customer_user_id,
        request_stack=[{"domain": "inventory", "operation": "stock"}],
    )

    assert result.created is True
    assert created["initial_verified_facts"] == latest.verified_facts


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
async def test_supersede_subject_correction_description_clears_pending_without_financial_command(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = SupportCase(
        **{
            **_case(status="AWAITING_CUSTOMER").__dict__,
            "pending": {
                "kind": "subject_correction_description",
                "transition_from_order_id": "SO-1",
            },
            "pending_command": {"status": "MUST_NOT_BE_RESTORED"},
        }
    )

    result = await SupportCaseService().supersede_subject_correction_description(case)

    assert result == case
    assert captured["status"] == "ACTIVE"
    assert captured["pending"] == {}
    assert captured["pending_command"] == {}
    assert captured["event_payload"]["reason"] == "SUBJECT_CORRECTION_SUPERSEDED"


@pytest.mark.asyncio
async def test_supersede_retires_disputed_subject_and_transaction_facts(monkeypatch):
    captured: dict = {}

    async def replace(case, **kwargs):
        captured.update(kwargs)
        return case

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    case = SupportCase(
        **{
            **_case(status="AWAITING_CUSTOMER").__dict__,
            "request_stack": [{"domain": "refund", "operation": "status"}],
            "selected_subjects": {"order_id": "SO-A"},
            "verified_facts": {
                "refund_status": "PROCESSING",
                "refund_amount": 899900,
                "_decision_contexts": [
                    {
                        "subject_type": "order",
                        "subject_id": "SO-A",
                        "provenance": "current",
                        "source": "query_refund_status",
                        "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                    }
                ],
            },
            "pending": {
                "kind": "subject_correction_description",
                "transition_from_order_id": "SO-A",
            },
            "pending_command": {"status": "MUST_NOT_BE_RESTORED"},
        }
    )

    await SupportCaseService().supersede_subject_correction_description(case)

    assert captured["status"] == "ACTIVE"
    assert captured["request_stack"] == []
    assert captured["selected_subjects"] == {}
    assert captured["pending"] == {}
    assert captured["pending_command"] == {}
    verified_facts = captured["verified_facts"]
    assert "refund_status" not in verified_facts
    assert "refund_amount" not in verified_facts
    contexts = verified_facts["_decision_contexts"]
    assert contexts and contexts[0]["subject_id"] == "SO-A"
    assert contexts[0]["provenance"] == "historical"
    assert verified_facts[SUBJECT_CONTEXT_RESET_MARKER]["from_subject_id"] == "SO-A"
    assert captured["event_payload"]["request_stack_retired"] is True


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


@pytest.mark.asyncio
async def test_mark_awaiting_staff_requires_a_real_ticket_id():
    with pytest.raises(ValueError, match="ticket_id"):
        await SupportCaseService().mark_awaiting_staff(
            _case(status="ACTIVE"),
            reason="CAPABILITY_GAP",
            handoff_summary={},
        )


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


@pytest.mark.asyncio
async def test_subject_reset_barrier_survives_subjectless_facts_and_completion(monkeypatch):
    marker = {
        "from_subject_id": "SO-A",
        "reason": "customer_disputed_subject",
    }
    case = SupportCase(
        **{
            **_case().__dict__,
            "selected_subjects": {},
            "verified_facts": {SUBJECT_CONTEXT_RESET_MARKER: marker},
        }
    )
    saved: list[SupportCase] = []

    async def replace(current, **kwargs):
        state = {**current.__dict__, **kwargs}
        state = {name: state[name] for name in SupportCase.__dataclass_fields__}
        updated = SupportCase(**state)
        saved.append(updated)
        return updated

    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    service = SupportCaseService()

    after_stock = await service.record_verified_facts(
        case,
        facts={"stock_status": "IN_STOCK"},
    )
    assert after_stock is not None
    assert after_stock.verified_facts[SUBJECT_CONTEXT_RESET_MARKER] == marker
    assert after_stock.verified_facts["stock_status"] == "IN_STOCK"

    after_complete = await service.complete(after_stock, outcome={"completion": "stock_answer"})
    assert after_complete is not None
    assert after_complete.status == "COMPLETED"
    assert after_complete.verified_facts[SUBJECT_CONTEXT_RESET_MARKER] == marker


@pytest.mark.asyncio
async def test_fresh_trusted_new_subject_releases_reset_barrier(monkeypatch):
    case = SupportCase(
        **{
            **_case().__dict__,
            "selected_subjects": {},
            "verified_facts": {
                SUBJECT_CONTEXT_RESET_MARKER: {
                    "from_subject_id": "SO-A",
                    "reason": "customer_disputed_subject",
                }
            },
        }
    )
    captured: dict[str, object] = {}

    async def replace(current, **kwargs):
        captured.update(kwargs)
        return current

    monkeypatch.setattr("service.support_case_service.replace_case", replace)

    await SupportCaseService().record_verified_facts(
        case,
        facts={"refund_status": "COMPLETED"},
        decision_contexts=[
            {
                "subject_type": "order",
                "subject_id": "SO-B",
                "provenance": "current",
                "source": "query_refund_status",
                "facts": {"refund_status": "COMPLETED"},
            }
        ],
    )

    saved = captured["verified_facts"]
    assert isinstance(saved, dict)
    assert SUBJECT_CONTEXT_RESET_MARKER not in saved
    contexts = saved["_decision_contexts"]
    assert isinstance(contexts, list)
    assert {(item["subject_id"], item["provenance"]) for item in contexts} == {("SO-B", "current")}
