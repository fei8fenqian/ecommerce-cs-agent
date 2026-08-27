"""客服 Support Case 的业务层：保存可恢复状态，不直接执行订单或资金写操作。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from store.support_case_store import (
    OPEN_CASE_STATUSES,
    SupportCase,
    SupportCaseStatus,
    create_open_case,
    get_open_case,
    get_open_case_by_ticket_id,
    replace_case,
)


@dataclass(frozen=True)
class SupportCaseOpenResult:
    """本轮使用的 Case 以及是否由本轮创建。"""

    case: SupportCase
    created: bool


class SupportCaseService:
    """将聊天会话中的复杂客服诉求映射为一个活动 Case。

    Case 只保存经过白名单化的请求、服务端事实和下一步状态；订单、退款等真实写操作
    仍必须调用各自的确定性服务。一个会话同一时刻仅允许一个活动 Case。
    """

    async def open_or_resume(
        self,
        *,
        session_id: str,
        customer_user_id: int,
        request_stack: list[dict[str, Any]],
    ) -> SupportCaseOpenResult:
        """创建或恢复客户在当前会话中的活动 Case。"""
        session_uuid = self._session_uuid(session_id)
        existing = await get_open_case(session_id=session_uuid, customer_user_id=customer_user_id)
        if existing is not None:
            return SupportCaseOpenResult(case=existing, created=False)
        case = await create_open_case(
            session_id=session_uuid,
            customer_user_id=customer_user_id,
            request_stack=request_stack,
        )
        return SupportCaseOpenResult(case=case, created=True)

    async def get_active(
        self,
        *,
        session_id: str,
        customer_user_id: int,
    ) -> SupportCase | None:
        """读取当前会话的活动 Case，不创建也不修改状态。"""
        return await get_open_case(
            session_id=self._session_uuid(session_id),
            customer_user_id=customer_user_id,
        )

    async def resume_customer_response(self, case: SupportCase) -> SupportCase | None:
        """记录客户已回答 pending 问题，但不把原问题或选择丢失。

        用户原话已由 Session 保存；Case 审计只记录“收到答复”，避免重复保存可能含
        联系方式的自由文本。后续 Workflow 会结合原 pending 和会话历史解释答复。
        """
        return await self._replace(
            case,
            status="ACTIVE",
            event_type="CUSTOMER_RESPONSE",
            event_payload={"pending_kind": str(case.pending.get("kind") or "question")},
        )

    async def record_requests(
        self,
        case: SupportCase,
        *,
        request_stack: list[dict[str, Any]],
        event_payload: dict[str, Any] | None = None,
    ) -> SupportCase | None:
        """用本轮经校验的请求更新 Case；并发更新时返回 ``None``。"""
        return await self._replace(
            case,
            status=case.status,
            request_stack=request_stack,
            event_type="REQUESTS_UPDATED",
            event_payload=event_payload or {"request_count": len(request_stack)},
        )

    async def await_customer(
        self,
        case: SupportCase,
        *,
        pending: dict[str, Any],
        pending_command: dict[str, Any] | None = None,
        request_stack: list[dict[str, Any]] | None = None,
        selected_subjects: dict[str, Any] | None = None,
        verified_facts: dict[str, Any] | None = None,
    ) -> SupportCase | None:
        """保存一个明确、可验证的客户问题或选择，等待下一轮恢复。"""
        safe_pending = self._json_object(pending)
        return await self._replace(
            case,
            status="AWAITING_CUSTOMER",
            request_stack=request_stack if request_stack is not None else case.request_stack,
            selected_subjects=selected_subjects if selected_subjects is not None else case.selected_subjects,
            verified_facts=verified_facts if verified_facts is not None else case.verified_facts,
            pending=safe_pending,
            pending_command=(
                self._json_object(pending_command) if pending_command is not None else case.pending_command
            ),
            event_type="AWAITING_CUSTOMER",
            event_payload={"pending_kind": safe_pending.get("kind", "question")},
        )

    async def record_verified_facts(
        self,
        case: SupportCase,
        *,
        facts: dict[str, Any],
        selected_subjects: dict[str, Any] | None = None,
    ) -> SupportCase | None:
        """追加服务端已核验事实；客户自述不得写入该字段。"""
        merged_facts = {**case.verified_facts, **self._json_object(facts)}
        return await self._replace(
            case,
            status=case.status,
            selected_subjects=selected_subjects if selected_subjects is not None else case.selected_subjects,
            verified_facts=merged_facts,
            event_type="FACTS_READ",
            event_payload={"fact_keys": sorted(merged_facts.keys())},
        )

    async def mark_awaiting_staff(
        self,
        case: SupportCase,
        *,
        reason: str,
        handoff_summary: dict[str, Any],
    ) -> SupportCase | None:
        """标记等待员工；创建工单由调用方在已满足业务边界后单独完成。"""
        safe_summary = self._json_object(handoff_summary)
        pending = {
            "kind": "staff_handoff",
            "reason": reason[:120],
            "summary": safe_summary,
        }
        return await self._replace(
            case,
            status="AWAITING_STAFF",
            pending=pending,
            event_type="ESCALATED",
            event_payload={"reason": reason[:120], "summary_keys": sorted(safe_summary.keys())},
        )

    async def complete(self, case: SupportCase, *, outcome: dict[str, Any]) -> SupportCase | None:
        """结束成功处理的 Case；保留事实和审计，不把结果伪装成资金/订单已成功。"""
        safe_outcome = self._json_object(outcome)
        return await self._replace(
            case,
            status="COMPLETED",
            pending={},
            pending_command={},
            event_type="CASE_COMPLETED",
            event_payload=safe_outcome,
        )

    async def complete_for_ticket(self, ticket_id: str, *, outcome: dict[str, Any]) -> bool:
        """在关联人工工单完成后结束等待人工的 Case。"""
        case = await get_open_case_by_ticket_id(ticket_id)
        if case is None:
            return False
        completed = await self.complete(case, outcome=outcome)
        return completed is not None

    async def fail(self, case: SupportCase, *, reason: str) -> SupportCase | None:
        """记录可理解的失败状态，调用方不得把失败答复伪装为成功。"""
        return await self._replace(
            case,
            status="FAILED",
            pending={"kind": "failure", "reason": reason[:300]},
            event_type="CASE_FAILED",
            event_payload={"reason": reason[:300]},
        )

    @staticmethod
    def to_prompt_context(case: SupportCase) -> str:
        """构造受控 Case 摘要供 Workflow 使用，不携带内部审计或其他用户数据。"""
        payload = {
            "case_status": case.status,
            "request_stack": case.request_stack[:3],
            "selected_subjects": case.selected_subjects,
            "verified_facts": case.verified_facts,
            "pending": case.pending,
            "pending_command": case.pending_command,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:6000]

    async def _replace(
        self,
        case: SupportCase,
        *,
        status: SupportCaseStatus,
        request_stack: list[dict[str, Any]] | None = None,
        selected_subjects: dict[str, Any] | None = None,
        verified_facts: dict[str, Any] | None = None,
        pending: dict[str, Any] | None = None,
        pending_command: dict[str, Any] | None = None,
        event_type: str,
        event_payload: dict[str, Any],
    ) -> SupportCase | None:
        if status not in OPEN_CASE_STATUSES | {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"unsupported support case status: {status}")
        return await replace_case(
            case,
            status=status,
            request_stack=request_stack if request_stack is not None else case.request_stack,
            selected_subjects=selected_subjects if selected_subjects is not None else case.selected_subjects,
            verified_facts=verified_facts if verified_facts is not None else case.verified_facts,
            pending=pending if pending is not None else case.pending,
            pending_command=pending_command if pending_command is not None else case.pending_command,
            event_type=event_type,  # type: ignore[arg-type]
            event_payload=self._json_object(event_payload),
        )

    @staticmethod
    def _session_uuid(session_id: str) -> UUID:
        try:
            return UUID(session_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("support case requires a UUID chat session") from exc

    @staticmethod
    def _json_object(value: dict[str, Any]) -> dict[str, Any]:
        """拒绝不可持久化的对象，保证 Case state 可恢复。"""
        if not isinstance(value, dict):
            raise ValueError("support case payload must be an object")
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("support case payload must be JSON serializable") from exc
        if len(encoded) > 12000:
            raise ValueError("support case payload is too large")
        return json.loads(encoded)
