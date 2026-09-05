"""导出不含机器答案的 JDDC Ground Truth 人工标注包。

示例：
    PYTHONPATH=src .venv/bin/python scripts/export_jddc_ground_truth_annotation_pack.py \
      --source /tmp/jddc-3c-context-30.jsonl \
      --trace /tmp/jddc-context-30-replayed.json \
      --markdown-out /tmp/jddc-ground-truth.md \
      --jsonl-out /tmp/jddc-ground-truth.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _blank_annotation() -> dict[str, Any]:
    return {
        "user_goal": {"primary": "", "preferred_resolution": ""},
        "entities": [],
        "required_facts": [],
        "expected_capabilities": [],
        "valid_solution_paths": [],
        "invalid_solution_paths": [],
        "business_constraints": [],
        "success_criteria": [],
        "completion_type": "",
        "escalation": {"required": None, "allowed_if": [], "forbidden_if": []},
        "expected_outcome": {"status": "", "description": ""},
        "expected_response_notes": "",
    }


def _template_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record["id"]),
        "schema_version": "agent_eval_ground_truth.v2",
        "input": {
            "history": [
                {"role": str(message["role"]), "content": str(message["content"])}
                for message in record.get("history") or []
            ],
            "query": str(record["query"]),
        },
        "ground_truth": _blank_annotation(),
        "annotation_status": "pending",
        "annotator": "",
        "reviewed_at": "",
    }


def _render_case(record: dict[str, Any], ordinal: int) -> str:
    lines = [f"## {ordinal}. {record['id']}", ""]
    history = record.get("history") or []
    if history:
        lines.extend(["**可见上下文**", ""])
        for message in history:
            role = "用户" if message.get("role") == "user" else "客服"
            lines.append(f"> **{role}：** {str(message.get('content') or '').replace(chr(10), ' ')}")
        lines.append("")
    lines.extend(
        [
            f"**当前用户：** {record['query']}",
            "",
            "### Ground Truth（必填）",
            "",
            "- 用户目标：",
            "- 用户偏好的解决方式（若有）：",
            "- 相关实体（订单 / 商品 / 售后单 / 物流单等）：",
            "- 解决此问题前必须确认的事实（使用稳定 fact 名，例如 `current_invoice_type`）：",
            "- 所需能力及顺序（能力名，不写具体工具实现，例如 `query_invoice_status`）：",
            "- 完成判据（成功必须同时满足什么）：",
            "- completion type（`RESOLVED` / `RESOLVE_OR_EXPLAIN_NEXT_STEP` / "
            "`AWAITING_CUSTOMER` / `ESCALATION_REQUIRED`）：",
            "- 预期最终状态和说明：",
            "- 转人工：是否必需；允许条件；禁止条件：",
            "",
            "### 合法执行路径",
            "",
            "```text",
            "precondition -> action -> expected_result -> success_branch / failure_branch",
            "",
            "```",
            "",
            "### 不能接受的路径或承诺",
            "",
            "- ",
            "",
            "### 最终回复要求 / 备注",
            "",
            "- ",
            "",
            "---",
            "",
        ]
    )
    return "\n".join(lines)


def _select_records(
    source: list[dict[str, Any]],
    trace_path: Path | None,
    limit: int,
    *,
    min_history_turns: int,
    stratify_topics: bool,
    seed: int,
) -> list[dict[str, Any]]:
    if trace_path is None:
        candidates = [record for record in source if len(record.get("history") or []) >= min_history_turns]
        if not stratify_topics:
            return candidates[:limit] if limit else candidates
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in candidates:
            topics = record.get("topics") or ["other_3c"]
            groups[str(topics[0])].append(record)
        rng = random.Random(seed)
        topic_names = sorted(groups)
        for topic_name in topic_names:
            rng.shuffle(groups[topic_name])
        selected: list[dict[str, Any]] = []
        while topic_names and (not limit or len(selected) < limit):
            next_topics: list[str] = []
            for topic_name in topic_names:
                if not limit or len(selected) < limit:
                    selected.append(groups[topic_name].pop())
                if groups[topic_name]:
                    next_topics.append(topic_name)
            topic_names = next_topics
        return selected
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    selected_ids = [str(item["id"]) for item in trace.get("results") or []]
    if limit:
        selected_ids = selected_ids[:limit]
    source_by_id = {str(record["id"]): record for record in source}
    missing = [case_id for case_id in selected_ids if case_id not in source_by_id]
    if missing:
        raise ValueError(f"trace 中有 {len(missing)} 条样本不在 source")
    return [source_by_id[case_id] for case_id in selected_ids]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="含 id/history/query 的 JDDC JSONL")
    parser.add_argument("--trace", type=Path, help="可选：按在线 trace 的 case id 顺序抽取")
    parser.add_argument("--markdown-out", type=Path, required=True, help="供人工阅读填写的 Markdown")
    parser.add_argument("--jsonl-out", type=Path, required=True, help="同一批案例的结构化空模板")
    parser.add_argument("--limit", type=int, default=30, help="导出条数；0 表示全部")
    parser.add_argument("--min-history-turns", type=int, default=0, help="不使用 trace 时要求的最少上下文条数")
    parser.add_argument("--stratify-topics", action="store_true", help="按主题轮询抽样，不按机器动作抽样")
    parser.add_argument("--seed", type=int, default=20260828, help="分层抽样随机种子")
    args = parser.parse_args()
    if args.limit < 0 or args.min_history_turns < 0:
        parser.error("--limit 和 --min-history-turns 必须非负")
    if not args.source.is_file():
        parser.error(f"source 不存在：{args.source}")
    if args.trace is not None and not args.trace.is_file():
        parser.error(f"trace 不存在：{args.trace}")

    selected = _select_records(
        _load_jsonl(args.source),
        args.trace,
        args.limit,
        min_history_turns=args.min_history_turns,
        stratify_topics=args.stratify_topics,
        seed=args.seed,
    )
    if not selected:
        parser.error("没有可导出的案例")
    templates = [_template_record(record) for record in selected]
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.jsonl_out.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "# JDDC 客服 Ground Truth 标注包",
        "",
        "> 只根据可见上下文与当前用户消息填写；不参考机器初标、Agent 回复或线上轨迹。",
        "> 一条 Case 可以处于 `AWAITING_CUSTOMER`，这不等于失败；"
        "只有把未解决问题标为 `COMPLETED` 才是 false resolution。",
        "",
        f"共 {len(selected)} 条。",
        "",
    ]
    args.markdown_out.write_text(
        "\n".join(header + [_render_case(record, i) for i, record in enumerate(selected, 1)]), encoding="utf-8"
    )
    with args.jsonl_out.open("w", encoding="utf-8") as handle:
        for template in templates:
            handle.write(json.dumps(template, ensure_ascii=False) + "\n")
    print(f"已导出 {len(selected)} 条：{args.markdown_out}；{args.jsonl_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
