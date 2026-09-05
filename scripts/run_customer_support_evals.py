"""运行无需 LLM、数据库或网络的客户售后动作回归案例。"""

import json
import sys
from pathlib import Path

from service.customer_support_policy import (
    CustomerSupportAction,
    decide_customer_support_action,
    refund_self_service_answer,
)

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "evals" / "customer_support_cases.jsonl"
ORACLE_PATH = ROOT / "evals" / "customer_support_oracle.jsonl"


def main() -> int:
    failures: list[str] = []
    total = 0
    inputs = [json.loads(raw) for raw in INPUT_PATH.read_text(encoding="utf-8").splitlines() if raw.strip()]
    oracle = {
        str(item["id"]): str(item["expected_action"])
        for item in (json.loads(raw) for raw in ORACLE_PATH.read_text(encoding="utf-8").splitlines() if raw.strip())
    }
    input_ids = {str(item["id"]) for item in inputs}
    oracle_ids = set(oracle)
    missing_labels = input_ids - oracle_ids
    orphan_labels = oracle_ids - input_ids
    if missing_labels or orphan_labels:
        print(f"Eval corpus mismatch: missing_labels={len(missing_labels)}, orphan_labels={len(orphan_labels)}")
        return 1

    for line_number, case in enumerate(inputs, start=1):
        if "expected_action" in case:
            print(f"Blind input contains an answer at line {line_number}; keep labels in the oracle file")
            return 1
        history = (
            [{"role": "assistant", "content": refund_self_service_answer()}]
            if case.get("refund_guidance_was_shown")
            else []
        )
        decision = decide_customer_support_action(
            intent_target=str(case["proposed_target"]),
            role=str(case["role"]),
            query=str(case["query"]),
            history=history,
        )
        expected = CustomerSupportAction(oracle[str(case["id"])])
        total += 1
        if decision.action != expected:
            failures.append(
                f"{case.get('id', line_number)}: expected {expected.value}, "
                f"got {decision.action.value} ({decision.reason})"
            )

    if failures:
        print("Customer-support eval failures:")
        print("\n".join(failures))
        return 1
    print(f"Customer-support eval passed: {total} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
