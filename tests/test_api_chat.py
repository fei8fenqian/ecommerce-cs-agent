"""tests/test_api_chat.py — /chat + /chat/stream 端点测试

用 FastAPI TestClient + mock app state，不依赖真实 LLM/Redis/PG。
"""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import tiktoken
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent.customer_presentation import SubjectChoiceInteraction
from agent.decision_context import SUBJECT_CONTEXT_RESET_MARKER
from agent.engines.loop import LoopResult
from agent.llm.intent_router import Intent, SupportRequest
from agent.llm.resolve import resolve_pronouns
from agent.tools_registry import ToolContext, ToolResult
from api.chat import (
    ChatRequest,
    _apply_subject_choice_interaction,
    _attach_answer_trace,
    _await_support_case_customer,
    _build_ticket_issue,
    _can_append_generic_customer_action,
    _claim_chat_run,
    _entities_from_retrieval,
    _is_confirmed_human_handoff,
    _is_current_chat_run,
    _is_pending_case_reply,
    _merge_evidence_context,
    _merge_last_entities,
    _merge_new_support_request,
    _operator_tool_context,
    _persist_support_case_progress,
    _product_entity_for_turn,
    _recent_subject_router_context,
    _record_support_case_facts,
    _resume_pending_case,
    _support_case_needs_customer_turn,
    chat_router,
    chat_stream,
)
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import BaseAppException, DependencyUnavailableError, LLMError
from service.support_case_service import SupportCaseService
from store.support_case_store import SupportCase

# 本文件只测试 HTTP 编排，避免 SessionManager 导入时为了下载 tokenizer
# 访问外网。真实 tokenizer 由 SessionManager/集成环境单独验证。
class _FakeEncoding:
    def encode(self, text: str) -> list[int]:
        return list(text.encode("utf-8"))


with patch.object(tiktoken, "get_encoding", return_value=_FakeEncoding()):
    from agent.llm.session import SessionContext


# =============================================================================
# Mocks
# =============================================================================
class _MockIntentRouter:
    """总是返回 agent（走 AgentLoop，不调 hybrid_search）"""

    async def route(self, query: str = "", history=None, case_context="", knowledge_context="") -> Intent:
        return Intent(
            target="agent",
            table="",
            query=query,
            confidence=0.95,
        )


class _MockTicketIntentRouter:
    """将测试请求稳定路由到客户售后工单闭环。"""

    async def route(self, query: str = "", history=None, case_context="", knowledge_context="") -> Intent:
        return Intent(target="ticket", query=query, confidence=1.0)


class _MockToolRegistry:
    """记录受控工具调用，不连接真实工单数据库。"""

    def __init__(self):
        self.execute = AsyncMock(
            return_value=ToolResult(
                name="create_ticket",
                status="success",
                data={"ticket_id": "TK-DEMO-001"},
            )
        )


class _ChatRunRedis:
    """只覆盖会话运行令牌测试所需的 Redis 操作。"""

    def __init__(self):
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        assert ex > 0
        self.values[key] = value

    async def get(self, key: str) -> bytes | None:
        value = self.values.get(key)
        return value.encode() if value is not None else None


@pytest.mark.asyncio
async def test_later_chat_run_supersedes_earlier_run(monkeypatch):
    """同一会话后发请求必须让旧流失效，而不同 run 不共用令牌。"""
    redis = _ChatRunRedis()
    monkeypatch.setattr("api.chat.get_redis", lambda: redis)

    first_run = await _claim_chat_run("session-a")
    second_run = await _claim_chat_run("session-a")

    assert first_run != second_run
    assert await _is_current_chat_run("session-a", first_run) is False
    assert await _is_current_chat_run("session-a", second_run) is True


class _MockAgentLoop:
    """总是返回固定回答"""

    def __init__(self, answer: str = "Mock 回答"):
        self._answer = answer
        self.last_context = ""
        self.last_query = ""

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        self.last_query = query
        self.last_context = context
        return LoopResult(
            answer=self._answer,
            total_steps=1,
            total_tokens=50,
            total_latency_ms=100.0,
        )

    async def run_stream(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        """模拟流式回答"""
        self.last_query = query
        yield {"event": "start"}
        for char in self._answer:
            yield {"event": "token", "content": char}
        yield {
            "event": "done",
            "answer": self._answer,
            "total_steps": 1,
        }


class _MockSupportWorkflow:
    """记录复杂客服请求是否进入独立 Workflow。"""

    def __init__(self, answer: str = "复杂流程回答"):
        self.answer = answer
        self.calls: list[dict] = []

    async def run(
        self,
        query,
        *,
        context="",
        history=None,
        system_prompt_extra="",
        case_context="",
        support_requests=None,
        tool_context=None,
        selected_subjects=None,
        previous_subjects=None,
        subject_relation="unknown",
        historical_contexts=None,
        allow_historical_explanation=False,
    ):
        self.calls.append(
            {
                "query": query,
                "context": context,
                "history": history,
                "system_prompt_extra": system_prompt_extra,
                "case_context": case_context,
                "support_requests": support_requests,
                "tool_context": tool_context,
                "selected_subjects": selected_subjects,
                "previous_subjects": previous_subjects,
                "subject_relation": subject_relation,
                "historical_contexts": historical_contexts,
                "allow_historical_explanation": allow_historical_explanation,
            }
        )
        return LoopResult(
            answer=self.answer,
            total_steps=2,
            total_tokens=12,
            total_latency_ms=10.0,
        )


def _support_case_request(service: SupportCaseService):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(support_case_service=service)))


def _support_case_fixture(status: str = "ACTIVE") -> SupportCase:
    now = datetime.now(UTC)
    return SupportCase(
        case_id=uuid4(),
        session_id=UUID("00000000-0000-0000-0000-000000000003"),
        customer_user_id=1,
        status=status,  # type: ignore[arg-type]
        request_stack=[],
        selected_subjects={},
        verified_facts={},
        pending={},
        pending_command={},
        version=1,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )


def _awaiting_staff_case(*, ticket_id: str | None = "TK-REFUND-001") -> SupportCase:
    case = _support_case_fixture(status="AWAITING_STAFF")
    pending: dict[str, object] = {"kind": "staff_handoff", "reason": "CUSTOMER_CONFIRMED_HUMAN_HANDOFF"}
    if ticket_id is not None:
        pending["summary"] = {"ticket_id": ticket_id}
    return SupportCase(
        **{
            **case.__dict__,
            "request_stack": [{"domain": "refund", "operation": "request"}],
            "selected_subjects": {"order_id": "SO-Y9000P"},
            "verified_facts": {
                "refund_eligibility": True,
                "shipping_status": "NOT_SHIPPED",
                "_decision_contexts": [
                    {
                        "subject_type": "order",
                        "subject_id": "SO-Y9000P",
                        "provenance": "current",
                        "source": "check_refund_eligibility",
                        "facts": {"refund_eligibility": True, "shipping_status": "NOT_SHIPPED"},
                    }
                ],
            },
            "pending": pending,
        }
    )


def test_recent_completed_case_exposes_subject_without_old_goal_to_router():
    case = SupportCase(
        **{
            **_support_case_fixture(status="COMPLETED").__dict__,
            "request_stack": [{"domain": "refund", "operation": "request"}],
            "selected_subjects": {"order_id": "SO-OLD"},
        }
    )

    context = json.loads(_recent_subject_router_context(case))

    assert context["recent_verified_subject"] == {"subject_type": "order", "subject_id": "SO-OLD"}
    assert "recent_goal" not in context


@pytest.mark.asyncio
async def test_order_list_supersedes_old_non_list_task_frame_even_if_router_says_continue():
    case = SupportCase(
        **{
            **_support_case_fixture(status="ACTIVE").__dict__,
            "request_stack": [{"domain": "refund", "operation": "request"}],
        }
    )
    service = SupportCaseService()
    retired = SupportCase(**{**case.__dict__, "status": "CANCELLED"})
    service.supersede_for_new_request = AsyncMock(return_value=retired)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        case_update="continue",
        requests=[SupportRequest(domain="order", operation="list")],
    )

    result = await _merge_new_support_request(
        _support_case_request(service),
        case=case,
        intent=intent,
    )

    assert result is None
    service.supersede_for_new_request.assert_awaited_once_with(case)


def _superseded_correction_cases() -> tuple[SupportCase, SupportCase, SupportCase, SupportCase]:
    """构造 correction 被新请求取代后的同一 Case 生命周期快照。"""
    context = {
        "subject_type": "order",
        "subject_id": "SO-A",
        "provenance": "current",
        "source": "query_refund_status",
        "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
    }
    case = SupportCase(
        **{
            **_support_case_fixture(status="AWAITING_CUSTOMER").__dict__,
            "request_stack": [{"domain": "refund", "operation": "status"}],
            "selected_subjects": {"order_id": "SO-A"},
            "verified_facts": {
                "refund_status": "PROCESSING",
                "refund_amount": 899900,
                "_decision_contexts": [context],
            },
            "pending": {
                "kind": "subject_correction_description",
                "subject_type": "order",
                "selection_event": "subject_correction",
                "transition_from_order_id": "SO-A",
            },
        }
    )
    retired = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "request_stack": [],
            "selected_subjects": {},
            "verified_facts": {
                "_decision_contexts": [{**context, "provenance": "historical"}],
                SUBJECT_CONTEXT_RESET_MARKER: {
                    "from_subject_id": "SO-A",
                    "reason": "customer_disputed_subject",
                },
            },
            "pending": {},
            "pending_command": {},
            "version": case.version + 1,
        }
    )
    new_request = SupportCase(
        **{
            **retired.__dict__,
            "request_stack": [{"domain": "after_sales", "operation": "after_sales_transition"}],
            "version": retired.version + 1,
        }
    )
    awaiting = SupportCase(
        **{
            **new_request.__dict__,
            "status": "AWAITING_CUSTOMER",
            "pending": {"kind": "customer_clarification"},
            "version": new_request.version + 1,
        }
    )
    return case, retired, new_request, awaiting


