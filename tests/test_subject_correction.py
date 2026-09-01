"""Controlled order-subject correction tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from agent.customer_presentation import SubjectChoiceInteraction
from agent.llm.intent_router import Intent
from agent.subject_correction import (
    SubjectCorrection,
    detect_subject_correction,
    looks_like_subject_correction_candidate,
    parse_subject_correction,
    resolve_subject_description,
)
from agent.support_subjects import (
    looks_like_bare_subject_description,
    resolve_pending_subject_choice,
)
from agent.tools_registry import ToolContext, ToolResult
from api.chat import (
    _apply_subject_choice_interaction,
    _is_pending_case_reply,
    _prepare_customer_subject_correction,
    _resume_pending_case,
    _supersede_subject_correction_pending,
)
from service.support_case_service import SupportCaseService
from store.support_case_store import SupportCase


def _case(*, status: str = "ACTIVE", order_id: str = "SO-A") -> SupportCase:
    now = datetime.now(UTC)
    return SupportCase(
        case_id=uuid4(),
        session_id=UUID("00000000-0000-0000-0000-000000000051"),
        customer_user_id=101,
        status=status,  # type: ignore[arg-type]
        request_stack=[
            {
                "domain": "refund",
                "operation": "status",
                "next_step": "LOOKUP",
                "required_tools": ["track_order", "query_refund_status"],
                "risk": "read_only",
            }
        ],
        selected_subjects={"order_id": order_id},
        verified_facts={
            "refund_status": "PROCESSING",
            "refund_amount": 899900,
            "_decision_contexts": [
                {
                    "subject_type": "order",
                    "subject_id": order_id,
                    "provenance": "current",
                    "source": "query_refund_status",
                    "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                }
            ],
        },
        pending={},
        pending_command={},
        version=1,
        created_at=now,
        updated_at=now,
        completed_at=now if status in {"COMPLETED", "FAILED", "CANCELLED"} else None,
    )


def _request(service: SupportCaseService, registry: object, detector: object):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                support_case_service=service,
                registry=registry,
                subject_correction_detector=detector,
            )
        )
    )


def _checkout_order(order_id: str, product_name: str, amount_cents: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        order_no=order_id,
        product_name=product_name,
        total_amount_cents=amount_cents,
    )


def test_correction_gate_covers_common_explicit_corrections_but_not_new_order_requests():
    assert looks_like_subject_correction_candidate("前面那个不对，是 Sony 耳机") is True
    assert looks_like_subject_correction_candidate("我弄错了，是 Sony 耳机") is True
    assert looks_like_subject_correction_candidate("那 Sony 那个退款怎么样？") is False
    assert looks_like_subject_correction_candidate("再查一下戴尔那笔") is False


@pytest.mark.asyncio
async def test_correction_gate_invokes_semantic_detector_for_new_phrasings():
    llm = SimpleNamespace(
        chat=AsyncMock(
            return_value=SimpleNamespace(content='{"is_subject_correction":true,"subject_description":"Sony 耳机"}')
        )
    )

    result = await detect_subject_correction(
        llm,
        query="前面那个不对，是 Sony 耳机",
        current_subject_id="SO-A",
    )

    assert result == SubjectCorrection(True, "Sony 耳机")
    llm.chat.assert_awaited_once()


def test_bare_subject_description_continuation_does_not_capture_new_business_request():
    assert looks_like_bare_subject_description("Sony 耳机") is True
    assert looks_like_bare_subject_description("那 Sony 那个退款怎么样？") is False


def test_correction_parser_never_accepts_model_selected_order_id():
    parsed = parse_subject_correction(
        '{"is_subject_correction":true,"subject_description":"Sony 耳机","target_order_id":"SO-B"}'
    )

    assert parsed == SubjectCorrection(True, "Sony 耳机")
    assert not hasattr(parsed, "target_order_id")


def test_subject_description_reuses_resolver_for_unique_multiple_and_none():
    choices = [
        {"order_id": "SO-B", "product_name": "Sony WF 耳机", "amount_cents": 189900},
        {"order_id": "SO-C", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
    ]

    kind, subject, matches = resolve_subject_description("Sony 耳机", choices)
    assert kind == "unique"
    assert subject is not None and subject["order_id"] == "SO-B"
    assert [item["order_id"] for item in matches] == ["SO-B"]

    kind, subject, matches = resolve_subject_description("XPS", choices)
    assert kind == "unique"
    assert subject is not None and subject["order_id"] == "SO-C"
    assert [item["order_id"] for item in matches] == ["SO-C"]

    kind, subject, matches = resolve_subject_description("华为手机", choices)
    assert kind == "none"
    assert subject is None
    assert matches == []


@pytest.mark.parametrize("query", ["第二个", "贵一点", "不是 Sony"])
def test_description_resolver_never_uses_hidden_choice_syntax(query):
    choices = [
        {"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
        {"order_id": "SO-C", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
    ]

    kind, subject, matches = resolve_subject_description(query, choices)

    assert kind == "none"
    assert subject is None
    assert matches == []


def test_description_resolver_does_not_let_comparison_select_an_identity_match():
    choices = [
        {"order_id": "SO-B", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
        {"order_id": "SO-C", "product_name": "戴尔 Inspiron 笔记本", "amount_cents": 420000},
    ]

    kind, subject, matches = resolve_subject_description("贵一点的戴尔", choices)

    assert kind == "none"
    assert subject is None
    assert matches == []


def test_description_resolver_accepts_identity_and_chinese_comma():
    choices = [
        {"order_id": "SO-B", "product_name": "Sony WH-1000XM5 黑色耳机", "amount_cents": 299900},
    ]

    assert looks_like_bare_subject_description("Sony WH-1000XM5，黑色") is True
    kind, subject, matches = resolve_subject_description("Sony WH-1000XM5，黑色", choices)

    assert kind == "unique"
    assert subject is not None and subject["order_id"] == "SO-B"
    assert [item["order_id"] for item in matches] == ["SO-B"]


def test_description_contract_rejects_bare_amount_but_accepts_numeric_model():
    choices = [
        {"order_id": "SO-B", "product_name": "iPhone 15 手机", "amount_cents": 189900},
        {"order_id": "SO-C", "product_name": "Sony WH-1000XM5 耳机", "amount_cents": 299900},
    ]

    assert looks_like_bare_subject_description("1899") is False
    kind, subject, matches = resolve_subject_description("1899", choices)
    assert (kind, subject, matches) == ("none", None, [])

    assert looks_like_bare_subject_description("iPhone 15") is True
    kind, subject, matches = resolve_subject_description("iPhone 15", choices)
    assert kind == "unique"
    assert subject is not None and subject["order_id"] == "SO-B"
    assert looks_like_bare_subject_description("WH-1000XM5") is True
    assert looks_like_bare_subject_description("MacBook Air") is True


@pytest.mark.parametrize(
    "query",
    [
        "Sony价格",
        "Sony多少钱",
        "Sony库存",
        "iPhone 15库存",
        "戴尔参数",
        "Sony和Bose哪个好",
        "推荐Sony耳机",
        "Sony",
    ],
)
def test_description_gate_routes_non_identity_text_back_to_router(query):
    assert looks_like_bare_subject_description(query) is False


def test_description_contract_does_not_use_hidden_exclusion_with_one_candidate():
    choices = [{"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900}]

    kind, subject, matches = resolve_subject_description("不是 Sony", choices)

    assert kind == "none"
    assert subject is None
    assert matches == []


def test_description_resolver_keeps_ambiguous_identity_as_multiple():
    choices = [
        {"order_id": "SO-B", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
        {"order_id": "SO-C", "product_name": "戴尔 Inspiron 笔记本", "amount_cents": 420000},
    ]

    kind, subject, matches = resolve_subject_description("戴尔电脑", choices)

    assert kind == "multiple"
    assert subject is None
    assert [item["order_id"] for item in matches] == ["SO-B", "SO-C"]


@pytest.mark.parametrize(
    "query",
    ["换货", "退货", "保修", "维修", "物流", "发票", "人工客服", "转人工", "不用了", "算了", "谢谢"],
)
def test_description_gate_leaves_business_and_control_replies_to_router(query):
    assert looks_like_bare_subject_description(query) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["换货", "人工客服", "转人工", "不用了", "谢谢"],
)
async def test_non_description_control_turn_does_not_discover_correction_candidates(monkeypatch, query):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    orders = AsyncMock()
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, SimpleNamespace(execute=AsyncMock()), AsyncMock()),
        case=case,
        query=query,
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "none"
    assert updated is case
    assert subject is None
    assert choices == []
    orders.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_kind"),
    [("第二个", "none"), ("贵一点", "none"), ("不是 Sony", "none"), ("1899", "none")],
)
async def test_description_pending_does_not_select_from_hidden_candidates(monkeypatch, query, expected_kind):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock()  # type: ignore[method-assign]
    service.await_subject_correction_description = AsyncMock(return_value=case)  # type: ignore[method-assign]
    orders = AsyncMock(
        return_value=[
            _checkout_order("SO-A", "苹果 MacBook Air", 899900),
            _checkout_order("SO-B", "Sony 耳机", 189900),
            _checkout_order("SO-C", "戴尔笔记本", 920000),
        ]
    )
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)
    detector = AsyncMock(return_value=SubjectCorrection(False))

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, SimpleNamespace(execute=AsyncMock()), detector),
        case=case,
        query=query,
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == expected_kind
    assert updated is case
    assert subject is None
    assert choices == []
    if expected_kind == "none":
        orders.assert_not_awaited()
    else:
        orders.assert_awaited_once()
    service.transition_customer_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_description_pending_identity_match_transitions_after_ownership_check(monkeypatch):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "pending": {},
            "selected_subjects": {"order_id": "SO-B"},
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        AsyncMock(
            return_value=[
                _checkout_order("SO-A", "苹果 MacBook Air", 899900),
                _checkout_order("SO-B", "Sony 耳机", 189900),
            ]
        ),
    )
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-B"}))
    )

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, AsyncMock()),
        case=case,
        query="Sony 耳机",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "unique"
    assert updated is selected
    assert subject is not None and subject["order_id"] == "SO-B"
    assert choices == []


@pytest.mark.asyncio
async def test_description_pending_ambiguous_identity_persists_displayed_choice_frame(monkeypatch):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    pending_case = SupportCase(
        **{
            **case.__dict__,
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SO-B", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
                    {"order_id": "SO-C", "product_name": "戴尔 Inspiron 笔记本", "amount_cents": 420000},
                ],
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    service.prepare_subject_correction = AsyncMock(return_value=pending_case)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        AsyncMock(
            return_value=[
                _checkout_order("SO-A", "苹果 MacBook Air"),
                _checkout_order("SO-B", "戴尔 XPS 笔记本", 920000),
                _checkout_order("SO-C", "戴尔 Inspiron 笔记本", 420000),
            ]
        ),
    )

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, SimpleNamespace(execute=AsyncMock()), AsyncMock()),
        case=case,
        query="戴尔电脑",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "multiple"
    assert updated is pending_case
    assert subject is None
    assert [item["order_id"] for item in choices] == ["SO-B", "SO-C"]
    assert pending_case.pending["kind"] == "customer_choice"


def test_displayed_choice_resolver_still_allows_ordinal_after_choice_frame_exists():
    choices = [
        {"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
        {"order_id": "SO-C", "product_name": "戴尔笔记本", "amount_cents": 920000},
    ]

    selected = resolve_pending_subject_choice("第二个", choices)

    assert selected is not None
    assert selected["choice"]["order_id"] == "SO-C"
    assert selected["selection_source"] == "ordinal"


@pytest.mark.asyncio
async def test_non_description_router_turn_supersedes_correction_pending():
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    updated = SupportCase(**{**case.__dict__, "status": "ACTIVE", "pending": {}, "version": case.version + 1})
    service = SupportCaseService()
    service.supersede_subject_correction_description = AsyncMock(return_value=updated)  # type: ignore[method-assign]
    request = _request(service, SimpleNamespace(execute=AsyncMock()), AsyncMock())

    superseded = await _supersede_subject_correction_pending(request, case=case)

    assert superseded is updated
    assert superseded.pending == {}
    service.supersede_subject_correction_description.assert_awaited_once_with(case)
    # Once the frame is superseded, an unrelated Router ``continue`` cannot
    # resume the old correction workflow.
    assert _is_pending_case_reply(case, Intent(target="agent", case_update="continue"), "换货") is False


@pytest.mark.asyncio
async def test_superseded_correction_pending_does_not_consume_later_bare_text(monkeypatch):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    updated = SupportCase(**{**case.__dict__, "status": "ACTIVE", "pending": {}, "version": case.version + 1})
    service = SupportCaseService()
    service.supersede_subject_correction_description = AsyncMock(return_value=updated)  # type: ignore[method-assign]
    request = _request(service, SimpleNamespace(execute=AsyncMock()), AsyncMock())

    superseded = await _supersede_subject_correction_pending(request, case=case)
    assert superseded is updated

    orders = AsyncMock()
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)
    kind, next_case, subject, choices = await _prepare_customer_subject_correction(
        request,
        case=superseded,
        query="Sony 耳机",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "none"
    assert next_case is updated
    assert subject is None
    assert choices == []
    orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_bound_unique_correction_transitions_subject_without_rerouting(monkeypatch):
    case = _case(order_id="SO-A")
    selected = SupportCase(**{**case.__dict__, "selected_subjects": {"order_id": "SO-B"}, "status": "ACTIVE"})
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        AsyncMock(
            return_value=[
                _checkout_order("SO-A", "苹果 MacBook Air", 899900),
                _checkout_order("SO-B", "Sony WF 耳机", 189900),
            ]
        ),
    )
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-B"}))
    )
    detector = AsyncMock(return_value=SubjectCorrection(True, "Sony 耳机"))

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="不是这个，我搞错了，是 Sony 耳机",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "unique"
    assert updated is selected
    assert subject is not None and subject["order_id"] == "SO-B"
    assert choices == []
    # Candidate ownership verification is a server-side scoped read; it does not
    # grant the Agent a way to bypass the old Case binding.
    verification_context = registry.execute.await_args.kwargs["tool_context"]
    assert verification_context.selected_order_id is None
    service.transition_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SO-B", "product_name": "Sony WF 耳机", "amount_cents": 189900},
        selection_source="subject_correction",
    )


@pytest.mark.asyncio
async def test_bound_multiple_correction_persists_existing_choice_frame(monkeypatch):
    case = _case(order_id="SO-A")
    pending_case = SupportCase(
        **{
            **case.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SO-B", "product_name": "戴尔 XPS 笔记本", "amount_cents": 920000},
                    {"order_id": "SO-C", "product_name": "戴尔 Inspiron 笔记本", "amount_cents": 420000},
                ],
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    service.prepare_subject_correction = AsyncMock(return_value=pending_case)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        AsyncMock(
            return_value=[
                _checkout_order("SO-A", "苹果 MacBook Air"),
                _checkout_order("SO-B", "戴尔 XPS 笔记本", 920000),
                _checkout_order("SO-C", "戴尔 Inspiron 笔记本", 420000),
            ]
        ),
    )
    detector = AsyncMock(return_value=SubjectCorrection(True, "戴尔电脑"))
    registry = SimpleNamespace(execute=AsyncMock())

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="不是 MacBook，是戴尔电脑",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "multiple"
    assert updated is pending_case
    assert subject is None
    assert [item["order_id"] for item in choices] == ["SO-B", "SO-C"]
    service.prepare_subject_correction.assert_awaited_once_with(case, choices=choices)
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_choice_resume_uses_transition_and_revalidates_candidate(monkeypatch):
    case = _case(order_id="SO-A")
    case = SupportCase(
        **{
            **case.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
                    {"order_id": "SO-C", "product_name": "戴尔笔记本", "amount_cents": 920000},
                ],
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "selected_subjects": {"order_id": "SO-B"},
            "pending": {},
            "version": case.version + 1,
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    service.select_customer_subject = AsyncMock()  # type: ignore[method-assign]
    registry = SimpleNamespace(
        execute=AsyncMock(
            return_value=ToolResult(
                name="track_order",
                status="success",
                data={"order_id": "SO-B"},
            )
        )
    )
    request = _request(service, registry, AsyncMock(return_value=SubjectCorrection(False)))

    result = await _resume_pending_case(
        request,
        case=case,
        raw_query="第一个",
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert result is selected
    verification_context = registry.execute.await_args.kwargs["tool_context"]
    assert verification_context.selected_order_id is None
    service.transition_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="ordinal",
    )
    service.select_customer_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_choice_without_tool_context_fails_closed():
    case = _case(order_id="SO-A")
    case = SupportCase(
        **{
            **case.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "customer_choice",
                "choices": [{"order_id": "SO-B", "product_name": "Sony 耳机"}],
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock()  # type: ignore[method-assign]
    request = _request(service, SimpleNamespace(execute=AsyncMock()), AsyncMock())

    result = await _resume_pending_case(request, case=case, raw_query="Sony 耳机")

    assert result is case
    service.transition_customer_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_correction_choice_uses_the_same_transition_path():
    case = _case(order_id="SO-A")
    case = SupportCase(
        **{
            **case.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [{"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900}],
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "pending": {},
            "selected_subjects": {"order_id": "SO-B"},
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    service.select_customer_subject = AsyncMock()  # type: ignore[method-assign]
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-B"}))
    )
    request = _request(service, registry, AsyncMock(return_value=SubjectCorrection(False)))

    result = await _apply_subject_choice_interaction(
        request,
        case=case,
        interaction=SubjectChoiceInteraction(subject_id="SO-B"),
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert result is selected
    service.transition_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="structured_interaction",
    )
    service.select_customer_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_subject_choices_use_checkout_scope_and_limit_thirty(monkeypatch):
    from api.chat import _customer_subject_choices

    checkout_orders = AsyncMock(
        return_value=[
            _checkout_order("SO-B", "Sony 耳机", 189900),
            _checkout_order("SO-C", "戴尔 XPS 笔记本", 920000),
        ]
    )
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        checkout_orders,
    )

    choices = await _customer_subject_choices(101)

    assert [choice["order_id"] for choice in choices] == ["SO-B", "SO-C"]
    checkout_orders.assert_awaited_once_with(101, limit=30)


@pytest.mark.asyncio
async def test_customer_subject_choices_can_reach_the_thirtieth_checkout_order(monkeypatch):
    from api.chat import _customer_subject_choices

    checkout_orders = AsyncMock(
        return_value=[_checkout_order(f"SO-{index:02d}", f"测试商品 {index}", index * 100) for index in range(1, 31)]
    )
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", checkout_orders)

    choices = await _customer_subject_choices(101)

    assert len(choices) == 30
    assert choices[10]["order_id"] == "SO-11"
    assert choices[29]["order_id"] == "SO-30"
    checkout_orders.assert_awaited_once_with(101, limit=30)


@pytest.mark.asyncio
async def test_correction_with_no_candidate_does_not_switch_or_query_old_order(monkeypatch):
    case = _case(order_id="SO-A")
    service = SupportCaseService()
    pending_case = SupportCase(
        **{
            **case.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service.await_subject_correction_description = AsyncMock(return_value=pending_case)  # type: ignore[method-assign]
    monkeypatch.setattr(
        "api.chat.list_customer_checkout_orders",
        AsyncMock(return_value=[_checkout_order("SO-A", "苹果 MacBook Air")]),
    )
    registry = SimpleNamespace(execute=AsyncMock())
    detector = AsyncMock(return_value=SubjectCorrection(True, "华为 MateBook"))

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="不是这个，是华为电脑",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "description"
    assert updated is pending_case
    assert subject is None
    assert choices == []
    registry.execute.assert_not_awaited()
    service.await_subject_correction_description.assert_awaited_once_with(case)


@pytest.mark.asyncio
async def test_subject_correction_description_continuation_resolves_without_router(monkeypatch):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "pending": {},
            "selected_subjects": {"order_id": "SO-B"},
        }
    )
    service = SupportCaseService()
    service.transition_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    orders = AsyncMock(
        return_value=[
            _checkout_order("SO-A", "苹果 MacBook Air", 899900),
            _checkout_order("SO-B", "Sony 耳机", 189900),
        ]
    )
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)
    detector = AsyncMock(side_effect=AssertionError("bare continuation must not invoke correction detector"))
    registry = SimpleNamespace(
        execute=AsyncMock(return_value=ToolResult(name="track_order", status="success", data={"order_id": "SO-B"}))
    )

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="Sony 耳机",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "unique"
    assert updated is selected
    assert subject is not None and subject["order_id"] == "SO-B"
    assert choices == []
    detector.assert_not_awaited()
    service.transition_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SO-B", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="subject_correction_description",
    )


@pytest.mark.asyncio
async def test_subject_correction_description_new_request_returns_to_router(monkeypatch):
    case = SupportCase(
        **{
            **_case(order_id="SO-A").__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    service = SupportCaseService()
    orders = AsyncMock()
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)
    detector = AsyncMock(return_value=SubjectCorrection(False))
    registry = SimpleNamespace(execute=AsyncMock())

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="那 Sony 那个退款怎么样？",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "none"
    assert updated is case
    assert subject is None
    assert choices == []
    orders.assert_not_awaited()
    detector.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_correction_does_not_modify_bound_case_or_candidates(monkeypatch):
    case = _case(order_id="SO-A")
    service = SupportCaseService()
    orders = AsyncMock()
    monkeypatch.setattr("api.chat.list_customer_checkout_orders", orders)
    detector = AsyncMock(return_value=SubjectCorrection(False))
    registry = SimpleNamespace(execute=AsyncMock())

    kind, updated, subject, choices = await _prepare_customer_subject_correction(
        _request(service, registry, detector),
        case=case,
        query="那 Sony 那个退款怎么样？",
        history=[],
        customer_user_id=101,
        tool_context=ToolContext(user_id=101, role="customer", selected_order_id="SO-A"),
    )

    assert kind == "none"
    assert updated is case
    assert subject is None
    assert choices == []
    orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_correction_creates_derived_case_instead_of_reopening(monkeypatch):
    terminal = _case(status="COMPLETED", order_id="SO-A")
    derived = _case(status="ACTIVE", order_id="SO-A")
    created_kwargs: dict[str, object] = {}
    replace = AsyncMock()

    async def create_open_case(**kwargs):
        created_kwargs.update(kwargs)
        return SupportCase(
            **{
                **derived.__dict__,
                "status": kwargs["initial_status"],
                "pending": kwargs["initial_pending"],
            }
        )

    monkeypatch.setattr("service.support_case_service.create_open_case", create_open_case)
    monkeypatch.setattr("service.support_case_service.replace_case", replace)
    service = SupportCaseService()

    updated = await service.prepare_subject_correction(
        terminal,
        choices=[{"order_id": "SO-B", "product_name": "Sony 耳机"}],
    )

    assert updated is not None
    assert updated.case_id == derived.case_id
    assert updated.status == "AWAITING_CUSTOMER"
    assert updated.pending["selection_event"] == "subject_correction"
    assert created_kwargs["require_new"] is True
    assert created_kwargs["selected_subjects"] == {"order_id": "SO-A"}
    assert created_kwargs["initial_status"] == "AWAITING_CUSTOMER"
    assert created_kwargs["initial_pending"] == updated.pending
    assert created_kwargs["initial_event_type"] == "AWAITING_CUSTOMER"
    assert created_kwargs["event_payload"]["derived_from_case_id"] == str(terminal.case_id)  # type: ignore[index]
    replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_correction_race_returns_without_follow_up_replace(monkeypatch):
    terminal = _case(status="COMPLETED", order_id="SO-A")
    create = AsyncMock(return_value=None)
    replace = AsyncMock()
    monkeypatch.setattr("service.support_case_service.create_open_case", create)
    monkeypatch.setattr("service.support_case_service.replace_case", replace)

    updated = await SupportCaseService().prepare_subject_correction(
        terminal,
        choices=[{"order_id": "SO-B", "product_name": "Sony 耳机"}],
    )

    assert updated is None
    create.assert_awaited_once()
    replace.assert_not_awaited()


@pytest.mark.asyncio
async def test_transition_clears_old_flat_transaction_facts_and_keeps_context_audit(monkeypatch):
    case = _case(order_id="SO-A")
    captured: dict[str, object] = {}

    async def replace_case(current, **kwargs):
        captured.update(kwargs)
        return current

    monkeypatch.setattr("service.support_case_service.replace_case", replace_case)

    result = await SupportCaseService().transition_customer_subject(
        case,
        subject={"order_id": "SO-B"},
        selection_source="product",
    )

    assert result is case
    assert captured["status"] == "ACTIVE"
    assert captured["selected_subjects"] == {"order_id": "SO-B"}
    assert "refund_status" not in captured["verified_facts"]  # type: ignore[operator]
    assert "refund_amount" not in captured["verified_facts"]  # type: ignore[operator]
    contexts = captured["verified_facts"]["_decision_contexts"]  # type: ignore[index]
    assert contexts and all(item["provenance"] == "historical" for item in contexts)
    assert captured["event_payload"]["old_subject_id"] == "SO-A"  # type: ignore[index]
    assert captured["event_payload"]["new_subject_id"] == "SO-B"  # type: ignore[index]
