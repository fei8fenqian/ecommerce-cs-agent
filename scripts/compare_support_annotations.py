"""比较两份客户支持动作标注，输出可复核的代理一致性指标。

这个脚本只比较已经独立产生的动作标注，不生成金标，也不把一致率称为准确率。
典型用法是把规则初标与盲审/第二个模型的标注按 ``id`` 对齐：

    PYTHONPATH=src .venv/bin/python scripts/compare_support_annotations.py \
        --left /tmp/jddc-3c-provisional.jsonl \
        --right /tmp/jddc-3c-independent-agent-annotations.jsonl \
        --left-field provisional_action \
        --right-field human_gold_action \
        --right-name independent_agent

只有两边都存在动作的记录才进入比较；缺失、跳过和未审记录会单独报告。
``--left-field provisional_action`` 是机器初标，不能当作人工金标。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from service.customer_support_policy import CustomerSupportAction

_VALID_ACTIONS = {action.value for action in CustomerSupportAction}


def _load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON") from exc
            if not isinstance(record, dict) or not str(record.get("id") or "").strip():
                raise ValueError(f"{path}:{line_number} 缺少 id")
            record_id = str(record["id"])
            if record_id in records:
                raise ValueError(f"{path}:{line_number} 重复 id={record_id}")
            records[record_id] = record
    return records


def _action(record: dict[str, Any], field: str, *, source: str) -> str | None:
    value = record.get(field)
    if value is None or str(value).strip() == "":
        return None
    action = str(value).strip()
    if action not in _VALID_ACTIONS:
        raise ValueError(f"{source} id={record.get('id')} 的 {field} 不是有效动作: {action}")
    return action


def compare(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    *,
    left_field: str,
    right_field: str,
    left_name: str,
    right_name: str,
    max_examples: int,
) -> dict[str, Any]:
    common_ids = sorted(left.keys() & right.keys())
    missing_left = sorted(right.keys() - left.keys())
    missing_right = sorted(left.keys() - right.keys())
    matrix: Counter[tuple[str, str]] = Counter()
    disagreements: list[dict[str, Any]] = []
    comparable = 0
    agreement = 0
    left_missing_action = 0
    right_missing_action = 0

    for record_id in common_ids:
        left_action = _action(left[record_id], left_field, source=left_name)
        right_action = _action(right[record_id], right_field, source=right_name)
        if left_action is None:
            left_missing_action += 1
        if right_action is None:
            right_missing_action += 1
        if left_action is None or right_action is None:
            continue
        comparable += 1
        matrix[(left_action, right_action)] += 1
        if left_action == right_action:
            agreement += 1
            continue
        if len(disagreements) < max_examples:
            disagreements.append(
                {
                    "id": record_id,
                    "left_action": left_action,
                    "right_action": right_action,
                    "query": str(right[record_id].get("query") or left[record_id].get("query") or ""),
                    "right_reason": right[record_id].get("independent_reason"),
                    "left_reason": left[record_id].get("provisional_reason"),
                }
            )

    actions = sorted(_VALID_ACTIONS)
    confusion = {
        left_action: {right_action: matrix[(left_action, right_action)] for right_action in actions}
        for left_action in actions
    }
    return {
        "evaluation_kind": "annotation_agreement",
        "metric_name": "proxy_agreement",
        "left": {"name": left_name, "records": len(left), "field": left_field},
        "right": {"name": right_name, "records": len(right), "field": right_field},
        "common_ids": len(common_ids),
        "comparable": comparable,
        "agreement_count": agreement,
        "proxy_agreement": agreement / comparable if comparable else None,
        "left_missing_action": left_missing_action,
        "right_missing_action": right_missing_action,
        "missing_from_left": len(missing_left),
        "missing_from_right": len(missing_right),
        "confusion_matrix": confusion,
        "disagreements": disagreements,
        "disagreement_count": comparable - agreement,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--left", type=Path, required=True, help="左侧 JSONL")
    parser.add_argument("--right", type=Path, required=True, help="右侧 JSONL")
    parser.add_argument("--left-field", default="provisional_action", help="左侧动作字段")
    parser.add_argument("--right-field", default="human_gold_action", help="右侧动作字段")
    parser.add_argument("--left-name", default="left", help="左侧标注来源名称")
    parser.add_argument("--right-name", default="right", help="右侧标注来源名称")
    parser.add_argument("--max-examples", type=int, default=50, help="最多输出多少条分歧原话")
    parser.add_argument("--json-out", type=Path, help="可选 JSON 输出路径")
    args = parser.parse_args()
    if args.max_examples < 0:
        parser.error("--max-examples must be non-negative")
    for path in (args.left, args.right):
        if not path.is_file():
            parser.error(f"input does not exist: {path}")
    try:
        left = _load_jsonl(args.left)
        right = _load_jsonl(args.right)
        result = compare(
            left,
            right,
            left_field=args.left_field,
            right_field=args.right_field,
            left_name=args.left_name,
            right_name=args.right_name,
            max_examples=args.max_examples,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