@pytest.mark.asyncio
async def test_api_persists_normalized_agent_tool_facts_into_case():
    service = SupportCaseService()
    case = _support_case_fixture()
    recorded = SupportCase(**{**case.__dict__, "verified_facts": {"refund_status": "COMPLETED"}})
    service.record_verified_facts = AsyncMock(return_value=recorded)  # type: ignore[method-assign]

    result = await _record_support_case_facts(
        _support_case_request(service),
        case=case,
        loop_result=LoopResult(
            verified_facts={"query_refund_status": {"status": "success"}},
            decision_facts={"refund_status": "COMPLETED"},
        ),
    )

    assert result == recorded
    service.record_verified_facts.assert_awaited_once_with(case, facts={"refund_status": "COMPLETED"})


@pytest.mark.asyncio
async def test_api_uses_workflow_confirmation_boundary_not_llm_risk():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[
            SupportRequest(
                domain="after_sales",
                operation="after_sales_transition",
                risk="read_only",
            )
        ],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_confirmation",
            "next_action": "AWAITING_CONFIRMATION",
        },
    )

    pending_command = service.await_customer.await_args.kwargs["pending_command"]
    pending = service.await_customer.await_args.kwargs["pending"]
    assert pending["kind"] == "customer_confirmation"
    assert pending_command["status"] == "PROPOSED_NOT_EXECUTED"
    assert pending_command["requires_explicit_confirmation"] is True
    assert pending_command["risk"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_api_does_not_propose_command_for_clarification():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="clarify", risk="read_only")],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_customer",
            "next_action": "ASK_CLARIFICATION",
            "next_actor": "CUSTOMER",
        },
    )

    assert service.await_customer.await_args.kwargs["pending"]["kind"] == "customer_clarification"
    assert service.await_customer.await_args.kwargs["pending_command"] == {}


@pytest.mark.asyncio
async def test_api_persists_displayed_customer_choices_in_pending_frame():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]
    intent = Intent(
        target="agent",
        requests=[SupportRequest(domain="refund", operation="status")],
    )

    await _await_support_case_customer(
        _support_case_request(service),
        case=case,
        intent=intent,
        workflow_progress={
            "goal_status": "awaiting_customer",
            "next_action": "ASK_CHOICE",
            "next_actor": "CUSTOMER",
            "pending_choices": [
                {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                {"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
            ],
        },
    )

    pending = service.await_customer.await_args.kwargs["pending"]
    assert pending["kind"] == "customer_choice"
    assert pending["subject_type"] == "order"
    assert [item["order_id"] for item in pending["choices"]] == ["SOREAL_A6", "SOREAL_A7"]
    assert service.await_customer.await_args.kwargs["pending_command"] == {}


@pytest.mark.asyncio
async def test_api_resolves_pending_choice_without_recreating_case():
    case = _support_case_fixture(status="AWAITING_CUSTOMER")
    case = SupportCase(
        **{
            **case.__dict__,
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                    {"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
                ],
            },
        }
    )
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "selected_subjects": {"order_id": "SOREAL_A7"},
            "pending": {},
            "version": case.version + 1,
        }
    )
    service = SupportCaseService()
    service.select_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]

    resumed = await _resume_pending_case(
        _support_case_request(service),
        case=case,
        raw_query="第二个",
    )

    assert resumed is selected
    assert resumed.case_id == case.case_id
    service.select_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="ordinal",
    )


@pytest.mark.asyncio
async def test_api_keeps_case_awaiting_when_pending_choice_is_ambiguous():
    case = _support_case_fixture(status="AWAITING_CUSTOMER")
    case = SupportCase(
        **{
            **case.__dict__,
            "pending": {
                "kind": "customer_choice",
                "choices": [
                    {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                    {"order_id": "SOREAL_A7", "product_name": "联想笔记本", "amount_cents": 189900},
                ],
            },
        }
    )
    service = SupportCaseService()
    service.select_customer_subject = AsyncMock()  # type: ignore[method-assign]
    service.resume_customer_response = AsyncMock()  # type: ignore[method-assign]
    service.await_customer = AsyncMock(return_value=case)  # type: ignore[method-assign]

    resumed = await _resume_pending_case(
        _support_case_request(service),
        case=case,
        raw_query="笔记本那个",
    )

    assert resumed is case
    service.select_customer_subject.assert_not_awaited()
    service.resume_customer_response.assert_not_awaited()
    service.await_customer.assert_awaited_once()


def _choice_case() -> SupportCase:
    case = _support_case_fixture(status="AWAITING_CUSTOMER")
    return SupportCase(
        **{
            **case.__dict__,
            "request_stack": [
                {
                    "domain": "refund",
                    "operation": "status",
                    "next_step": "LOOKUP",
                    "required_tools": ["track_order"],
                    "risk": "read_only",
                }
            ],
            "pending": {
                "kind": "customer_choice",
                "subject_type": "order",
                "choices": [
                    {"order_id": "SOREAL_A6", "product_name": "戴尔笔记本", "amount_cents": 920000},
                    {"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_structured_subject_choice_revalidates_candidate_and_keeps_case():
    case = _choice_case()
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "selected_subjects": {"order_id": "SOREAL_A7"},
            "pending": {},
            "version": case.version + 1,
        }
    )
    service = SupportCaseService()
    service.select_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    registry = SimpleNamespace(
        execute=AsyncMock(
            return_value=ToolResult(
                name="track_order",
                status="success",
                data={"order_id": "SOREAL_A7"},
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                registry=registry,
                support_case_service=service,
            )
        )
    )

    result = await _apply_subject_choice_interaction(
        request,
        case=case,
        interaction=SubjectChoiceInteraction(subject_id="SOREAL_A7"),
        tool_context=ToolContext(user_id=1, role="customer"),
    )

    assert result is selected
    registry.execute.assert_awaited_once_with(
        "track_order",
        tool_context=ToolContext(user_id=1, role="customer"),
        order_id="SOREAL_A7",
    )
    service.select_customer_subject.assert_awaited_once_with(
        case,
        subject={"order_id": "SOREAL_A7", "product_name": "Sony 耳机", "amount_cents": 189900},
        selection_source="structured_interaction",
    )


@pytest.mark.asyncio
async def test_structured_subject_choice_rejects_non_candidate_without_execution():
    case = _choice_case()
    service = SupportCaseService()
    registry = SimpleNamespace(execute=AsyncMock())
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                registry=registry,
                support_case_service=service,
            )
        )
    )

    with pytest.raises(StarletteHTTPException) as error:
        await _apply_subject_choice_interaction(
            request,
            case=case,
            interaction=SubjectChoiceInteraction(subject_id="SOREAL_OTHER"),
            tool_context=ToolContext(user_id=1, role="customer"),
        )

    assert error.value.status_code == 409
    registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_subject_choice_rejects_ownership_or_stale_verification():
    case = _choice_case()
    service = SupportCaseService()
    registry = SimpleNamespace(
        execute=AsyncMock(
            return_value=ToolResult(
                name="track_order",
                status="success",
                data={"order_id": "SOREAL_OTHER"},
            )
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                registry=registry,
                support_case_service=service,
            )
        )
    )

    with pytest.raises(StarletteHTTPException) as ownership_error:
        await _apply_subject_choice_interaction(
            request,
            case=case,
            interaction=SubjectChoiceInteraction(subject_id="SOREAL_A6"),
            tool_context=ToolContext(user_id=1, role="customer"),
        )
    assert ownership_error.value.status_code == 409
    service.select_customer_subject = AsyncMock(return_value=case)  # type: ignore[method-assign]
    stale_case = SupportCase(**{**case.__dict__, "status": "ACTIVE"})
    with pytest.raises(StarletteHTTPException) as stale_error:
        await _apply_subject_choice_interaction(
            request,
            case=stale_case,
            interaction=SubjectChoiceInteraction(subject_id="SOREAL_A6"),
            tool_context=ToolContext(user_id=1, role="customer"),
        )
    assert stale_error.value.status_code == 409
    service.select_customer_subject.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_subject_choice_chat_skips_router_and_resumes_case(client):
    case = _choice_case()
    selected = SupportCase(
        **{
            **case.__dict__,
            "status": "ACTIVE",
            "selected_subjects": {"order_id": "SOREAL_A7"},
            "pending": {},
            "version": case.version + 1,
        }
    )
    completed = SupportCase(**{**selected.__dict__, "status": "COMPLETED", "version": selected.version + 1})
    service = SupportCaseService()
    service.get_active = AsyncMock(return_value=case)  # type: ignore[method-assign]
    service.get_latest = AsyncMock(return_value=case)  # type: ignore[method-assign]
    service.select_customer_subject = AsyncMock(return_value=selected)  # type: ignore[method-assign]
    service.complete = AsyncMock(return_value=completed)  # type: ignore[method-assign]
    client.app.state.support_case_service = service
    client.app.state.registry.execute = AsyncMock(
        return_value=ToolResult(name="track_order", status="success", data={"order_id": "SOREAL_A7"})
    )
    client.app.state.intent_router.route = AsyncMock(side_effect=AssertionError("结构化选择不应重新路由"))

    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        response = await http_client.post(
            "/api/v1/chat",
            json={
                "session_id": "mock-session-id",
                "interaction": {"type": "subject_choice", "subject_type": "order", "subject_id": "SOREAL_A7"},
            },
        )

    assert response.status_code == 200
    client.app.state.intent_router.route.assert_not_awaited()
    client.app.state.registry.execute.assert_awaited_once_with(
        "track_order",
        tool_context=ToolContext(
            user_id=1,
            role="customer",
            blocked_tools=frozenset({"create_ticket"}),
            allowed_tools=frozenset(
                {
                    "search_product",
                    "search_knowledge",
                    "check_stock",
                    "track_order",
                    "check_payment_status",
                    "query_refund_status",
                    "check_refund_eligibility",
                    "check_after_sales",
                    "compare_products",
                    "search_component",
                }
            ),
        ),
        order_id="SOREAL_A7",
    )
    service.select_customer_subject.assert_awaited_once()
    assert client.app.state.support_workflow_agent.calls[-1]["selected_subjects"] == {"order_id": "SOREAL_A7"}


