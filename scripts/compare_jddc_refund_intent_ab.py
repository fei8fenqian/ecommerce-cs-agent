"""比较同一批 JDDC 退款 Intent A/B 结果，并计算 Pre-RAG Help/Harm。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("evaluation_kind") != "jddc_refund_intent_router_ab":
        raise ValueError(f"不是退款 Intent A/B 结果: {path}")
    return payload


def _help_harm(off_rows: dict[str, Any], on_rows: dict[str, Any], ids: list[str]) -> dict[str, Any]:
    help_cases = [case_id for case_id in ids if not off_rows[case_id]["goal_match"] and on_rows[case_id]["goal_match"]]
    harm_cases = [case_id for case_id in ids if off_rows[case_id]["goal_match"] and not on_rows[case_id]["goal_match"]]
    return {
        "comparable_cases": len(ids),
        "help": len(help_cases),
        "harm": len(harm_cases),
        "help_case_ids": help_cases,
        "harm_case_ids": harm_cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--off", type=Path, required=True)
    parser.add_argument("--on", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    off = _load(args.off)
    on = _load(args.on)
    if off.get("gold_sha256") != on.get("gold_sha256"):
        raise ValueError("OFF/ON 使用的 Gold SHA256 不一致")
    if off.get("model") != on.get("model"):
        raise ValueError("OFF/ON 使用的模型不一致")
    off_rows = {str(row["case_id"]): row for row in off["results"]}
    on_rows = {str(row["case_id"]): row for row in on["results"]}
    common_ids = sorted(set(off_rows) & set(on_rows))
    common_llm_ids = [
        case_id
        for case_id in common_ids
        if off_rows[case_id].get("route_source") == "llm" and on_rows[case_id].get("route_source") == "llm"
    ]
    result = {
        "evaluation_kind": "jddc_refund_intent_router_ab_comparison",
        "model": off.get("model"),
        "gold_sha256": off.get("gold_sha256"),
        "pre_rag_threshold": on.get("pre_rag_similarity_threshold"),
        "all_cases": _help_harm(off_rows, on_rows, common_ids),
        "llm_route_common_cases": _help_harm(off_rows, on_rows, common_llm_ids),
        "off_llm_route_cases": sum(row.get("route_source") == "llm" for row in off_rows.values()),
        "on_llm_route_cases": sum(row.get("route_source") == "llm" for row in on_rows.values()),
        "definition": "Help/Harm 比较 goal_match；Gold expected_requests=[] 时，goal_match 表示没有错误生成业务请求。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
