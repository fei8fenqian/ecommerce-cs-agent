"""用当前评分口径重算已生成的客服 Trace，不重新调用 LLM。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evaluation.support_trace import aggregate_scores, score_gold_against_trace


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} 顶层必须是 JSON object")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} 必须是 JSON object")
        records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    gold = _load_jsonl(args.gold)
    result = _load_json(args.trace)
    traces = result.get("traces")
    old_scores = result.get("scores")
    if not isinstance(traces, list) or not isinstance(old_scores, list) or len(traces) != len(old_scores):
        raise ValueError("trace 文件缺少等长的 traces/scores")
    if len(gold) != len(traces):
        raise ValueError(f"gold={len(gold)} 与 traces={len(traces)} 数量不一致")

    gold_by_id = {str(record.get("id")): record for record in gold}
    scores: list[dict[str, Any]] = []
    for trace, old_score in zip(traces, old_scores):
        if not isinstance(trace, dict) or not isinstance(old_score, dict):
            raise ValueError("traces/scores 中存在非 object")
        case_id = str(trace.get("case_id") or "")
        record = gold_by_id.get(case_id)
        if record is None:
            raise ValueError(f"trace 找不到对应 Gold: {case_id}")
        scores.append(
            score_gold_against_trace(
                record,
                trace,
                goal_judgment=old_score.get("goal_accuracy"),
            )
        )

    result["summary"] = aggregate_scores(scores)
    result["scores"] = scores
    result["scoring_revision"] = "capability-normalized-order-deduplicated-v1"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Rescored {len(scores)} traces: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
