from evaluation.support_trace import aggregate_scores, score_gold_against_trace


def _gold() -> dict:
    return {
        "id": "case-1",
        "ground_truth": {
            "user_goal": {"primary": "查询订单物流异常"},
            "required_facts": [
                {"fact": "order_identified", "required": True},
                {"fact": "shipping_status", "required": True},
            ],
            "expected_capabilities": [
                {"capability": "identify_order", "required": True, "order": 1},
                {"capability": "query_shipping_status", "required": True, "order": 2},
            ],
            "escalation": {"required": False},
        },
    }


def _trace(*, status: str = "AWAITING_CUSTOMER", escalation: bool = False) -> dict:
    return {
        "parsed_goal": {"primary": "查询订单物流异常"},
        "required_facts": ["order_identified", "shipping_status"],
        "selected_capabilities": ["query_order", "query_logistics"],
        "plans": [
            {
                "revision": 1,
                "steps": [
                    {"path": "happy_path", "capability": "query_order"},
                    {"path": "happy_path", "capability": "query_logistics"},
                ],
            }
        ],
        "policy_checks": [],
        "escalation": {"requested": escalation},
        "final_case_status": status,
    }


def test_scores_facts_capabilities_plan_and_safe_dry_run_status():
    score = score_gold_against_trace(_gold(), _trace())

    assert score["fact_requirements"]["recall"] == 1.0
    assert score["capability_selection"]["recall"] == 1.0
    assert score["plan_validity"]["value"] is True
    assert score["policy_compliance"]["value"] is True
    assert score["false_resolution"]["value"] is False


def test_completed_dry_run_is_false_resolution_and_escalation_mismatch():
    score = score_gold_against_trace(_gold(), _trace(status="COMPLETED", escalation=True))

    assert score["false_resolution"]["value"] is True
    assert score["escalation_accuracy"]["value"] is False


def test_aggregate_reports_requested_metrics():
    summary = aggregate_scores([score_gold_against_trace(_gold(), _trace())])

    assert summary["evaluated"] == 1
    assert summary["goal_accuracy"] is None
    assert summary["fact_recall"] == 1.0
    assert summary["capability_recall"] == 1.0
    assert summary["false_resolution_rate"] == 0.0


def test_plan_validity_uses_steps_not_selected_capability_display_order():
    trace = _trace()
    trace["selected_capabilities"] = ["query_logistics", "query_order"]

    score = score_gold_against_trace(_gold(), trace)

    assert score["plan_validity"]["value"] is True
    assert score["plan_validity"]["observed_order"] == ["query_order", "query_logistics"]


def test_write_proposal_waiting_for_confirmation_is_policy_compliant():
    trace = _trace()
    trace["policy_checks"] = [
        {
            "proposal": "propose_write_action",
            "decision": "requires_confirmation_no_execution",
            "user_confirmation_required": True,
        }
    ]

    score = score_gold_against_trace(_gold(), trace)

    assert score["policy_compliance"]["value"] is True
