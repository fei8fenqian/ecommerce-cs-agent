"""为 JDDC 3C 会话生成可复核的机器初标批次。

JDDC 自带的 ``is_transfer``/``is_repeat`` 是原始数据字段，不是本项目的动作金标，
而且 transfer 极度稀疏。因此本脚本只把它们原样保留，并基于当前确定性客服策略生成
``provisional_action``。输出中的 ``human_gold_action`` 永远为空，供人工盲审后填写；
不要把本文件当作准确率金标或训练标签。

运行示例：
    PYTHONPATH=src .venv/bin/python scripts/build_jddc_provisional_annotations.py \
        --source /path/to/JDDC-Baseline-Seq2Seq/data/chat.txt \
        --output /tmp/jddc-3c-provisional.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from run_jddc_customer_support_eval import SupportCase, _read_sessions, _select_cases

from service.customer_support_policy import CustomerSupportAction, decide_customer_support_action


def _read_source_flags(path: Path) -> dict[str, list[dict[str, bool]]]:
    """按与评测脚本相同的非空内容规则读取原始 JDDC 标志。"""

    flags: dict[str, list[dict[str, bool]]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        next(handle, None)
        for raw in handle:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            session_id, _user_id, _waiter_send = parts[:3]
            content = parts[-1].strip()
            if not content:
                continue
            flags[session_id].append(
                {
                    "is_transfer": parts[3] == "1",
                    "is_repeat": parts[4] == "1",
                }
            )
    return dict(flags)


def _review_reasons(case: SupportCase, decision: object, source_flags: dict[str, bool]) -> list[str]:
    reasons: list[str] = []
    if decision.action == CustomerSupportAction.ASK_FOR_CLARIFICATION:
        reasons.append("insufficient_facts")
    if "other_3c" in case.topics:
        reasons.append("topic_unclassified")
    if len(case.topics) > 1:
        reasons.append("multi_topic")
    if decision.action == CustomerSupportAction.CREATE_TICKET:
        reasons.append("high_risk_action")
    if source_flags.get("is_transfer"):
        reasons.append("source_transfer_signal")
    if source_flags.get("is_repeat"):
        reasons.append("source_repeat_signal")
    return reasons


def _build_records(
    source: Path,
    *,
    seed: int,
    limit: int,
    min_history_turns: int = 0,
) -> tuple[list[dict[str, object]], dict[str, int]]:
    sessions = _read_sessions(source)
    all_cases = _select_cases(sessions, seed=seed, min_history_turns=min_history_turns)
    cases = all_cases[:limit] if limit else all_cases
    source_flags = _read_source_flags(source)
    records: list[dict[str, object]] = []
    action_counts: Counter[str] = Counter()

    for case in cases:
        history = [{"role": turn.role, "content": turn.content} for turn in case.history]
        decision = decide_customer_support_action(
            intent_target="ticket",
            role="customer",
            query=case.query,
            history=history,
        )
        action = CustomerSupportAction(decision.action).value
        action_counts[action] += 1
        selected_flags = (
            source_flags.get(case.session_id, [])[case.turn_index]
            if case.turn_index < len(source_flags.get(case.session_id, []))
            else {}
        )
        review_reasons = _review_reasons(case, decision, selected_flags)
        records.append(
            {
                "id": f"jddc-{case.session_id}-{case.turn_index}",
                "dataset": "JDDC baseline anonymized customer-service corpus",
                "annotation_source": "policy_v1_machine",
                "annotation_version": "2026-08-27",
                "human_review_status": "pending",
                "human_gold_action": None,
                "session_id": case.session_id,
                "turn_index": case.turn_index,
                "query": case.query,
                "history": history[-8:],
                "topics": list(case.topics),
                "provisional_action": action,
                "provisional_reason": decision.reason,
                "review_required": bool(review_reasons),
                "review_reasons": review_reasons,
                "source_labels": {
                    "is_transfer": bool(selected_flags.get("is_transfer", False)),
                    "is_repeat": bool(selected_flags.get("is_repeat", False)),
                    "session_has_transfer": any(
                        flag.get("is_transfer", False) for flag in source_flags.get(case.session_id, [])
                    ),
                },
            }
        )
    return records, dict(sorted(action_counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="JDDC data/chat.txt")
    parser.add_argument("--output", type=Path, required=True, help="输出 JSONL 路径（建议放在仓库外）")
    parser.add_argument("--limit", type=int, default=0, help="最多输出多少个 3C 会话；0 表示全部")
    parser.add_argument("--seed", type=int, default=20260827, help="抽样种子")
    parser.add_argument(
        "--min-history-turns",
        type=int,
        default=0,
        help="只选择前面至少已有多少条对话消息的用户轮；0 表示不限制",
    )
    args = parser.parse_args()
    if args.limit < 0 or args.min_history_turns < 0:
        parser.error("limit and min-history-turns must be non-negative")
    if not args.source.is_file():
        parser.error(f"JDDC source does not exist: {args.source}")

    records, action_counts = _build_records(
        args.source,
        seed=args.seed,
        limit=args.limit,
        min_history_turns=args.min_history_turns,
    )
    if not records:
        parser.error("JDDC source contains no 3C customer-support sessions")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "records": len(records),
                "action_counts": action_counts,
                "annotation_source": "policy_v1_machine",
                "human_gold_action": "null (pending blind review)",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