def test_api_does_not_persist_capability_gap_as_customer_or_staff_turn():
    assert (
        _support_case_needs_customer_turn(
            Intent(target="agent", requests=[SupportRequest(domain="refund", operation="expected_arrival")]),
            workflow_progress={"goal_status": "blocked", "next_actor": "NONE"},
        )
        is False
    )


@pytest.mark.asyncio
async def test_api_completes_capability_gap_without_creating_staff_handoff():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.complete = AsyncMock(return_value=case)  # type: ignore[method-assign]

    await _persist_support_case_progress(
        _support_case_request(service),
        case=case,
        intent=Intent(
            target="agent",
            requests=[SupportRequest(domain="refund", operation="expected_arrival")],
        ),
        loop_result=LoopResult(
            answer="当前无法提供可信到账时间。",
            workflow_progress={
                "goal": "expected_arrival",
                "goal_status": "resolved_with_limitation",
                "next_action": "EXPLAIN_LIMITATION",
                "next_actor": "NONE",
                "reason": "capability_unavailable",
                "unavailable_capabilities": ["query_refund_expected_arrival"],
            },
        ),
    )

    service.complete.assert_awaited_once()
    assert service.complete.await_args.kwargs["outcome"]["completion"] == "safe_partial_answer_returned"


@pytest.mark.asyncio
async def test_api_records_refund_self_service_handoff_as_completed_case_not_refund_success():
    service = SupportCaseService()
    case = _support_case_fixture()
    service.record_verified_facts = AsyncMock(return_value=case)  # type: ignore[method-assign]
    service.complete = AsyncMock(return_value=case)  # type: ignore[method-assign]

    await _persist_support_case_progress(
        _support_case_request(service),
        case=case,
        intent=Intent(target="agent", requests=[SupportRequest(domain="refund", operation="request")]),
        loop_result=LoopResult(
            answer="请在订单页提交退款申请。",
            decision_facts={"refund_entry": "?page=orders&refund_order=SO-1"},
            workflow_progress={
                "goal": "request",
                "goal_status": "resolved",
                "next_action": "SELF_SERVICE_HANDOFF",
                "next_actor": "NONE",
                "resolution_type": "SELF_SERVICE_HANDOFF",
            },
        ),
    )

    service.complete.assert_awaited_once()
    outcome = service.complete.await_args.kwargs["outcome"]
    assert outcome["completion"] == "SELF_SERVICE_HANDOFF"
    assert outcome["resolution_type"] == "SELF_SERVICE_HANDOFF"


def test_product_retrieval_does_not_promote_noncanonical_title_only_row():
    assert _entities_from_retrieval(
        "laptop_products",
        [{"title": "惠普 惠普锐Pro"}],
    ) == {}
    assert _entities_from_retrieval("knowledge_chunks", [{"title": "售后政策"}]) == {}


def test_product_retrieval_preserves_server_owned_candidates_for_explicit_next_turn_selection():
    entities = _entities_from_retrieval(
        "component_products",
        [
            {
                "id": "cooler-1",
                "product_name": "玄冰500",
                "display_title": "九州风神玄冰500",
                "category": "components",
                "component_category": "cooling_product",
            },
            {
                "id": "cooler-2",
                "product_name": "AK400",
                "display_title": "九州风神 AK400",
                "category": "components",
                "component_category": "cooling_product",
            },
        ],
        "推荐两款散热器",
    )

    assert _product_entity_for_turn("就选九州风神玄冰500这款", entities) == {
        "product": "九州风神玄冰500",
        "product_id": "cooler-1",
        "product_category": "components",
        "product_name": "玄冰500",
        "component_category": "cooling_product",
    }
    assert _product_entity_for_turn("购买", entities) is None


def test_router_candidate_ref_promotes_shortened_product_selection_server_side():
    from api.chat import _product_entity_from_intent

    entities = _entities_from_retrieval(
        "component_products",
        [
            {
                "id": "cooler-1",
                "product_name": "利民Peerless Assassin 120 BLACK 逆重力热管散热器，支持双平台",
                "display_title": "利民Peerless Assassin 120 BLACK 逆重力热管散热器，支持双平台",
                "category": "components",
                "component_category": "cooling_product",
            },
            {
                "id": "cooler-2",
                "product_name": "九州风神 AK400",
                "display_title": "九州风神 AK400",
                "category": "components",
                "component_category": "cooling_product",
            },
        ],
        "推荐两款散热器",
    )
    intent = Intent(
        target="agent",
        domain="product",
        operation="purchase",
        subject_refs=["candidate_1"],
    )

    assert _product_entity_from_intent(intent, "换一种说法也不影响选择", entities, None) == {
        "product": "利民Peerless Assassin 120 BLACK 逆重力热管散热器，支持双平台",
        "product_id": "cooler-1",
        "product_category": "components",
        "product_name": "利民Peerless Assassin 120 BLACK 逆重力热管散热器，支持双平台",
        "component_category": "cooling_product",
    }


def test_customer_action_uses_canonical_product_detail_without_rag_table():
    from api.chat import _customer_action_suffix

    assert _customer_action_suffix(
        "agent",
        "",
        "推荐一款散热器",
        "利民 Frozen Magic 360",
        "cooler-1",
        "components",
        "cooling_product",
    ) == "\n\n[查看该商品](?page=product&category=components&product=cooler-1)"


def test_canonical_product_action_removes_model_generic_catalog_fallback():
    from api.chat import _append_customer_action_suffix

    assert _append_customer_action_suffix(
        "可以从目录查看。\n\n[去商品目录查看](?page=catalog)",
        "agent",
        "",
        "购买",
        "利民散热器",
        "cooler-1",
        "components",
        "cooling_product",
    ) == "可以从目录查看。\n\n[查看该商品](?page=product&category=components&product=cooler-1)"


def test_server_owned_product_action_is_allowed_for_safe_fact_response_only():
    assert _can_append_generic_customer_action(
        LoopResult(answer="商品信息", response_control={"mode": "FACT"}),
        trusted_navigation="product_detail_navigation",
    ) is True
    assert _can_append_generic_customer_action(
        LoopResult(answer="请选择", response_control={"mode": "ASK_CHOICE"}),
        trusted_navigation="product_detail_navigation",
    ) is False


def test_ticket_issue_keeps_recent_customer_context():
    issue = _build_ticket_issue(
        "我哪知道订单号，反正就是最近的那单，我想退了",
        [
            {"role": "user", "content": "刚买的电脑突然开不了机，没摔过也没进水"},
            {"role": "assistant", "content": "请提供订单号"},
        ],
    )

    assert "电脑突然开不了机" in issue
    assert "订单号" in issue
    assert "客户售后诉求" in issue


def test_selected_product_context_is_evidence_not_route_override():
    """详情页商品补充公开事实，但不能覆盖已识别的售后 Goal。"""
    intent = Intent(target="agent", domain="refund", operation="status")
    context = _merge_evidence_context(
        "当前商品详情（优先依据）：\n名称：Pallas II DDR5 6000 32G\n支持 XMP 3.0",
        "[知识来源: refund.md / 退款] 退款状态说明",
    )

    assert intent.target == "agent"
    assert intent.domain == "refund"
    assert "Pallas II DDR5 6000 32G" in context
    assert "知识来源" in context


def test_workflow_tool_context_is_not_semantically_cropped_twice():
    """Workflow policy remains the sole semantic capability narrowing layer."""
    context = ToolContext(
        user_id=1,
        role="customer",
        allowed_tools=frozenset({"track_order", "query_refund_status", "check_refund_eligibility"}),
    )
    intent = Intent(
        target="agent",
        domain="refund",
        operation="request",
        requests=[SupportRequest(domain="refund", operation="request")],
    )

    shaped = _operator_tool_context(context, intent)

    assert shaped.allowed_tools == context.allowed_tools


def test_product_purchase_without_canonical_product_keeps_discovery_but_not_stock():
    context = ToolContext(
        user_id=1,
        role="customer",
        allowed_tools=frozenset({"search_product", "search_component", "check_stock"}),
    )
    intent = Intent(
        target="agent",
        domain="product",
        operation="purchase",
        requests=[SupportRequest(domain="product", operation="purchase")],
    )

    shaped = _operator_tool_context(context, intent, canonical_product=False)

    assert shaped.allowed_tools == frozenset({"search_product", "search_component"})


def test_product_purchase_with_canonical_product_needs_no_lookup_tool():
    context = ToolContext(
        user_id=1,
        role="customer",
        allowed_tools=frozenset({"search_product", "search_component", "check_stock"}),
    )
    intent = Intent(
        target="agent",
        domain="product",
        operation="purchase",
        requests=[SupportRequest(domain="product", operation="purchase")],
    )

    shaped = _operator_tool_context(context, intent, canonical_product=True)

    assert shaped.allowed_tools == frozenset()


@pytest.mark.parametrize(
    "answer_source",
    ["DETERMINISTIC_FALLBACK", "OPERATOR_VALIDATED", "CLARIFICATION", "CONTROLLED_ACTION"],
)
def test_answer_trace_does_not_overwrite_response_layer_source(answer_source):
    result = LoopResult(answer="安全回答", answer_source=answer_source)
    intent = Intent(target="agent", domain="refund", operation="status")

    _attach_answer_trace(
        result,
        intent,
        raw_query="为什么",
        retrieval_query="为什么",
        tool_context=ToolContext(user_id=1, role="customer", allowed_tools=frozenset()),
    )

    assert result.answer_source == answer_source
    assert result.answer_trace["answer_source"] == answer_source


def test_router_new_request_wins_over_old_pending_choice_match():
    case = _choice_case()
    intent = Intent(
        target="agent",
        domain="product",
        operation="purchase",
        requests=[SupportRequest(domain="product", operation="purchase")],
        case_update="new_request",
    )

    # “Sony 耳机” is a valid old refund candidate, but the Router has already
    # classified this turn as a new product-purchase Goal.  Pending choice is
    # allowed to interpret continuations, never to resurrect the old refund.
    assert _is_pending_case_reply(case, intent, "我想买 Sony 耳机") is False


