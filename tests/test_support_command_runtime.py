"""Pure Phase 3 legality tests; intentionally no DB/FastAPI/LangGraph fixtures."""

from agent.support_command_contract import SupportCommand, parse_support_command_turn
from agent.support_command_runtime import SupportCaseSnapshot, plan_support_command


def _refund_choice_case(count: int = 3) -> SupportCaseSnapshot:
    return SupportCaseSnapshot(
        status="AWAITING_CUSTOMER",
        goal_domain="refund",
        goal_operation="request",
        pending_kind="customer_choice",
        pending_choice_count=count,
    )


def run_matrix() -> None:
    case = _refund_choice_case()

    # Subject replacement is a Case transition, not phrase parsing.
    for description in [
        "华为",
        "昨天买的华为，但不是 Pura X",
        "我前面说错的那台手机",
        "另一台 512GB 的手机",
    ]:
        plan = plan_support_command(
            SupportCommand("set_subject", subject_description=description),
            active_case=case,
        )
        assert plan.action == "set_subject_description"
        assert plan.subject_description == description
        assert plan.use_case_requests

    # Empty semantic subject is not enough to replace a subject.
    assert not plan_support_command(
        SupportCommand("set_subject"),
        active_case=case,
    ).handled

    # Current pending choice refs are bounded to the server-owned frame.
    assert (
        plan_support_command(
            SupportCommand("set_subject", candidate_ref="choice_1"),
            active_case=case,
        ).candidate_index
        == 0
    )
    assert (
        plan_support_command(
            SupportCommand("set_subject", candidate_ref="choice_3"),
            active_case=case,
        ).candidate_index
        == 2
    )
    for bad_ref in ["choice_0", "choice_4", "SO2026090405413392B0EB29D431", "order_candidate_1", ""]:
        assert not plan_support_command(
            SupportCommand("set_subject", candidate_ref=bad_ref),
            active_case=case,
        ).handled

    # A choice cannot be answered after the frame has gone away.
    active_no_pending = SupportCaseSnapshot(
        status="ACTIVE",
        goal_domain="refund",
        goal_operation="request",
        has_verified_subject=True,
    )
    assert not plan_support_command(
        SupportCommand("set_subject", candidate_ref="choice_1"),
        active_case=active_no_pending,
    ).handled

    # Read-only current-fact verification may continue a verified subject.
    completed = SupportCaseSnapshot(
        status="COMPLETED",
        goal_domain="refund",
        goal_operation="request",
        has_verified_subject=True,
    )
    for domain, operation in [("refund", "status"), ("order", "status")]:
        plan = plan_support_command(
            SupportCommand("start_goal", domain, operation, candidate_ref="current_subject"),
            recent_case=completed,
        )
        assert plan.action == "read_status"
        assert plan.reuse_verified_subject

    # A brand-new status goal must not silently inherit the previous Case subject.
    plan = plan_support_command(
        SupportCommand("start_goal", "refund", "status"),
        recent_case=completed,
    )
    assert plan.action == "read_status"
    assert not plan.reuse_verified_subject

    # A status query with a different active Goal is an interruption; Phase 3
    # does not cut it over yet, so the existing Case cannot be completed by it.
    active_refund_request = SupportCaseSnapshot(
        status="AWAITING_CUSTOMER",
        goal_domain="refund",
        goal_operation="request",
        pending_kind="customer_choice",
        pending_choice_count=3,
    )
    assert not plan_support_command(
        SupportCommand("start_goal", "refund", "status"),
        active_case=active_refund_request,
    ).handled

    # New identity description means rediscovery; previous subject is not reused.
    plan = plan_support_command(
        SupportCommand("start_goal", "refund", "status", "另一台华为"),
        recent_case=completed,
    )
    assert plan.action == "read_status"
    assert not plan.reuse_verified_subject

    # A command must not provide two competing subject sources.
    assert not plan_support_command(
        SupportCommand("set_subject", subject_description="华为", candidate_ref="choice_1"),
        active_case=case,
    ).handled
    assert not plan_support_command(
        SupportCommand("start_goal", "refund", "status", "华为", "current_subject"),
        recent_case=completed,
    ).handled

    # current_subject is an explicit safe reference, not a hint.  If the
    # server-side continuity frame cannot validate it, cutover must fail.
    invalid_recent = SupportCaseSnapshot(
        status="CANCELLED",
        goal_domain="refund",
        goal_operation="request",
        has_verified_subject=True,
    )
    assert not plan_support_command(
        SupportCommand("start_goal", "refund", "status", candidate_ref="current_subject"),
        recent_case=invalid_recent,
    ).handled

    # Unsupported business goals remain on legacy path in this cutover slice.
    for goal in [("payment", "status"), ("refund", "request"), ("delivery", "status")]:
        assert not plan_support_command(
            SupportCommand("start_goal", goal[0], goal[1]),
            recent_case=completed,
        ).handled

    # Terminal/invalid Case cannot accept subject replacement.
    for status in ["COMPLETED", "FAILED", "CANCELLED", "AWAITING_STAFF"]:
        terminal = SupportCaseSnapshot(
            status=status,
            goal_domain="refund",
            goal_operation="request",
            has_verified_subject=True,
        )
        assert not plan_support_command(
            SupportCommand("set_subject", subject_description="华为"),
            active_case=terminal,
        ).handled

    # A recently completed singular support Goal is conversational context,
    # not a mutable Case row.  Subject correction may therefore continue from
    # it; the API opens a fresh ordinary Case for the new turn.
    plan = plan_support_command(
        SupportCommand("set_subject", subject_description="华为"),
        recent_case=completed,
    )
    assert plan.action == "set_subject_description"
    assert plan.use_case_requests

    # Parser supports current-fact refund status without allowing real order ids as candidate refs.
    parsed = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "start_goal",
                    "domain": "refund",
                    "operation": "status",
                    "subject_description": "当前订单",
                    "candidate_ref": "SO2026090405413392B0EB29D431",
                    "confidence": 0.99,
                }
            ],
        }
    )
    assert parsed.commands[0].goal == "refund.status"
    assert parsed.commands[0].candidate_ref == ""

    current = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {"type": "start_goal", "domain": "refund", "operation": "status", "candidate_ref": "current_subject"}
            ],
        }
    )
    assert current.commands[0].candidate_ref == "current_subject"


def test_support_command_runtime_matrix() -> None:
    run_matrix()


if __name__ == "__main__":
    run_matrix()
    print("support_command_runtime matrix PASS")
