"""用已保存的 Intent predictions 对指定 Gold 做离线重评分，不调用模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

SPEECH_ACTS = (
    "INFORMATION_QUERY",
    "ACTION_REQUEST",
    "STATEMENT",
    "CLARIFICATION_NEEDED",
    "FUTURE_INTENTION",
    "ACKNOWLEDGEMENT",
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(records) != 100:
        raise ValueError(f"Gold 必须包含 100 条，实际为 {len(records)} 条")
    return records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gold_requests(record: dict[str, Any]) -> list[dict[str, str]]:
    return [{"domain": item["domain"], "operation": item["operation"]} for item in record.get("expected_requests", [])]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", "--gold-path", dest="gold", type=Path, required=True)
    parser.add_argument("--source-result", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    gold = {record["case_id"]: record for record in _load_jsonl(args.gold)}
    source = json.loads(args.source_result.read_text(encoding="utf-8"))
    source_rows = {row["case_id"]: row for row in source["results"]}
    if set(gold) != set(source_rows):
        raise ValueError("Gold 与 source result 的 case_id 集合不一致")

    rows: list[dict[str, Any]] = []
    for case_id, record in gold.items():
        source_row = source_rows[case_id]
        prediction = source_row["prediction"]
        predicted_requests = prediction["requests"] if prediction else []
        expected_requests = _gold_requests(record)
        predicted_speech_act = prediction["speech_act"] if prediction else ""
        prediction_workflow = prediction["resolved_workflow"] if prediction else None
        expected_workflow = record.get("expected_workflow")
        error = source_row.get("error", "")
        goal_match = (
            bool(predicted_requests) and predicted_requests[0] == expected_requests[0]
            if expected_requests
            else not predicted_requests
        )
        rows.append(
            {
                **source_row,
                "gold": {
                    "speech_act": record["speech_act"],
                    "expected_requests": expected_requests,
                    "primary_goal": record.get("primary_goal"),
                    "expected_workflow": expected_workflow,
                    "needs_clarification": bool(record.get("needs_clarification")),
                    "notes": record.get("notes", ""),
                },
                "speech_act_match": bool(not error and predicted_speech_act == record["speech_act"]),
                "goal_match": bool(not error and goal_match),
                "goal_sequence_match": bool(not error and predicted_requests == expected_requests),
                "workflow_route_match": bool(
                    not error and expected_workflow is not None and prediction_workflow == expected_workflow
                ),
                "clarification_match": bool(
                    not error
                    and ((predicted_speech_act == "CLARIFICATION_NEEDED") == bool(record.get("needs_clarification")))
                ),
            }
        )

    evaluated = [row for row in rows if not row.get("error")]
    goal_rows = [row for row in evaluated if row["gold"]["expected_requests"]]
    no_request_rows = [row for row in evaluated if not row["gold"]["expected_requests"]]
    predicted_business_rows = [row for row in evaluated if row["prediction"] and row["prediction"]["requests"]]
    detected_business_rows = [row for row in goal_rows if row["prediction"] and row["prediction"]["requests"]]
    workflow_rows = [row for row in evaluated if row["gold"]["expected_workflow"] is not None]
    speech_recall = {}
    for act in SPEECH_ACTS:
        subset = [row for row in evaluated if row["gold"]["speech_act"] == act]
        speech_recall[act] = {"correct": sum(row["speech_act_match"] for row in subset), "total": len(subset)}
        speech_recall[act]["recall"] = speech_recall[act]["correct"] / len(subset) if subset else None
    summary = {
        "total": len(rows),
        "evaluated": len(evaluated),
        "errors": len(rows) - len(evaluated),
        "speech_act_accuracy": sum(row["speech_act_match"] for row in evaluated) / len(evaluated),
        "primary_goal_accuracy": sum(row["goal_match"] for row in goal_rows) / len(goal_rows),
        "workflow_route_accuracy": sum(row["workflow_route_match"] for row in workflow_rows) / len(workflow_rows),
        "clarification_accuracy": sum(row["clarification_match"] for row in evaluated) / len(evaluated),
        "speech_act_recall": speech_recall,
        "no_business_request_accuracy": sum(row["goal_match"] for row in no_request_rows) / len(no_request_rows),
        "no_business_request_cases": len(no_request_rows),
        "business_request_detection": {
            "precision": len(detected_business_rows) / len(predicted_business_rows),
            "recall": len(detected_business_rows) / len(goal_rows),
            "false_business_activation": sum(
                bool(row["prediction"] and row["prediction"]["requests"]) for row in no_request_rows
            ),
            "missed_business_request": len(goal_rows) - len(detected_business_rows),
        },
        "action_request": {
            "precision": (
                sum(
                    row["speech_act_match"] and row["prediction"]["speech_act"] == "ACTION_REQUEST" for row in evaluated
                )
                / sum(row["prediction"]["speech_act"] == "ACTION_REQUEST" for row in evaluated)
            ),
            "recall": speech_recall["ACTION_REQUEST"]["recall"],
        },
        "goal_scored_cases": len(goal_rows),
        "workflow_scored_cases": len(workflow_rows),
        "request_sequence_accuracy": sum(row["goal_sequence_match"] for row in goal_rows) / len(goal_rows),
        "request_free_accuracy": sum(row["goal_match"] for row in no_request_rows) / len(no_request_rows),
        "route_source_distribution": dict(Counter(row["route_source"] for row in rows)),
        "conditional_goal_accuracy_by_route_source": {
            source: {
                "correct": sum(row["goal_match"] for row in detected_business_rows if row["route_source"] == source),
                "total": sum(row["route_source"] == source for row in detected_business_rows),
            }
            for source in sorted({row["route_source"] for row in detected_business_rows})
        },
    }
    for values in summary["conditional_goal_accuracy_by_route_source"].values():
        values["accuracy"] = values["correct"] / values["total"] if values["total"] else None
    result = {
        **source,
        "model_rerun": False,
        "source_result": str(args.source_result),
        "gold_path": str(args.gold),
        "gold_sha256": _sha256(args.gold),
        "summary": summary,
        "results": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
