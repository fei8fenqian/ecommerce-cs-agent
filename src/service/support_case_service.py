"""客服 Support Case 的业务层：保存可恢复状态，不直接执行订单或资金写操作。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agent.decision_context import (
    SUBJECT_BOUND_FACTS,
    SUBJECT_CONTEXT_RESET_MARKER,
    historicalize_decision_contexts,
    merge_decision_contexts,
)
from agent.support_subjects import MAX_ORDER_CHOICE_OPTIONS
from store.support_case_store import (
    OPEN_CASE_STATUSES,
    STAFF_CASE_STATUSES,
    SupportCase,
    SupportCaseStatus,
    create_open_case,
    get_latest_case,
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
        initial_selected_subjects: dict[str, Any] | None = None,
    ) -> SupportCaseOpenResult:
        """创建或恢复客户在当前会话中的活动 Case。"""
        session_uuid = self._session_uuid(session_id)
        existing = await get_open_case(session_id=session_uuid, customer_user_id=customer_user_id)
        if existing is not None:
            return SupportCaseOpenResult(case=existing, created=False)
        # A disputed subject remains retired across Case rollover.  Carry only
        # the internal barrier forward; never carry the old subject facts or
        # selection into a new Case.
        latest = await get_latest_case(session_id=session_uuid, customer_user_id=customer_user_id)
        initial_verified_facts: dict[str, Any] | None = None
        if latest is not None:
            reset_marker = latest.verified_facts.get(SUBJECT_CONTEXT_RESET_MARKER)
            if isinstance(reset_marker, dict):
                initial_verified_facts = {
                    SUBJECT_CONTEXT_RESET_MARKER: self._json_object(reset_marker),
                }
        create_kwargs: dict[str, Any] = {
            "session_id": session_uuid,
            "customer_user_id": customer_user_id,
            "request_stack": request_stack,
        }
        if initial_verified_facts is not None:
            create_kwargs["initial_verified_facts"] = initial_verified_facts
        if initial_selected_subjects:
            selected_order_id = initial_selected_subjects.get("order_id")
            if not self._valid_order_id(selected_order_id):
                raise ValueError("invalid initial selected subject")
            # This argument is intentionally accepted only from server-side
            # continuation validation.  Browser/LLM values never reach here.
            create_kwargs["selected_subjects"] = {"order_id": selected_order_id}
        case = await create_open_case(
            **create_kwargs,
        )
        if case is None:
            raise RuntimeError("support case creation failed")
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

    async def get_latest(
        self,
        *,
        session_id: str,
        customer_user_id: int,
    ) -> SupportCase | None:
        """读取最近 Case 供事实安全边界使用，不恢复或修改其状态。"""
        return await get_latest_case(
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

    async def supersede_subject_correction_description(self, case: SupportCase) -> SupportCase | None:
        """安全结束未完成的 correction 描述 pending。

        当 Router 已将本轮识别为其他输入（例如新业务请求、人工交接或结束对话）时，
        旧 correction frame 不能继续影响后续裸短语。旧 subject 已被客户明确否定，
        因此必须同时撤销它的 authoritative binding；旧 facts 只保留为 historical
        audit，不能被新请求当作当前事实。也不恢复任何旧的 financial command。
        """
        if case.pending.get("kind") != "subject_correction_description":
            return case
        old_order_id = case.selected_subjects.get("order_id")
        existing_contexts = case.verified_facts.get("_decision_contexts", [])
        historical_contexts = historicalize_decision_contexts(
            existing_contexts if isinstance(existing_contexts, list) else []
        )
        verified_facts = {
            key: value
            for key, value in case.verified_facts.items()
            if key not in {"_decision_contexts", SUBJECT_CONTEXT_RESET_MARKER} and key not in SUBJECT_BOUND_FACTS
        }
        if historical_contexts:
            verified_facts["_decision_contexts"] = historical_contexts
        verified_facts[SUBJECT_CONTEXT_RESET_MARKER] = {
            "from_subject_id": old_order_id if isinstance(old_order_id, str) else "",
            "reason": "customer_disputed_subject",
        }
        selected_subjects = {key: value for key, value in case.selected_subjects.items() if key != "order_id"}
        return await self._replace(
            case,
            status="ACTIVE",
            request_stack=[],
            selected_subjects=selected_subjects,
            verified_facts=verified_facts,
            pending={},
            pending_command={},
            event_type="CUSTOMER_RESPONSE",
            event_payload={
                "pending_kind": "subject_correction_description",
                "reason": "SUBJECT_CORRECTION_SUPERSEDED",
                "old_subject_id": old_order_id,
                "request_stack_retired": True,
            },
        )

    async def retire_customer_subject_for_change(self, case: SupportCase) -> SupportCase | None:
        """Retire stale subject state before semantic replacement.

        Once the customer says the object is different, the old order is no longer a
        current binding.  Keep the canonical Goal/request stack, but clear the displayed
        choice frame, current order selection and subject-bound transaction facts.  The
        previous order survives only as historical audit context plus a reset marker until
        a fresh authenticated subject is bound.

        This deliberately avoids a separate ``subject_correction`` Case lifecycle.  The
        next Workflow pass is an ordinary subject discovery step for the same business
        Goal.
        """
        if case.status not in {"ACTIVE", "AWAITING_CUSTOMER"}:
            return case
        old_pending_kind = str(case.pending.get("kind") or "")
        choices = case.pending.get("choices")
        choice_count = len(choices) if isinstance(choices, list) else 0
        old_order_id = case.selected_subjects.get("order_id")
        existing_contexts = case.verified_facts.get("_decision_contexts", [])
        historical_contexts = historicalize_decision_contexts(
            existing_contexts if isinstance(existing_contexts, list) else []
        )
        verified_facts = {
            key: value
            for key, value in case.verified_facts.items()
            if key not in {"_decision_contexts", SUBJECT_CONTEXT_RESET_MARKER} and key not in SUBJECT_BOUND_FACTS
        }
        if historical_contexts:
            verified_facts["_decision_contexts"] = historical_contexts
        if self._valid_order_id(old_order_id):
            verified_facts[SUBJECT_CONTEXT_RESET_MARKER] = {
                "from_subject_id": old_order_id,
                "reason": "customer_changed_subject",
            }
        selected_subjects = {key: value for key, value in case.selected_subjects.items() if key != "order_id"}
        return await self._replace(
            case,
            status="ACTIVE",
            selected_subjects=selected_subjects,
            verified_facts=verified_facts,
            pending={},
            pending_command={},
            event_type="CUSTOMER_RESPONSE",
            event_payload={
                "pending_kind": old_pending_kind,
                "reason": "SUBJECT_CHANGED",
                "old_subject_id": old_order_id if self._valid_order_id(old_order_id) else None,
                "choice_count": choice_count,
                "request_stack_preserved": True,
            },
        )

    async def select_customer_subject(
        self,
        case: SupportCase,
        *,
        subject: dict[str, Any],
        selection_source: str,
    ) -> SupportCase | None:
        """确定性保存客户选择并清除选择 pending，保持同一 Case 可继续执行。"""
        safe_subject = self._json_object(subject)
        order_id = safe_subject.get("order_id")
        if not isinstance(order_id, str) or not order_id.startswith("SO"):
            raise ValueError("customer subject requires a valid checkout order")
        selected_subjects = {**case.selected_subjects, "order_id": order_id}
        verified_facts = dict(case.verified_facts)
        verified_facts.pop(SUBJECT_CONTEXT_RESET_MARKER, None)
        return await self._replace(
            case,
            status="ACTIVE",
            selected_subjects=selected_subjects,
            verified_facts=verified_facts,
            pending={},
            pending_command={},
            # Reuse the schema-approved customer-response event.  The
            # selection details remain in the payload so no migration is
            # needed just to audit a resumable choice.
            event_type="CUSTOMER_RESPONSE",
            event_payload={
                "subject_type": "order",
                "selection_source": selection_source[:80],
                "selected_order_id": order_id,
                "pending_kind": "customer_choice",
                "selection_event": "subject_selected",
            },
        )

    async def prepare_subject_correction(
        self,
        case: SupportCase,
        *,
        choices: list[dict[str, Any]],
    ) -> SupportCase | None:
        """保存绑定订单纠正所展示的候选快照，并等待客户选择。

        当前绑定 subject 保留不变，直到客户从这组服务端候选中唯一选择一个。
        候选只保存订单选择所需的最小字段；实际切换仍由
        ``transition_customer_subject`` 或 ``select_customer_subject`` 完成。
        """
        current_order_id = case.selected_subjects.get("order_id")
        eligible_statuses = {"ACTIVE", "AWAITING_CUSTOMER", "COMPLETED", "FAILED", "CANCELLED"}
        if case.status not in eligible_statuses or not self._valid_order_id(current_order_id):
            return None
        safe_choices = self._safe_subject_choices(choices)
        if not safe_choices:
            return None
        pending = {
            "kind": "customer_choice",
            "subject_type": "order",
            "choices": safe_choices,
            "selection_event": "subject_correction",
            "transition_from_order_id": current_order_id,
        }
        target_case = case
        if case.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            # 终态 Case 不安全重开；在一个事务里创建仍绑定旧 subject、但已经带有
            # correction pending 的派生活动 Case。``require_new`` 防止并发时误把
            # 已有的另一活动 Case 当成这次纠正的载体。
            target_case = await create_open_case(
                session_id=case.session_id,
                customer_user_id=case.customer_user_id,
                request_stack=json.loads(json.dumps(case.request_stack, ensure_ascii=False)),
                selected_subjects={"order_id": current_order_id},
                initial_status="AWAITING_CUSTOMER",
                initial_pending=pending,
                event_payload={
                    "request_count": len(case.request_stack),
                    "selection_event": "subject_correction",
                    "derived_from_case_id": str(case.case_id),
                    "from_order_id": current_order_id,
                    "choice_count": len(safe_choices),
                },
                require_new=True,
                initial_event_type="AWAITING_CUSTOMER",
            )
            if target_case is None:
                return None
            if target_case.selected_subjects.get("order_id") != current_order_id:
                # A derived Case must remain bound to the old subject while its
                # correction choices are pending.  Do not trust a malformed or
                # concurrently returned Case as a transition target.
                return None
            if target_case.status != "AWAITING_CUSTOMER" or target_case.pending != pending:
                return None
            return target_case

        return await self._replace(
            target_case,
            status="AWAITING_CUSTOMER",
            pending=pending,
            pending_command={},
            event_type="AWAITING_CUSTOMER",
            event_payload={
                "pending_kind": "customer_choice",
                "selection_event": "subject_correction",
                "from_order_id": current_order_id,
                "choice_count": len(safe_choices),
            },
        )

    async def await_subject_correction_description(self, case: SupportCase) -> SupportCase | None:
        """保存“请继续描述纠正订单”的 pending，不重新路由下一条裸描述。

        对终态 Case 使用一次性派生创建；对活动 Case 使用现有乐观锁替换。旧 subject
        始终保持 authoritative，直到后续描述被当前用户订单候选唯一解析并完成 transition。
        """
        current_order_id = case.selected_subjects.get("order_id")
        if case.status not in {"ACTIVE", "AWAITING_CUSTOMER", "COMPLETED", "FAILED", "CANCELLED"}:
            return None
        if not self._valid_order_id(current_order_id):
            return None
        pending = {
            "kind": "subject_correction_description",
            "subject_type": "order",
            "selection_event": "subject_correction",
            "transition_from_order_id": current_order_id,
        }
        event_payload = {
            "pending_kind": pending["kind"],
            "selection_event": "subject_correction",
            "from_order_id": current_order_id,
        }
        if case.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return await create_open_case(
                session_id=case.session_id,
                customer_user_id=case.customer_user_id,
                request_stack=json.loads(json.dumps(case.request_stack, ensure_ascii=False)),
                selected_subjects={"order_id": current_order_id},
                initial_status="AWAITING_CUSTOMER",
                initial_pending=pending,
                event_payload={
                    **event_payload,
                    "derived_from_case_id": str(case.case_id),
                },
                require_new=True,
                initial_event_type="AWAITING_CUSTOMER",
            )
        return await self._replace(
            case,
            status="AWAITING_CUSTOMER",
            pending=pending,
            pending_command={},
            event_type="AWAITING_CUSTOMER",
            event_payload=event_payload,
        )

    async def transition_customer_subject(
        self,
        case: SupportCase,
        *,
        subject: dict[str, Any],
        selection_source: str,
    ) -> SupportCase | None:
        """在同一活动 Case 中审计并完成已验证的订单 subject 转换。

        API 层必须先用当前客户身份重新核验候选。该方法只接受已经由调用方
        解析出的 checkout order，并清除旧选择 pending；旧 subject 的事实上下文
        保留在 ``_decision_contexts`` 中，但所有旧 flat transaction facts 都会被
        移出当前 Case，避免新 subject 继承旧订单事实。
        """
        if case.status not in {"ACTIVE", "AWAITING_CUSTOMER"}:
            return None
        old_order_id = case.selected_subjects.get("order_id")
        safe_subject = self._json_object(subject)
        new_order_id = safe_subject.get("order_id")
        if not self._valid_order_id(old_order_id) or not self._valid_order_id(new_order_id):
            raise ValueError("subject transition requires bound checkout orders")
        if old_order_id == new_order_id:
            return None
        selected_subjects = {**case.selected_subjects, "order_id": new_order_id}
        existing_contexts = case.verified_facts.get("_decision_contexts", [])
        historical_contexts = historicalize_decision_contexts(
            existing_contexts if isinstance(existing_contexts, list) else []
        )
        verified_facts = {
            key: value
            for key, value in case.verified_facts.items()
            if key not in {"_decision_contexts", SUBJECT_CONTEXT_RESET_MARKER} and key not in SUBJECT_BOUND_FACTS
        }
        if historical_contexts:
            verified_facts["_decision_contexts"] = historical_contexts
        return await self._replace(
            case,
            status="ACTIVE",
            selected_subjects=selected_subjects,
            verified_facts=verified_facts,
            pending={},
            pending_command={},
            event_type="CUSTOMER_RESPONSE",
            event_payload={
                "subject_type": "order",
                "selection_source": selection_source[:80],
                "old_subject_id": old_order_id,
                "new_subject_id": new_order_id,
                "selection_event": "subject_corrected",
            },
        )

    async def open_subject_correction_case(
        self,
        case: SupportCase,
        *,
        subject: dict[str, Any],
        selection_source: str,
    ) -> SupportCase | None:
        """为不能安全重开的终态 Case 创建一个派生活动 Case。

        终态记录保持不可变；新 Case 继承原 canonical request stack，并只带新的、
        已验证的 order subject。``CASE_CREATED`` payload 记录来源 Case，作为内部
        审计信息，不进入客户会话或模型上下文。
        """
        if case.status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            return None
        safe_subject = self._json_object(subject)
        new_order_id = safe_subject.get("order_id")
        if not self._valid_order_id(new_order_id):
            raise ValueError("subject correction requires a valid checkout order")
        created = await create_open_case(
            session_id=case.session_id,
            customer_user_id=case.customer_user_id,
            request_stack=json.loads(json.dumps(case.request_stack, ensure_ascii=False)),
            selected_subjects={"order_id": new_order_id},
            event_payload={
                "request_count": len(case.request_stack),
                "selection_event": "subject_correction",
                "derived_from_case_id": str(case.case_id),
                "selection_source": selection_source[:80],
                "new_subject_id": new_order_id,
            },
            require_new=True,
        )
        if created is None:
            return None
        # A concurrent request may have created an unrelated active Case.  Never
        # attach this correction to it unless the requested subject was persisted.
        if created.selected_subjects.get("order_id") != new_order_id:
            return None
        return created

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

    async def supersede_for_new_request(self, case: SupportCase) -> SupportCase | None:
        """Close an automated task frame before a distinct customer task starts.

        Case history remains auditable, but a new request must not inherit this
        Case's goal stack or selected subject.  Real staff-owned Cases are
        deliberately left alone; their public lifecycle is independent from a
        new automated conversation task.
        """
        if case.status not in {"ACTIVE", "AWAITING_CUSTOMER"}:
            return case
        return await self._replace(
            case,
            status="CANCELLED",
            pending={},
            pending_command={},
            event_type="CASE_COMPLETED",
            event_payload={"reason": "TASK_SUPERSEDED_BY_NEW_REQUEST"},
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
        decision_contexts: list[dict[str, Any]] | None = None,
    ) -> SupportCase | None:
        """追加服务端已核验事实；客户自述不得写入该字段。"""
        existing_contexts = case.verified_facts.get("_decision_contexts", [])
        merged_contexts = merge_decision_contexts(
            historicalize_decision_contexts(existing_contexts if isinstance(existing_contexts, list) else []),
            decision_contexts or [],
        )
        reset_marker = case.verified_facts.get(SUBJECT_CONTEXT_RESET_MARKER)
        release_reset_barrier = self._has_trusted_new_subject(
            reset_marker,
            selected_subjects=selected_subjects if selected_subjects is not None else case.selected_subjects,
            decision_contexts=decision_contexts,
        )
        merged_facts = {
            key: value
            for key, value in case.verified_facts.items()
            if key != SUBJECT_CONTEXT_RESET_MARKER and key != "_decision_contexts" and key not in SUBJECT_BOUND_FACTS
        }
        merged_facts.update(
            key_value
            for key_value in self._json_object(facts).items()
            if key_value[0] != "_decision_contexts" and key_value[0] not in SUBJECT_BOUND_FACTS
        )
        if merged_contexts:
            merged_facts["_decision_contexts"] = merged_contexts
        if isinstance(reset_marker, dict) and not release_reset_barrier:
            merged_facts[SUBJECT_CONTEXT_RESET_MARKER] = self._json_object(reset_marker)
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
        """Mark a Case as staff-owned only after a real ticket was created."""
        safe_summary = self._json_object(handoff_summary)
        ticket_id = safe_summary.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise ValueError("AWAITING_STAFF requires a real ticket_id")
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
        prompt_facts = {
            key: value
            for key, value in case.verified_facts.items()
            if not key.startswith("_") and key not in SUBJECT_BOUND_FACTS
        }
        payload = {
            "case_status": case.status,
            "request_stack": case.request_stack[:3],
            "selected_subjects": case.selected_subjects,
            "verified_facts": prompt_facts,
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
        if status not in OPEN_CASE_STATUSES | STAFF_CASE_STATUSES | {"COMPLETED", "FAILED", "CANCELLED"}:
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

    @classmethod
    def _safe_subject_choices(cls, choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """裁剪订单候选，保持 pending 只包含 customer-safe 选择字段。"""
        safe: list[dict[str, Any]] = []
        for raw in choices[:MAX_ORDER_CHOICE_OPTIONS]:
            if not isinstance(raw, dict):
                continue
            order_id = raw.get("order_id") or raw.get("order_no")
            if not cls._valid_order_id(order_id):
                continue
            item: dict[str, Any] = {"order_id": order_id}
            product_name = raw.get("product_name")
            if isinstance(product_name, str) and product_name.strip():
                item["product_name"] = product_name.strip()[:160]
            amount_cents = raw.get("amount_cents")
            if isinstance(amount_cents, int) and not isinstance(amount_cents, bool) and amount_cents >= 0:
                item["amount_cents"] = amount_cents
            for key in ("catalog_category", "catalog_product_id", "component_category"):
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    item[key] = value.strip()[:160]
            raw_items = raw.get("items")
            if isinstance(raw_items, list):
                safe_items: list[dict[str, str]] = []
                for raw_item in raw_items[:10]:
                    if not isinstance(raw_item, dict):
                        continue
                    safe_item: dict[str, str] = {}
                    for key in (
                        "product_name",
                        "catalog_category",
                        "catalog_product_id",
                        "component_category",
                    ):
                        value = raw_item.get(key)
                        if isinstance(value, str) and value.strip():
                            safe_item[key] = value.strip()[:160]
                    if safe_item:
                        safe_items.append(safe_item)
                if safe_items:
                    item["items"] = safe_items
            safe.append(item)
        return safe

    @staticmethod
    def _valid_order_id(value: object) -> bool:
        return isinstance(value, str) and value.startswith("SO") and 1 < len(value) <= 128

    @staticmethod
    def _has_trusted_new_subject(
        reset_marker: object,
        *,
        selected_subjects: dict[str, Any],
        decision_contexts: list[dict[str, Any]] | None,
    ) -> bool:
        """Return whether a fresh, unique order context can release reset.

        Subjectless facts and facts for the disputed subject never release the
        barrier.  Explicit subject selection/transition has its own validated
        service path and removes the marker before this method is reached.
        """
        if not isinstance(reset_marker, dict):
            return False
        retired_subject = reset_marker.get("from_subject_id")
        current_subjects = {
            str(item.get("subject_id"))
            for item in merge_decision_contexts(decision_contexts)
            if item.get("provenance") == "current" and isinstance(item.get("subject_id"), str)
        }
        if len(current_subjects) != 1:
            return False
        current_subject = next(iter(current_subjects))
        if isinstance(retired_subject, str) and retired_subject and current_subject == retired_subject:
            return False
        selected_order_id = selected_subjects.get("order_id")
        return not selected_order_id or selected_order_id == current_subject
