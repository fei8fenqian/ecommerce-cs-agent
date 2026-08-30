"""从 JDDC 原始语料导出退款领域的 100 条人工标注包。

输出格式与此前的 ``jddc-3c-context-100``、``jddc-3c-ground-truth-100`` 对齐：
一份带来源信息的上下文 JSONL、一份空 Ground Truth JSONL，以及一份供人工填写的
Markdown。这里不生成、不推断 Gold 标签。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from extract_jddc_refund_cases import _load_excluded_session_ids, _read_sessions, _select_cases


def _blank_ground_truth() -> dict[str, Any]:
    return {
        "domain": "",
        "operation": "",
        "user_goal": "",
        "relevant_entities": [],
        "required_facts": [],
        "required_capabilities": [],
        "valid_solution_paths": [],
        "invalid_solution_paths": [],
        "business_constraints": [],
        "expected_case_status": "",
        "expected_final_outcome": "",
        "escalation_required": None,
        "expected_response_notes": "",
        "annotator_note": "",
    }


def _context_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "dataset": record["dataset"],
        "annotation_source": "JDDC source extraction only",
        "human_review_status": "pending",
        "session_id": record["session_id"],
        "turn_index": record["turn_index"],
        "query": record["query"],
        "history": record["history"],
    }


def _ground_truth_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "history": record["history"],
        "query": record["query"],
        "ground_truth": _blank_ground_truth(),
        "annotation_status": "pending",
        "annotator": "",
        "reviewed_at": "",
    }


def _render_case(record: dict[str, Any], ordinal: int) -> str:
    lines = [f"## {ordinal}. {record['id']}", ""]
    if record["history"]:
        lines.extend(["**可见上下文**", ""])
        for message in record["history"]:
            role = "用户" if message["role"] == "user" else "客服"
            content = str(message["content"]).replace("\n", " ")
            lines.append(f"> **{role}：** {content}")
        lines.append("")
    lines.extend(
        [
            f"**当前用户：** {record['query']}",
            "",
            "### Ground Truth（必填）",
            "",
            "- domain（例如 `refund` / `price_protection` / `return` / `payment`）：",
            "- operation（例如 `status` / `expected_arrival` / `destination` / `request`）：",
            "- 用户目标：",
            "- 相关实体（订单 / 商品 / 退款单 / 售后单 / 支付记录等）：",
            "- 解决此问题前必须确认的事实（使用稳定 fact 名）：",
            "- 所需业务能力及顺序（使用能力名，不写具体 Tool 名；一个能力可以由一个或多个 Tool 提供）：",
            "- 预期 Case 状态（`ACTIVE` / `AWAITING_CUSTOMER` / `AWAITING_STAFF` / `COMPLETED` / `FAILED`）：",
            "- 预期最终结果：",
            "- 是否必须转人工（是 / 否 / 条件性）：",
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
            "### 最终回复要求 / 标注备注",
            "",
            "- ",
            "",
            "---",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="JDDC 原始 data/chat.txt")
    parser.add_argument("--context-out", type=Path, required=True)
    parser.add_argument("--ground-truth-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument(
        "--exclude",
        type=Path,
        help="已有批次 JSONL；按 session_id 排除，确保新批次不重复",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260828)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("limit 必须为正数")
    if not args.source.is_file():
        parser.error(f"JDDC source 不存在：{args.source}")

    if args.exclude is not None and not args.exclude.is_file():
        parser.error(f"排除文件不存在：{args.exclude}")
    records = _select_cases(
        _read_sessions(args.source),
        limit=args.limit,
        seed=args.seed,
        exclude_session_ids=_load_excluded_session_ids(args.exclude),
    )
    context_records = [_context_record(record) for record in records]
    ground_truth_records = [_ground_truth_record(record) for record in records]
    for path in (args.context_out, args.ground_truth_out, args.markdown_out):
        path.parent.mkdir(parents=True, exist_ok=True)

    args.context_out.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in context_records),
        encoding="utf-8",
    )
    args.ground_truth_out.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in ground_truth_records),
        encoding="utf-8",
    )
    header = [
        "# JDDC 退款领域 Ground Truth 标注包",
        "",
        "> 100 条案例均来自 JDDC 原始 `chat.txt`，仅筛选退款相关用户消息并保留该会话最近 8 条上下文。",
        "> JDDC 原始 `is_transfer` 等字段不是本项目的业务 Ground Truth；请只根据可见上下文和当前用户消息标注。",
        "",
        f"共 {len(records)} 条。",
        "",
    ]
    args.markdown_out.write_text(
        "\n".join(header + [_render_case(record, index) for index, record in enumerate(records, start=1)]),
        encoding="utf-8",
    )
    print(f"Exported {len(records)} real JDDC refund cases")
    print(args.context_out)
    print(args.ground_truth_out)
    print(args.markdown_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