class _MockSessionManager:
    """内存版 SessionManager，不依赖 PostgreSQL。"""

    def __init__(self):
        self._sessions: dict[str, SessionContext] = {}

    async def get_or_create(
        self,
        session_id: str | None,
        owner_user_id: int,
    ) -> SessionContext | None:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or "mock-session-id"
        ctx = SessionContext(session_id=sid)
        self._sessions[sid] = ctx
        return ctx

    async def resolve(
        self,
        query: str,
        session_id: str | None,
        owner_user_id: int,
    ) -> str:
        if session_id and session_id in self._sessions:
            return resolve_pronouns(query, self._sessions[session_id].last_entities)
        return query

    async def add_turn(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        result: LoopResult,
    ) -> None:
        ctx = self._sessions.get(session_id)
        if ctx:
            ctx.messages.append({"role": "user", "content": query})
            ctx.messages.append({"role": "assistant", "content": result.answer})
            if result.customer_presentation:
                ctx.messages[-1]["_presentation"] = result.customer_presentation
            if result.last_entities:
                ctx.last_entities.update(result.last_entities)

    async def add_turn_simple(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        answer: str,
        *,
        presentation=None,
    ) -> None:
        ctx = self._sessions.get(session_id)
        if ctx:
            ctx.messages.append({"role": "user", "content": query})
            assistant = {"role": "assistant", "content": answer}
            if presentation:
                assistant["_presentation"] = presentation
            ctx.messages.append(assistant)

    async def truncate_from(
        self,
        session_id: str,
        owner_user_id: int,
        sequence_no: int,
    ) -> bool:
        ctx = self._sessions.get(session_id)
        if ctx is None or sequence_no < 0:
            return False
        ctx.messages = ctx.messages[:sequence_no]
        ctx.last_entities = {}
        return True


# =============================================================================
# TestClient fixture
# =============================================================================
@pytest.fixture
def client(monkeypatch):
    # HTTP 编排测试不加载 embedding 模型或访问知识库；检索行为由独立 RAG 单测覆盖。
    monkeypatch.setattr("api.chat._pre_route_knowledge_context", AsyncMock(return_value=""))
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user = {"id": 1, "username": "test-user", "role": "customer"}
        return await call_next(request)

    app.include_router(chat_router)
    app.state.agent = _MockAgentLoop(answer="这是测试回答")
    app.state.session = _MockSessionManager()
    app.state.intent_router = _MockIntentRouter()
    app.state.registry = _MockToolRegistry()
    # plan_execute agent (used when intent is plan_execute)
    app.state.plan_execute_agent = _MockAgentLoop(
        answer=json.dumps({"answer": "逐步诊断结果", "plan": ["步骤1", "步骤2"]})
    )
    app.state.support_workflow_agent = _MockSupportWorkflow()
    return TestClient(app)


def _install_superseded_correction_case(client, *, query: str):
    case, retired, new_request, awaiting = _superseded_correction_cases()
    service = SupportCaseService()
    service.get_active = AsyncMock(side_effect=[case, retired])  # type: ignore[method-assign]
    service.get_latest = AsyncMock(side_effect=[case, retired])  # type: ignore[method-assign]
    service.supersede_subject_correction_description = AsyncMock(return_value=retired)  # type: ignore[method-assign]
    service.open_or_resume = AsyncMock(return_value=SimpleNamespace(case=new_request))  # type: ignore[method-assign]
    service.supersede_for_new_request = AsyncMock(return_value=retired)  # type: ignore[method-assign]
    service.await_customer = AsyncMock(return_value=awaiting)  # type: ignore[method-assign]
    client.app.state.support_case_service = service
    client.app.state.agent = _MockAgentLoop(answer="这笔退款记录目前显示处理中，金额为 ¥8999.00。")
    client.app.state.support_workflow_agent = _MockSupportWorkflow(
        answer="这笔退款记录目前显示处理中，金额为 ¥8999.00。"
    )
    request = SupportRequest(domain="after_sales", operation="after_sales_transition")
    if query == "人工客服":
        request = SupportRequest(domain="human", operation="human_handoff", risk="staff_approval")
    client.app.state.intent_router.route = AsyncMock(
        return_value=Intent(
            target="agent",
            query=query,
            confidence=1.0,
            domain=request.domain,
            operation=request.operation,
            requests=[request],
            case_update="new_request",
        )
    )
    return service, case, retired, new_request, awaiting


