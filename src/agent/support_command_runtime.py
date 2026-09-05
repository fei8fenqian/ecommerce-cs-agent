"""Deterministic legality planning for SupportCommand cutover.

This module deliberately contains no natural-language parsing.  It consumes a
bounded LLM command plus server-owned Case metadata and decides only whether the
command is structurally/legalistically eligible for the Phase 3 migration
slice.  API/Service code still performs ownership and optimistic-lock checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.support_command_contract import SupportCommand


@dataclass(frozen=True)
class SupportCaseSnapshot:
    status: str = ""
    goal_domain: str = ""
    goal_operation: str = ""
    pending_kind: str = ""
    pending_choice_count: int = 0
    has_verified_subject: bool = False


@dataclass(frozen=True)
class SupportCommandPlan:
    action: str = "fallback"
    domain: str = ""
    operation: str = ""
    subject_description: str = ""
    candidate_index: int = -1
    use_case_requests: bool = False
    reuse_verified_subject: bool = False

    @property
    def handled(self) -> bool:
        return self.action != "fallback"


def _same_goal(command: SupportCommand, case: SupportCaseSnapshot | None) -> bool:
    return bool(
        case
        and command.domain
        and command.operation
        and command.domain == case.goal_domain
        and command.operation == case.goal_operation
    )


def plan_support_command(
    command: SupportCommand | None,
    *,
    active_case: SupportCaseSnapshot | None = None,
    recent_case: SupportCaseSnapshot | None = None,
) -> SupportCommandPlan:
    """Plan only the bounded Phase 3 coexistence slice.

    The semantic layer supplies high-level commands; Runtime uses Case state to
    determine what a subject assignment means.  It never inspects customer text.
    Unsupported/multi-goal semantics stay on the legacy coexistence path.
    """
    if command is None:
        return SupportCommandPlan()

    if command.type == "set_subject":
        # ``set_subject`` is semantic slot replacement, not a mutation of the
        # previous Case row.  A currently open Case is the primary continuity
        # source; when the previous refund/order task already completed, its
        # canonical Goal may still provide the immediate conversational frame.
        # The API will open a fresh ordinary Case for the new turn instead of
        # reopening/deriving from a terminal record.
        continuity = active_case or recent_case
        if continuity is None:
            return SupportCommandPlan()
        if active_case is not None:
            if active_case.status not in {"ACTIVE", "AWAITING_CUSTOMER"}:
                return SupportCommandPlan()
        else:
            if recent_case is None or recent_case.status != "COMPLETED":
                return SupportCommandPlan()
            if (recent_case.goal_domain, recent_case.goal_operation) not in {
                ("order", "status"),
                ("refund", "request"),
                ("refund", "status"),
            }:
                return SupportCommandPlan()
        if command.candidate_ref and command.subject_description.strip():
            return SupportCommandPlan()

        if command.candidate_ref.startswith("choice_"):
            # Choice refs are valid only inside the currently displayed,
            # server-owned pending frame.  A completed Case can provide Goal
            # continuity, never a stale selector frame.
            if active_case is None:
                return SupportCommandPlan()
            if active_case.status != "AWAITING_CUSTOMER" or active_case.pending_kind != "customer_choice":
                return SupportCommandPlan()
            try:
                candidate_index = int(command.candidate_ref.split("_", 1)[1]) - 1
            except (ValueError, IndexError):
                return SupportCommandPlan()
            if candidate_index < 0 or candidate_index >= active_case.pending_choice_count:
                return SupportCommandPlan()
            return SupportCommandPlan(
                action="set_subject_choice",
                candidate_index=candidate_index,
                use_case_requests=True,
            )

        subject_description = command.subject_description.strip()
        if subject_description:
            return SupportCommandPlan(
                action="set_subject_description",
                subject_description=subject_description,
                use_case_requests=True,
            )
        return SupportCommandPlan()

    if command.type == "start_goal" and (command.domain, command.operation) in {
        ("order", "status"),
        ("refund", "status"),
    }:
        # A new read-only status flow must not implicitly complete or mutate a
        # different active business Case.  Interruption/stack semantics are a
        # later migration slice.
        if active_case is not None and not _same_goal(command, active_case):
            return SupportCommandPlan()

        continuity = active_case or recent_case
        if command.candidate_ref and command.subject_description.strip():
            return SupportCommandPlan()
        if command.candidate_ref and command.candidate_ref != "current_subject":
            return SupportCommandPlan()
        reuse_verified_subject = False
        if command.candidate_ref == "current_subject":
            if (
                continuity is None
                or not continuity.has_verified_subject
                or continuity.status not in {"ACTIVE", "AWAITING_CUSTOMER", "COMPLETED"}
            ):
                return SupportCommandPlan()
            reuse_verified_subject = True

        return SupportCommandPlan(
            action="read_status",
            domain=command.domain,
            operation=command.operation,
            subject_description=command.subject_description.strip(),
            reuse_verified_subject=reuse_verified_subject,
        )

    return SupportCommandPlan()
