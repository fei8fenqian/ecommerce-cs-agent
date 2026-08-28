"""Canonical Goal taxonomy must stay aligned across Router, Gold, and Control Plane."""

import json
from pathlib import Path

from agent.goal_taxonomy import GOAL_DEFINITIONS, get_goal_definition, is_canonical_goal, workflow_key_for_goal
from agent.support_control import resolve_workflow

ROOT = Path(__file__).resolve().parents[1]
REFUND_GOLD = ROOT / "data/benchmarks/jddc/refund-gold-100.jsonl"


def test_refund_status_family_has_distinct_canonical_goals_and_workflows():
    expected = {
        "refund.status": "refund.refund_status",
        "refund.expected_arrival": "refund.expected_arrival",
        "refund.processing_time": "refund.processing_time",
        "refund.anomaly": "refund.anomaly",
        "return.refund_dependency": "return.refund_dependency",
        "refund.delivery_after_refund": None,
    }

    for key, workflow_key in expected.items():
        domain, operation = key.split(".", 1)
        definition = get_goal_definition(domain, operation)
        assert definition is not None
        assert definition.workflow_key == workflow_key


def test_taxonomy_rejects_illegal_cross_domain_refund_pairs():
    assert is_canonical_goal("after_sales", "refund_status") is False
    assert is_canonical_goal("after_sales", "refund_request") is False
    assert is_canonical_goal("refund", "status") is True


def test_every_taxonomy_workflow_key_resolves_to_that_exact_workflow():
    for definition in GOAL_DEFINITIONS:
        resolved = resolve_workflow({"domain": definition.domain, "operation": definition.operation})
        assert (resolved.key if resolved else None) == definition.workflow_key


def test_frozen_refund_gold_uses_taxonomy_and_real_workflow_contracts():
    records = [json.loads(line) for line in REFUND_GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(records) == 100

    for record in records:
        requests = record["expected_requests"]
        if not requests:
            assert record["primary_goal"] is None
            assert record["expected_workflow"] is None
            continue

        first = requests[0]
        assert is_canonical_goal(first["domain"], first["operation"])
        assert record["primary_goal"] == f"{first['domain']}.{first['operation']}"
        assert record["expected_workflow"] == workflow_key_for_goal(first["domain"], first["operation"])