# =============================================================================
# POST /chat
# =============================================================================
class TestChatEndpoint:
    def test_basic_chat(self, client):
        """基本请求 → 返回 200 + ChatResponse 格式"""
        resp = client.post("/api/v1/chat", json={"query": "你好"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "这是测试回答"
        assert "session_id" in data
        assert data["session_id"] == "mock-session-id"
        assert data["total_steps"] == 1
        assert data["total_tokens"] == 50

    @pytest.mark.asyncio
    async def test_awaiting_staff_case_allows_router_to_handle_new_customer_turn(self, client):
        """真实人工 Case 不得在 Router 前吞掉客户后续输入。"""
        case = _awaiting_staff_case()
        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        client.app.state.intent_router.route = AsyncMock(
            return_value=Intent(target="agent", query="你好", confidence=1.0, speech_act="ACKNOWLEDGEMENT")
        )

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            for query in ("为什么无法核验相关业务信息", "那在之前买的联想小新Pro16呢", "啊啊啊啊", "有病是吧"):
                response = await http_client.post("/api/v1/chat", json={"query": query})
                assert response.status_code == 200
                answer = response.json()["answer"]
                assert "SO-Y9000P" not in answer
                assert "退款资格" not in answer
                assert "尚未发货" not in answer

        assert client.app.state.intent_router.route.await_count == 4
        assert client.app.state.support_workflow_agent.calls == []

    @pytest.mark.asyncio
    async def test_async_basic_chat_smoke(self, client):
        """Async ASGI transport must complete the basic customer chat path."""
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "你好"})

        assert response.status_code == 200
        assert response.json()["answer"] == "这是测试回答"

    @pytest.mark.asyncio
    async def test_operator_receives_raw_query_not_retrieval_rewrite(self, client):
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="检索辅助改写",
                domain="product",
                operation="answer",
                confidence=1.0,
            )
        )
        client.app.state.intent_router.route = route
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "我真正想问的是这款能不能装进我的机箱"},
            )

        assert response.status_code == 200
        assert client.app.state.agent.last_query == "我真正想问的是这款能不能装进我的机箱"
        assert route.await_args.args[0] == "我真正想问的是这款能不能装进我的机箱"

    @pytest.mark.asyncio
    async def test_plain_chat_replaces_unbound_refund_transaction_claim(self, client):
        """退款语境下没有可信订单事实时，普通出口不能透传模型交易结论。"""
        client.app.state.agent = _MockAgentLoop(answer="我查询到您名下有两笔退款记录，一笔处理中，另一笔已完成。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "戴尔笔记本和 Sony 耳机这两件都有退款"},
            )

        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "请提供或确认具体订单号" in answer
        assert "处理中" not in answer
        assert "已完成" not in answer
        assert "两笔退款记录" not in answer

    @pytest.mark.asyncio
    async def test_llm_failure_returns_503_and_does_not_save_fake_answer(self, client):
        """LLM 失败不能伪装成 200，也不能写入假的 assistant 消息。"""
        agent_run = AsyncMock(
            side_effect=LLMError(
                "provider failure",
                retry_count=1,
                status_code=503,
                last_response="retry_exhausted",
            )
        )
        client.app.state.agent.run = agent_run
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "请查询订单"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert response.json()["error"]["message"] != "服务暂时不可用"
        assert "provider failure" not in response.text
        agent_run.assert_awaited_once()
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_open_circuit_returns_503_without_fake_chat_result(self, client):
        """熔断后的依赖错误使用统一 503 语义。"""
        agent_run = AsyncMock(side_effect=DependencyUnavailableError("内部依赖详情"))
        client.app.state.agent.run = agent_run
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "你好"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "内部依赖详情" not in response.text
        agent_run.assert_awaited_once()
        client.app.state.session.add_turn.assert_not_awaited()

    def test_chat_with_session_id(self, client):
        """传 session_id → 复用同一个会话"""
        resp1 = client.post(
            "/api/v1/chat",
            json={"query": "问题1", "session_id": "my-session"},
        )
        assert resp1.status_code == 200
        sid1 = resp1.json()["session_id"]
        assert sid1 == "my-session"

        resp2 = client.post(
            "/api/v1/chat",
            json={"query": "问题2", "session_id": "my-session"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["session_id"] == "my-session"

    @pytest.mark.asyncio
    async def test_customer_cannot_edit_an_already_sent_message(self, client):
        """客户消息不可回滚；否则会与 SupportCase 状态失去事务一致性。"""
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            first = await http_client.post(
                "/api/v1/chat",
                json={"query": "旧问题", "session_id": "edit-session"},
            )
            assert first.status_code == 200
            response = await http_client.post(
                "/api/v1/chat",
                json={
                    "query": "新问题",
                    "session_id": "edit-session",
                    "replace_from_sequence": 0,
                },
            )

        assert first.status_code == 200
        assert response.status_code == 400
        messages = client.app.state.session._sessions["edit-session"].messages
        assert messages[0]["content"] == "旧问题"
        assert all(message["content"] != "新问题" for message in messages)

    @pytest.mark.asyncio
    async def test_customer_edit_is_rejected_before_session_lookup(self, client):
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "新问题", "replace_from_sequence": 0},
            )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_new_router_request_supersedes_correction_pending_before_later_bare_text(self, client):
        case = SupportCase(
            **{
                **_support_case_fixture(status="AWAITING_CUSTOMER").__dict__,
                "selected_subjects": {"order_id": "SO-A"},
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
        service.get_active = AsyncMock(side_effect=[case, updated])  # type: ignore[method-assign]
        service.get_latest = AsyncMock(side_effect=[case, updated])  # type: ignore[method-assign]
        service.supersede_subject_correction_description = AsyncMock(return_value=updated)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        client.app.state.intent_router.route = AsyncMock(
            side_effect=[
                Intent(target="agent", query="换货", confidence=1.0, case_update="new_request"),
                Intent(target="agent", query="Sony 耳机", confidence=1.0, case_update="none"),
            ]
        )

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            first = await http_client.post("/api/v1/chat", json={"query": "换货"})
            second = await http_client.post(
                "/api/v1/chat",
                json={"query": "Sony 耳机", "session_id": first.json()["session_id"]},
            )

        assert first.status_code == 200
        assert second.status_code == 200
        service.supersede_subject_correction_description.assert_awaited_once_with(case)
        assert updated.pending == {}

    @pytest.mark.asyncio
    async def test_chat_new_request_retires_disputed_subject_before_workflow(self, client, monkeypatch):
        lookup = AsyncMock()
        monkeypatch.setattr("api.chat.list_customer_checkout_orders", lookup)
        service, case, retired, new_request, awaiting = _install_superseded_correction_case(
            client,
            query="换货",
        )
        first_intent = client.app.state.intent_router.route.return_value
        client.app.state.intent_router.route.side_effect = [
            first_intent,
            Intent(target="agent", query="Sony 耳机", confidence=1.0),
        ]

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "换货"})
            later = await http_client.post(
                "/api/v1/chat",
                json={"query": "Sony 耳机", "session_id": response.json()["session_id"]},
            )

        assert response.status_code == 200
        assert later.status_code == 200
        lookup.assert_not_awaited()
        service.supersede_subject_correction_description.assert_awaited_once_with(case)
        service.supersede_for_new_request.assert_awaited_once()
        assert client.app.state.support_workflow_agent.calls[-1]["selected_subjects"] is None
        assert "处理中" not in response.json()["answer"]
        assert "8999" not in response.json()["answer"]
        assert "处理中" not in later.json()["answer"]
        assert "8999" not in later.json()["answer"]
        assert client.app.state.intent_router.route.await_count == 2
        assert retired.selected_subjects == {}
        assert awaiting.selected_subjects == {}

    @pytest.mark.asyncio
    async def test_chat_stream_human_request_retires_disputed_subject_before_execution(self, client, monkeypatch):
        lookup = AsyncMock()
        monkeypatch.setattr("api.chat.list_customer_checkout_orders", lookup)
        service, case, _, _, _ = _install_superseded_correction_case(
            client,
            query="人工客服",
        )

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "人工客服"})

        assert response.status_code == 200
        lookup.assert_not_awaited()
        service.supersede_subject_correction_description.assert_awaited_once_with(case)
        assert client.app.state.support_workflow_agent.calls[-1]["selected_subjects"] is None
        assert "处理中" not in response.text
        assert "8999" not in response.text

    @pytest.mark.asyncio
    async def test_short_confirmation_defaults_to_stock_lookup(self, client):
        """“需要”默认执行上一轮推荐中已提出的库存查询。"""
        session = client.app.state.session
        session._sessions["follow-up-session"] = SessionContext(
            session_id="follow-up-session",
            messages=[
                {
                    "role": "assistant",
                    "content": "首选推荐：微星魔影15（i7/RTX4070）\n需要我帮你对比其他型号或查询库存吗？",
                }
            ],
            last_entities={"product": "微星魔影15"},
        )
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="查询 微星魔影15 的实时库存",
                confidence=1.0,
            )
        )
        client.app.state.intent_router.route = route
        expected_history = session._sessions["follow-up-session"].history
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "需要", "session_id": "follow-up-session"},
            )

        assert response.status_code == 200
        route.assert_awaited_once_with(
            "需要",
            history=expected_history,
            case_context="",
            knowledge_context="",
            semantic_hints='{"resolved_reference_hint":"查询 微星魔影15 的实时库存"}',
        )

    @pytest.mark.asyncio
    async def test_complex_support_route_uses_support_workflow(self, client):
        """多步骤客服请求进入 SupportWorkflow，不再直接调用普通 AgentLoop。"""
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="核对部分发货订单并询问客户选择",
                confidence=1.0,
                domain="order_fulfillment",
                operation="partial_fulfillment",
                state="needs_customer_choice",
                next_step="ASK_CHOICE",
                required_tools=["track_order", "check_stock"],
            )
        )
        client.app.state.intent_router.route = route
        client.app.state.agent.run = AsyncMock(side_effect=AssertionError("不应走普通 AgentLoop"))

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat",
                json={"query": "订单里有一件缺货，另一件有货，怎么处理？"},
            )

        assert response.status_code == 200
        assert response.json()["answer"].startswith("复杂流程回答")
        assert response.json()["total_steps"] == 2
        assert len(client.app.state.support_workflow_agent.calls) == 1
        assert client.app.state.support_workflow_agent.calls[0]["system_prompt_extra"]
        route.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_short_reply_resumes_pending_support_case(self, client):
        """“第一个”必须回到待选 Case，而不是被路由成孤立 RAG 问题。"""
        active_case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            customer_user_id=1,
            status="AWAITING_CUSTOMER",
            request_stack=[
                {
                    "domain": "refund",
                    "operation": "refund_request",
                    "next_step": "LOOKUP",
                    "required_tools": ["track_order"],
                    "risk": "customer_confirmation",
                }
            ],
            selected_subjects={},
            verified_facts={"track_order": {"status": "success", "data": {"count": 2}}},
            pending={"kind": "customer_choice", "options_limit": 2},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )
        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        resumed_case = SupportCase(**{**active_case.__dict__, "status": "ACTIVE", "version": 3})
        service.resume_customer_response = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        service.await_customer = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        route = AsyncMock(return_value=Intent(target="rag", query="第一个", confidence=0.9, case_update="continue"))
        client.app.state.intent_router.route = route

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat", json={"query": "第一个"})

        assert response.status_code == 200
        assert route.await_args.kwargs["case_context"]
        service.resume_customer_response.assert_awaited_once_with(active_case)
        workflow_call = client.app.state.support_workflow_agent.calls[0]
        assert workflow_call["support_requests"][0]["operation"] == "request"
        assert workflow_call["support_requests"][0]["required_tools"] == ["track_order"]

    @pytest.mark.asyncio
    async def test_human_ticket_is_created_only_after_case_confirmation(self, client):
        active_case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000002"),
            customer_user_id=1,
            status="AWAITING_CUSTOMER",
            request_stack=[
                {
                    "domain": "human",
                    "operation": "human_handoff",
                    "next_step": "ASK_CLARIFICATION",
                    "required_tools": [],
                    "risk": "staff_approval",
                }
            ],
            selected_subjects={},
            verified_facts={},
            pending={"kind": "customer_confirmation"},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )
        assert _is_confirmed_human_handoff(active_case, "是") is True

        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=active_case)  # type: ignore[method-assign]
        resumed_case = SupportCase(**{**active_case.__dict__, "status": "ACTIVE", "version": 3})
        service.resume_customer_response = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        service.mark_awaiting_staff = AsyncMock(return_value=resumed_case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        client.app.state.intent_router.route = AsyncMock(
            return_value=Intent(target="rag", query="是", confidence=0.9, case_update="continue")
        )

        transport = httpx.ASGITransport(app=client.app)
        with patch("api.chat.enqueue_human_ticket", new=AsyncMock(return_value=True)) as enqueue:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
                response = await http_client.post("/api/v1/chat", json={"query": "是"})

        assert response.status_code == 200
        assert "TK-DEMO-001" in response.json()["answer"]
        client.app.state.registry.execute.assert_awaited_once()
        service.mark_awaiting_staff.assert_awaited_once()

        enqueue.assert_awaited_once()

    def test_standalone_human_request_is_a_real_handoff_request(self):
        assert _is_confirmed_human_handoff(None, "转人工") is True
        assert _is_confirmed_human_handoff(None, "我要找人工") is True
        # 复合退款请求仍保留现有先给自助入口的策略。
        assert _is_confirmed_human_handoff(None, "我要退款，请转人工") is False

    def test_read_only_support_case_can_complete_without_a_customer_turn(self):
        assert (
            _support_case_needs_customer_turn(
                Intent(
                    target="agent",
                    domain="delivery",
                    operation="track_order",
                    next_step="LOOKUP",
                    requests=[
                        SupportRequest(
                            domain="delivery",
                            operation="track_order",
                            next_step="LOOKUP",
                            risk="read_only",
                        )
                    ],
                )
            )
            is False
        )

    def test_resolved_workflow_can_complete_after_pending_reply(self):
        """旧 pending 仅供恢复上下文，不能阻止已解决的本轮工作流结束。"""
        case = SupportCase(
            case_id=uuid4(),
            session_id=UUID("00000000-0000-0000-0000-000000000001"),
            customer_user_id=1,
            status="ACTIVE",
            request_stack=[],
            selected_subjects={},
            verified_facts={},
            pending={"kind": "customer_choice", "options_limit": 2},
            pending_command={},
            version=2,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=None,
        )

        assert (
            _support_case_needs_customer_turn(
                Intent(target="agent", domain="delivery", operation="track_order"),
                case,
                {"goal_status": "resolved", "next_action": "ANSWER"},
            )
            is False
        )

    def test_empty_query_rejected(self, client):
        """空 query → 400 (pydantic 校验 min_length=1)"""
        resp = client.post("/api/v1/chat", json={"query": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_query_too_long_rejected(self, client):
        """超长 query → 400"""
        resp = client.post("/api/v1/chat", json={"query": "a" * 2001})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_missing_query_rejected(self, client):
        """缺少必填字段 → 400"""
        resp = client.post("/api/v1/chat", json={})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"

    def test_response_format(self, client):
        """返回的 JSON 结构完整"""
        resp = client.post("/api/v1/chat", json={"query": "测试"})
        data = resp.json()
        assert set(data.keys()) == {"answer", "session_id", "total_steps", "total_tokens"}
        assert isinstance(data["answer"], str)
        assert isinstance(data["session_id"], str)
        assert isinstance(data["total_steps"], int)
        assert isinstance(data["total_tokens"], int)


# =============================================================================
# Product entity lifecycle
# =============================================================================
def test_product_entity_lifecycle_preserves_candidates_after_server_selection():
    entities = {
        "product_candidates": [
            {
                "product": "Kingston NV2",
                "product_id": "p-1",
                "product_name": "NV2",
                "product_category": "components",
            },
            {
                "product": "Samsung 990 Pro",
                "product_id": "p-2",
                "product_name": "990 Pro",
                "product_category": "components",
            },
        ]
    }

    _merge_last_entities(
        entities,
        {
            "product": "Kingston NV2",
            "product_id": "p-1",
            "product_name": "NV2",
            "product_category": "components",
        },
    )

    assert len(entities["product_candidates"]) == 2
    assert entities["product_id"] == "p-1"


# =============================================================================
# POST /chat/stream
# =============================================================================
class TestChatStreamEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("correction_kind", ["description", "multiple", "none_found", "unavailable"])
    async def test_stream_subject_correction_short_circuits_do_not_read_uninitialized_handoff_flags(
        self, client, monkeypatch, correction_kind: str
    ):
        """All correction outcomes rejoin stream control flow without a 500 or ticket."""
        case = SupportCase(
            **{
                **_support_case_fixture(status="ACTIVE").__dict__,
                "request_stack": [{"domain": "refund", "operation": "request"}],
                "selected_subjects": {"order_id": "SO-OLD"},
            }
        )
        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        monkeypatch.setattr(
            "api.chat._prepare_customer_subject_correction",
            AsyncMock(
                return_value=(
                    correction_kind,
                    case,
                    None,
                    [{"order_id": "SO-SSD", "product_name": "测试 SSD", "amount_cents": 66900}],
                )
            ),
        )

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "我想退 SSD"})

        assert response.status_code == 200
        assert '"event": "done"' in response.text
        assert "create_ticket" not in response.text

    @pytest.mark.asyncio
    async def test_awaiting_staff_case_stream_allows_router_to_handle_new_customer_turn(self, client):
        """流式出口与普通出口都不能在 Router 前吞掉人工 Case 后续输入。"""
        case = _awaiting_staff_case()
        service = SupportCaseService()
        service.get_active = AsyncMock(return_value=case)  # type: ignore[method-assign]
        service.get_latest = AsyncMock(return_value=case)  # type: ignore[method-assign]
        client.app.state.support_case_service = service
        client.app.state.intent_router.route = AsyncMock(
            return_value=Intent(target="agent", query="你好", confidence=1.0, speech_act="ACKNOWLEDGEMENT")
        )

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "那在之前买的联想小新Pro16呢"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "SO-Y9000P" not in visible
        assert "退款资格" not in visible
        assert "尚未发货" not in visible
        assert events[-1]["event"] == "done"
        client.app.state.intent_router.route.assert_awaited_once()
        assert client.app.state.support_workflow_agent.calls == []

    @pytest.mark.asyncio
    async def test_stream_refund_follow_up_never_emits_unverified_facts(self, client):
        """跨轮退款上下文必须在任何 customer-visible token 发出前经过同一事实边界。"""
        session = client.app.state.session
        session._sessions["stream-refund-follow-up"] = SessionContext(
            session_id="stream-refund-follow-up",
            messages=[
                {
                    "role": "assistant",
                    "content": "这笔退款目前还在处理中。",
                    "_decision_facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                    "_decision_contexts": [
                        {
                            "subject_type": "order",
                            "subject_id": "SO-STREAM-A3",
                            "provenance": "current",
                            "source": "query_refund_status",
                            "facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                        }
                    ],
                }
            ],
        )
        client.app.state.agent = _MockAgentLoop(
            answer="退款申请已提交，目前处于平台审核处理阶段，一般需要 1-7 个工作日。"
        )

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "页面上显示还在处理中", "session_id": "stream-refund-follow-up"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "1-7 个工作日" not in visible_tokens
        assert "平台审核" not in visible_tokens
        assert "根据上一轮系统查询" in visible_tokens

    @pytest.mark.asyncio
    async def test_stream_subjectless_legacy_refund_facts_do_not_support_transaction_claims(self, client):
        """SSE 不能把旧 flat metadata 当作退款事实来源。"""
        session = client.app.state.session
        session._sessions["stream-subjectless-legacy"] = SessionContext(
            session_id="stream-subjectless-legacy",
            messages=[
                {
                    "role": "assistant",
                    "content": "上一轮查询结果。",
                    "_decision_facts": {"refund_status": "PROCESSING", "refund_amount": 899900},
                }
            ],
        )
        client.app.state.agent = _MockAgentLoop(answer="根据上一轮查询，这笔退款目前还在处理中，退款金额为 ¥8999.00。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "页面上显示还在处理中", "session_id": "stream-subjectless-legacy"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "处理中" not in visible_tokens
        assert "8999" not in visible_tokens
        assert "请提供或确认具体订单号" in visible_tokens

    @pytest.mark.asyncio
    async def test_stream_unbound_refund_transaction_claim_is_never_emitted(self, client):
        """首轮无 subject 的多笔退款结论不能在 SSE token 中泄露。"""
        client.app.state.agent = _MockAgentLoop(answer="我查询到您名下有两笔退款记录，一笔处理中，另一笔已完成。")

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "戴尔笔记本和 Sony 耳机这两件都有退款"},
            )

        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        visible_tokens = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
        assert "请提供或确认具体订单号" in visible_tokens
        assert "处理中" not in visible_tokens
        assert "已完成" not in visible_tokens
        assert "两笔退款记录" not in visible_tokens

    @pytest.mark.asyncio
    async def test_refund_request_offers_self_service_before_creating_ticket(self, client):
        """普通退款申请先进入本人订单页，不应直接制造人工工单。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "我要申请退款"})

        assert response.status_code == 200
        assert '"event": "tool_call"' not in response.text
        assert '"name": "create_ticket"' not in response.text
        assert "?page=orders" in response.text
        assert "需要人工" in response.text
        assert '"event": "done"' in response.text
        client.app.state.registry.execute.assert_not_awaited()
        client.app.state.agent.run_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_progress_update_does_not_create_a_second_ticket(self, client):
        """“已经退了”是退款进度说明，不能被误判为新的售后工单。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "已经退了"})

        assert response.status_code == 200
        assert '"name": "create_ticket"' not in response.text
        assert "不用重复创建工单" in response.text
        assert "?page=orders" in response.text
        client.app.state.registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_human_request_is_guided_before_ticket_creation(self, client):
        """第一次“退款并转人工”先给自助入口，避免一句话直接制造人工任务。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.agent.run_stream = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "我要退款，请转人工"})

        assert response.status_code == 200
        assert '"name": "create_ticket"' not in response.text
        assert "仍需人工" in response.text
        client.app.state.registry.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refund_human_request_after_guidance_can_create_ticket(self, client):
        """客户已收到入口仍坚持人工时，才允许创建人工售后任务。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        session = client.app.state.session
        session._sessions["refund-human-session"] = SessionContext(
            session_id="refund-human-session",
            messages=[
                {
                    "role": "assistant",
                    "content": "[前往我的订单申请退款](?page=orders)",
                }
            ],
        )

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "退款页面我不想弄，转人工", "session_id": "refund-human-session"},
            )

        assert response.status_code == 200
        assert '"name": "create_ticket"' in response.text
        client.app.state.registry.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_customer_ticket_result_is_not_hidden_when_session_history_save_fails(self, client):
        """工单已落库时，会话存档失败不能把成功业务动作伪装成 AI 服务故障。"""
        client.app.state.intent_router = _MockTicketIntentRouter()
        client.app.state.session.add_turn_simple = AsyncMock(side_effect=TypeError("persistence failed"))

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "电脑冒烟了"})

        assert response.status_code == 200
        assert '"event": "done"' in response.text
        assert "TK-DEMO-001" in response.text
        assert "DEPENDENCY_UNAVAILABLE" not in response.text

    @pytest.mark.asyncio
    async def test_stream_short_confirmation_defaults_to_stock_lookup(self, client):
        """短确认在流式路径也会直接进入库存查询。"""
        session = client.app.state.session
        session._sessions["stream-follow-up"] = SessionContext(
            session_id="stream-follow-up",
            messages=[
                {
                    "role": "assistant",
                    "content": "首选推荐：微星魔影15（i7/RTX4070）\n需要我帮你对比其他型号或查询库存吗？",
                }
            ],
            last_entities={"product": "微星魔影15"},
        )
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="查询 微星魔影15 的实时库存",
                confidence=1.0,
            )
        )
        client.app.state.intent_router.route = route
        expected_history = session._sessions["stream-follow-up"].history

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "需要", "session_id": "stream-follow-up"},
            )

        assert response.status_code == 200
        route.assert_awaited_once_with(
            "需要",
            history=expected_history,
            case_context="",
            knowledge_context="",
            semantic_hints='{"resolved_reference_hint":"查询 微星魔影15 的实时库存"}',
        )

    @pytest.mark.asyncio
    async def test_stream_complex_support_route_uses_support_workflow(self, client):
        """流式入口与普通入口使用同一复杂客服 Workflow。"""
        route = AsyncMock(
            return_value=Intent(
                target="agent",
                query="核对配送和部分发货状态",
                confidence=1.0,
                domain="order_fulfillment",
                operation="partial_fulfillment",
                state="needs_customer_choice",
                next_step="ASK_CHOICE",
                required_tools=["track_order", "check_stock"],
            )
        )
        client.app.state.intent_router.route = route
        client.app.state.agent.run_stream = AsyncMock(side_effect=AssertionError("不应走普通流式 AgentLoop"))

        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post(
                "/api/v1/chat/stream",
                json={"query": "一件缺货一件有货，能不能先发有货的？"},
            )

        assert response.status_code == 200
        assert '"event": "done"' in response.text
        assert "复杂流程回答" in response.text
        assert len(client.app.state.support_workflow_agent.calls) == 1

    @pytest.mark.asyncio
    async def test_stream_cancellation_does_not_continue_or_save(self, client):
        from starlette.requests import Request

        blocked = asyncio.Event()
        continued = False

        async def blocked_stream(*args, **kwargs):
            nonlocal continued
            yield {"event": "start"}
            yield {"event": "token", "content": "部分回答"}
            await blocked.wait()
            continued = True
            yield {"event": "done", "answer": "不应发送", "total_steps": 1}

        client.app.state.agent.run_stream = MagicMock(side_effect=blocked_stream)
        client.app.state.session.add_turn = AsyncMock()
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "raw_path": b"/api/v1/chat/stream",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 50000),
            "app": client.app,
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive)
        request.state.user = {"id": 1, "username": "test-user", "role": "customer"}
        stream_response = await chat_stream(ChatRequest(query="你好"), request)
        body_iterator = stream_response.body_iterator

        await anext(body_iterator)
        await anext(body_iterator)
        await anext(body_iterator)
        cancelled = asyncio.create_task(anext(body_iterator))
        await asyncio.sleep(0)
        cancelled.cancel()

        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await body_iterator.aclose()

        assert continued is False
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_setup_llm_failure_returns_503_before_sse(self, client):
        client.app.state.intent_router.route = AsyncMock(side_effect=LLMError("provider failure", status_code=503))

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
        assert "data:" not in response.text
        assert "provider failure" not in response.text

    @pytest.mark.asyncio
    async def test_stream_failure_after_start_sends_error_without_done_or_save(self, client):
        async def broken_stream(*args, **kwargs):
            yield {"event": "start"}
            yield {"event": "token", "content": "部分回答"}
            raise DependencyUnavailableError("provider secret")

        client.app.state.agent.run_stream = MagicMock(side_effect=broken_stream)
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 200
        assert '"event": "error"' in response.text
        assert '"code": "DEPENDENCY_UNAVAILABLE"' in response.text
        assert '"request_id": "-"' in response.text
        assert '"event": "done"' not in response.text
        assert "provider secret" not in response.text
        client.app.state.session.add_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_success_sends_start_token_done_and_saves(self, client):
        client.app.state.session.add_turn = AsyncMock()

        transport = httpx.ASGITransport(app=client.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.post("/api/v1/chat/stream", json={"query": "你好"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert '"event": "start"' in response.text
        assert '"event": "token"' in response.text
        assert '"event": "done"' in response.text
        client.app.state.session.add_turn.assert_awaited_once()

    def test_stream_basic(self, client):
        """SSE 流式请求 → 返回 text/event-stream"""
        with client.stream("POST", "/api/v1/chat/stream", json={"query": "你好"}) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line and line.startswith("data: "):
                    payload = line[6:]  # 去掉 "data: " 前缀
                    events.append(json.loads(payload))

        assert len(events) > 0
        # 第一个事件应该是 start
        assert events[0]["event"] == "start"
        assert events[0]["session_id"] == "mock-session-id"

    def test_stream_contains_done_event(self, client):
        """stream 最终有 done 事件"""
        with client.stream("POST", "/api/v1/chat/stream", json={"query": "测试流式"}) as resp:
            events = []
            for line in resp.iter_lines():
                if line and line.startswith("data: "):
                    events.append(json.loads(line[6:]))

        done_events = [e for e in events if e.get("event") == "done"]
        assert len(done_events) == 1

    def test_stream_empty_query_rejected(self, client):
        """空 query → 400"""
        resp = client.post("/api/v1/chat/stream", json={"query": ""})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"


def test_semantic_product_candidates_expose_trusted_price_but_not_canonical_id():
    from api.chat import _semantic_hint_payload

    payload = json.loads(
        _semantic_hint_payload(
            resolved_query="从这两款里选一个",
            entities={
                "product_candidates": [
                    {
                        "product": "iPhone 17 512GB",
                        "product_id": "phone-17",
                        "product_name": "iPhone 17 512GB",
                        "product_category": "phones",
                        "price_cents": 799900,
                        "brand": "Apple",
                        "storage": "512GB",
                        "screen_size": "6.3-inch",
                    },
                    {
                        "product": "iPhone 16 128GB",
                        "product_id": "phone-16",
                        "product_name": "iPhone 16 128GB",
                        "product_category": "phones",
                        "price_cents": 599900,
                        "brand": "Apple",
                        "storage": "128GB",
                        "screen_size": "6.1-inch",
                    },
                ]
            },
        )
    )

    assert payload["product_candidates"] == [
        {
            "ref": "candidate_1",
            "name": "iPhone 17 512GB",
            "product_category": "phones",
            "component_category": "",
            "price_cents": 799900,
            "brand": "Apple",
            "storage": "512GB",
            "screen_size": "6.3-inch",
        },
        {
            "ref": "candidate_2",
            "name": "iPhone 16 128GB",
            "product_category": "phones",
            "component_category": "",
            "price_cents": 599900,
            "brand": "Apple",
            "storage": "128GB",
            "screen_size": "6.1-inch",
        },
    ]
    assert "product_id" not in json.dumps(payload)


def test_purchase_goal_reuses_server_promoted_product_without_query_phrase_matching():
    from api.chat import _product_entity_from_intent

    entities = {
        "product": "iPhone 17 512GB",
        "product_id": "phone-17",
        "product_name": "iPhone 17 512GB",
        "product_category": "phones",
    }
    intent = Intent(target="agent", domain="product", operation="purchase")

    first = _product_entity_from_intent(intent, "继续吧", entities, None)
    second = _product_entity_from_intent(intent, "按刚才确定的方案进行", entities, None)

    assert first == second == entities


def test_customer_chat_capability_snapshot_for_promoted_product_disallows_checkout_writes():
    from api.chat import _customer_chat_capability_context

    context = _customer_chat_capability_context(
        ToolContext(
            user_id=7,
            role="customer",
            allowed_tools=frozenset({"search_product", "check_stock"}),
        ),
        canonical_product=True,
    )

    assert '"product_detail_navigation":true' in context
    assert '"create_order":false' in context
    assert '"write_shipping_address":false' in context
    assert '"select_payment_method":false' in context
    assert '"submit_checkout":false' in context
    assert '"pay_on_behalf_of_customer":false' in context


def test_customer_product_action_depends_on_canonical_state_not_query_wording():
    from api.chat import _customer_action_suffix

    args = ("agent", "", "", "iPhone 17 512GB", "phone-17", "phones", "")
    action_a = _customer_action_suffix(*args, intent_domain="product", intent_operation="purchase")
    action_b = _customer_action_suffix(
        "agent",
        "",
        "完全不同的自然语言表达",
        "iPhone 17 512GB",
        "phone-17",
        "phones",
        "",
        intent_domain="product",
        intent_operation="purchase",
    )

    assert action_a == action_b == "\n\n[查看该商品](?page=product&category=phones&product=phone-17)"


def test_selected_product_semantic_hint_hides_canonical_product_id():
    from api.chat import _semantic_hint_payload

    payload = json.loads(
        _semantic_hint_payload(
            resolved_query="继续处理当前选中的商品",
            entities={},
            explicit_product={
                "product": "iPhone 17 512GB",
                "product_id": "phone-17",
                "product_category": "phones",
            },
        )
    )

    assert payload["selected_product_context"] == {
        "product": "iPhone 17 512GB",
        "product_category": "phones",
    }
    assert "phone-17" not in json.dumps(payload)


def test_previous_turn_outcome_is_a_state_contract_not_customer_copy():
    from api.chat import _previous_outcome_operator_context, _router_case_context

    messages = [
        {
            "role": "assistant",
            "content": "当前暂时无法继续完成这项操作。",
            "_answer_trace": {
                "domain": "refund",
                "operation": "request",
                "selected_subject": "SO-IP17",
                "subject_resolution_status": "resolved",
                "required_facts": ["refund_eligibility"],
                "known_facts": ["refund_status"],
                "missing_facts": ["refund_eligibility"],
                "failed_capabilities": ["check_refund_eligibility"],
                "goal_status": "blocked",
                "workflow_reason": "fact_tool_failed",
                "next_action": "EXPLAIN_LIMITATION_OR_HANDOFF",
                "next_actor": "NONE",
                "answer_source": "DETERMINISTIC_FALLBACK",
            },
        }
    ]

    router_context = _router_case_context("case_status=ACTIVE", messages)
    assert "previous_turn_outcome=" in router_context
    assert "fact_tool_failed" in router_context
    assert "当前暂时无法继续完成这项操作" not in router_context

    explanation_intent = Intent(
        target="agent",
        domain="refund",
        operation="request",
        fact_scope="explain_previous",
    )
    operator_context = _previous_outcome_operator_context(explanation_intent, messages)
    assert "fact_tool_failed" in operator_context
    assert "check_refund_eligibility" in operator_context
    assert "Provider/交易失败" in operator_context

    current_intent = Intent(
        target="agent",
        domain="refund",
        operation="request",
        fact_scope="current",
    )
    assert _previous_outcome_operator_context(current_intent, messages) == ""


@pytest.mark.asyncio
async def test_catalog_evidence_plan_is_executed_for_product_goal_without_canonical_selection(monkeypatch):
    from api.chat import _acquire_catalog_evidence

    search = AsyncMock(
        side_effect=lambda query, *, table, **kwargs: (
            [
                {
                    "id": "phone-17",
                    "product_id": "phone-17",
                    "product_name": "iPhone 17 512GB",
                    "display_title": "Apple iPhone 17 512GB",
                    "category": "phones",
                    "price": 7999,
                    "comparison_metadata": {"brand": "Apple", "storage": "512GB", "screen_size": "6.3-inch"},
                    "score": 0.9,
                }
            ]
            if table == "phone_products"
            else []
        )
    )
    monkeypatch.setattr("api.chat.hybrid_search", search)
    intent = Intent(target="agent", domain="product", operation="purchase")

    docs, acquired = await _acquire_catalog_evidence(
        required=True,
        intent=intent,
        query="按当前商品继续选择",
        entities={},
        canonical_product=False,
    )

    assert acquired is True
    assert [doc["product_id"] for doc in docs] == ["phone-17"]
    assert {call.kwargs["table"] for call in search.await_args_list} == {
        "laptop_products",
        "phone_products",
        "component_products",
    }


def test_candidate_promotion_preserves_current_candidate_evidence_and_direct_action_cross_category():
    from api.chat import _customer_action_suffix, _product_entity_from_intent

    entities = {
        "product_candidates": [
            {
                "product": "Kingston NV2 1TB",
                "product_id": "ssd-nv2-1tb",
                "product_name": "Kingston NV2 1TB",
                "product_category": "components",
                "component_category": "solid_state_drive",
                "price_cents": 49900,
                "brand": "Kingston",
                "capacity": "1TB",
            },
            {
                "product": "Samsung 990 PRO 2TB",
                "product_id": "ssd-990pro-2tb",
                "product_name": "Samsung 990 PRO 2TB",
                "product_category": "components",
                "component_category": "solid_state_drive",
                "price_cents": 129900,
                "brand": "Samsung",
                "capacity": "2TB",
            },
        ]
    }
    intent = Intent(
        target="agent",
        domain="product",
        operation="purchase",
        subject_refs=["candidate_1"],
    )

    selected = _product_entity_from_intent(intent, "换一种表达", entities, None)
    assert selected is not None
    _merge_last_entities(entities, selected)

    assert entities["product_id"] == "ssd-nv2-1tb"
    assert len(entities["product_candidates"]) == 2
    assert _customer_action_suffix(
        "agent",
        "",
        "任意表达",
        entities["product"],
        entities["product_id"],
        entities["product_category"],
        entities["component_category"],
        intent_domain="product",
        intent_operation="purchase",
    ) == "\n\n[查看该商品](?page=product&category=components&product=ssd-nv2-1tb)"


def test_session_product_lifecycle_keeps_candidates_after_promotion_and_retires_selection_on_fresh_search():
    from agent.llm.session import SessionManager

    current = SessionManager._merge_entities(
        {},
        {
            "product_candidates": [
                {
                    "product": "Acer Swift 14",
                    "product_id": "laptop-acer-14",
                    "product_name": "Acer Swift 14",
                    "product_category": "laptops",
                    "price_cents": 699900,
                },
                {
                    "product": "Dell XPS 13",
                    "product_id": "laptop-dell-13",
                    "product_name": "Dell XPS 13",
                    "product_category": "laptops",
                    "price_cents": 899900,
                },
            ]
        },
    )
    promoted = SessionManager._merge_entities(
        current,
        {
            "product": "Dell XPS 13",
            "product_id": "laptop-dell-13",
            "product_name": "Dell XPS 13",
            "product_category": "laptops",
        },
    )

    assert promoted["product_id"] == "laptop-dell-13"
    assert len(promoted["product_candidates"]) == 2

    refreshed = SessionManager._merge_entities(
        promoted,
        {
            "product_candidates": [
                {
                    "product": "Sony WH-1000XM6",
                    "product_id": "audio-sony-xm6",
                    "product_name": "Sony WH-1000XM6",
                    "product_category": "components",
                    "component_category": "audio",
                }
            ]
        },
    )
    assert "product_id" not in refreshed
    assert refreshed["product_candidates"][0]["product_id"] == "audio-sony-xm6"


def test_orders_navigation_is_server_capability_for_safe_read_modes_not_query_wording():
    from api.chat import _customer_action_suffix, _trusted_navigation_action

    intent = Intent(target="agent", domain="order", operation="list")
    navigation = _trusted_navigation_action(intent, canonical_product=False)

    assert navigation == "orders_navigation"
    for mode in ("FACT", "EXPLANATION", "READ_ONLY", "GENERIC"):
        assert _can_append_generic_customer_action(
            LoopResult(answer="订单说明", response_control={"mode": mode}),
            trusted_navigation=navigation,
        ) is True
    for mode in ("ERROR", "ASK_CLARIFICATION", "ASK_CHOICE", "STAFF_HANDOFF"):
        assert _can_append_generic_customer_action(
            LoopResult(answer="受控状态", response_control={"mode": mode}),
            trusted_navigation=navigation,
        ) is False

    first = _customer_action_suffix("agent", "", "我现在有什么订单", intent_domain="order", intent_operation="list")
    second = _customer_action_suffix("agent", "", "换一种完全不同的表达", intent_domain="order", intent_operation="list")
    assert first == second == "\n\n[查看我的订单](?page=orders)"


def test_customer_capability_snapshot_declares_orders_navigation():
    from api.chat import _customer_chat_capability_context

    context = _customer_chat_capability_context(
        ToolContext(user_id=7, role="customer", allowed_tools=frozenset({"track_order"})),
        canonical_product=False,
    )

    assert '"orders_navigation":true' in context
    assert '"create_order":false' in context


@pytest.mark.asyncio
async def test_real_chat_product_candidate_promotion_persists_canonical_action_across_preferences(client, monkeypatch):
    catalog_docs = [
        {
            "id": "p2105475",
            "product_id": "p2105475",
            "product_name": "苹果iPhone 16 Pro Max（1TB）",
            "display_title": "苹果iPhone 16 Pro Max（1TB）",
            "category": "phones",
            "price": 13999,
            "comparison_metadata": {"brand": "苹果", "model": "iPhone 16 Pro Max", "storage": "1TB", "screen_size": "6.9英寸"},
            "score": 0.99,
        },
        {
            "id": "p2105471",
            "product_id": "p2105471",
            "product_name": "苹果iPhone 16 Pro（1TB）",
            "display_title": "苹果iPhone 16 Pro（1TB）",
            "category": "phones",
            "price": 12999,
            "comparison_metadata": {"brand": "苹果", "model": "iPhone 16 Pro", "storage": "1TB", "screen_size": "6.3英寸"},
            "score": 0.98,
        },
        {
            "id": "p2105470",
            "product_id": "p2105470",
            "product_name": "苹果iPhone 16 Pro（512GB）",
            "display_title": "苹果iPhone 16 Pro（512GB）",
            "category": "phones",
            "price": 10999,
            "comparison_metadata": {"brand": "苹果", "model": "iPhone 16 Pro", "storage": "512GB", "screen_size": "6.3英寸"},
            "score": 0.97,
        },
    ]

    async def fake_hybrid_search(query, *, table, **kwargs):
        return catalog_docs if table == "phone_products" else []

    monkeypatch.setattr("api.chat.hybrid_search", fake_hybrid_search)
    intents = [
        Intent(
            target="agent",
            domain="product",
            operation="search_product",
            requests=[SupportRequest(domain="product", operation="search_product")],
        ),
        Intent(
            target="agent",
            domain="product",
            operation="search_product",
            subject_refs=["candidate_1"],
            requests=[SupportRequest(domain="product", operation="search_product", subject_refs=["candidate_1"])],
        ),
        Intent(
            target="agent",
            domain="product",
            operation="search_product",
            subject_refs=["candidate_2"],
            requests=[SupportRequest(domain="product", operation="search_product", subject_refs=["candidate_2"])],
        ),
        Intent(
            target="agent",
            domain="product",
            operation="search_product",
            subject_refs=["candidate_2"],
            requests=[SupportRequest(domain="product", operation="search_product", subject_refs=["candidate_2"])],
        ),
        Intent(
            target="agent",
            domain="product",
            operation="purchase",
            requests=[SupportRequest(domain="product", operation="purchase")],
        ),
        Intent(
            target="agent",
            domain="product",
            operation="purchase",
            requests=[SupportRequest(domain="product", operation="purchase")],
        ),
    ]
    client.app.state.intent_router.route = AsyncMock(side_effect=intents)
    prompts: list[str] = []

    async def run_agent(query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        prompts.append(system_prompt_extra)
        return LoopResult(
            answer="我会按当前服务端候选继续处理。",
            total_steps=1,
            total_tokens=1,
            response_control={"mode": "READ_ONLY"},
        )

    client.app.state.agent.run = run_agent
    transport = httpx.ASGITransport(app=client.app)
    queries = [
        "给我推荐一台新的iphone",
        "预算无上限",
        "不用太大",
        "1TB就行",
        "直接把链接给我",
        "就买这台",
    ]
    responses = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        for query in queries:
            responses.append(
                await http_client.post(
                    "/api/v1/chat",
                    json={"session_id": "product-e2e", "query": query},
                )
            )

    assert all(response.status_code == 200 for response in responses)
    session_ctx = client.app.state.session._sessions["product-e2e"]
    assert session_ctx.last_entities["product_id"] == "p2105471"
    assert len(session_ctx.last_entities["product_candidates"]) == 3
    assert responses[-2].json()["answer"].endswith(
        "[查看该商品](?page=product&category=phones&product=p2105471)"
    )
    assert responses[-1].json()["answer"].endswith(
        "[查看该商品](?page=product&category=phones&product=p2105471)"
    )
    assert '"product_detail_navigation":true' in prompts[-1]
    assert '"create_order":false' in prompts[-1]
    assert '"write_shipping_address":false' in prompts[-1]
    assert '"select_payment_method":false' in prompts[-1]


@pytest.mark.asyncio
async def test_real_chat_order_list_safe_fact_response_gets_server_orders_navigation(client):
    intent = Intent(
        target="agent",
        domain="order",
        operation="list",
        requests=[SupportRequest(domain="order", operation="list")],
    )
    client.app.state.intent_router.route = AsyncMock(side_effect=[intent, intent])
    client.app.state.support_workflow_agent.run = AsyncMock(
        return_value=LoopResult(
            answer="这些是当前账户下可查询的订单。",
            total_steps=1,
            total_tokens=1,
            response_control={"mode": "FACT"},
            workflow_progress={"goal_status": "resolved", "next_actor": "NONE", "next_action": "ANSWER"},
        )
    )

    transport = httpx.ASGITransport(app=client.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        first = await http_client.post(
            "/api/v1/chat",
            json={"session_id": "order-nav-e2e", "query": "我现在有什么订单"},
        )
        second = await http_client.post(
            "/api/v1/chat",
            json={"session_id": "order-nav-e2e", "query": "那我怎么查看这些订单"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    expected = "[查看我的订单](?page=orders)"
    assert expected in first.json()["answer"]
    assert expected in second.json()["answer"]
    assert "手机号" not in first.json()["answer"]
    assert "手机号" not in second.json()["answer"]
