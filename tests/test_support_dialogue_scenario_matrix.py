import json
from pathlib import Path

from agent.support_command_contract import SupportCommand
from agent.support_command_runtime import SupportCaseSnapshot, plan_support_command

CASES = {
    "none": None,
    "refund_choice": SupportCaseSnapshot("AWAITING_CUSTOMER", "refund", "request", "customer_choice", 3, False),
    "refund_choice_2": SupportCaseSnapshot("AWAITING_CUSTOMER", "refund", "request", "customer_choice", 2, False),
    "active_refund_bound": SupportCaseSnapshot("ACTIVE", "refund", "request", "", 0, True),
    "active_order_bound": SupportCaseSnapshot("ACTIVE", "order", "status", "", 0, True),
    "completed_refund": SupportCaseSnapshot("COMPLETED", "refund", "request", "", 0, True),
    "awaiting_staff_refund": SupportCaseSnapshot("AWAITING_STAFF", "refund", "request", "", 0, True),
    "recent_completed_refund": SupportCaseSnapshot("COMPLETED", "refund", "request", "", 0, True),
    "recent_completed_order": SupportCaseSnapshot("COMPLETED", "order", "status", "", 0, True),
    "cancelled_order": SupportCaseSnapshot("CANCELLED", "order", "status", "", 0, True),
}


def run_matrix():
    data = json.loads((Path(__file__).parent / "support_dialogue_scenarios.json").read_text())
    assert len(data) >= 20
    for row in data:
        raw = row["command"]
        command = SupportCommand(
            type=raw.get("type", ""),
            domain=raw.get("domain", ""),
            operation=raw.get("operation", ""),
            subject_description=raw.get("subject_description", ""),
            candidate_ref=raw.get("candidate_ref", ""),
        )
        case = CASES[row["case"]]
        recent = case if row["case"].startswith("recent_") or row["case"] == "cancelled_order" else None
        active = None if recent is not None else case
        plan = plan_support_command(command, active_case=active, recent_case=recent)
        assert plan.action == row["expected"], (row["id"], row["utterance"], plan)
        if "reuse_verified_subject" in row:
            assert plan.reuse_verified_subject is row["reuse_verified_subject"], row["id"]


def test_support_dialogue_scenario_matrix() -> None:
    run_matrix()


if __name__ == "__main__":
    run_matrix()
    print("support dialogue scenario matrix PASS")
