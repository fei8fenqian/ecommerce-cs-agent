"""从 JDDC 原始 chat.txt 抽取 100 条真实退款相关多轮案例。

输出只包含匿名 case id、原始上下文和当前用户原话，不写入本项目的机器动作或
Ground Truth。Ground Truth 模板应再交给人工标注，避免把关键词筛选误当成业务金标。
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

_DIRECT_REFUND = re.compile(r"退款|退钱|退费|返款|返钱|退款到账|退款进度|退款状态|退款成功|退款失败|款没退|款未退")
_INFORMATIVE = re.compile(r"没到账|未到账|什么时候|多久|进度|状态|成功|失败|金额|少退|退不全|申请|查询|查一下|哪里")
_NOISE = re.compile(r"^(谢谢|好的|嗯+|哦+|在吗[？?！!。．]*|没了|没有了|\?+|？+)$")


def _read_sessions(path: Path) -> dict[str, list[dict[str, str]]]:
    sessions: dict[str, list[dict[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        next(handle, None)
        for raw in handle:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            session_id, _user_id, waiter_send = parts[:3]
            content = parts[-1].strip()
            if content:
                sessions[session_id].append({"role": "assistant" if waiter_send == "1" else "user", "content": content})
    return dict(sessions)


def _candidate_score(query: str, index: int, turn_count: int) -> tuple[int, int, int]:
    """优先选择有明确业务问题、且带上下文的真实用户轮次。"""
    score = len(query)
    if _INFORMATIVE.search(query):
        score += 30
    if len(query) <= 3:
        score -= 30
    return score, min(index, 999), min(turn_count, 999)


def _select_cases(
    sessions: dict[str, list[dict[str, str]]],
    *,
    limit: int,
    seed: int,
    exclude_session_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[tuple[str, int, dict[str, str], list[dict[str, str]]]] = []
    for session_id, turns in sessions.items():
        if exclude_session_ids and session_id in exclude_session_ids:
            continue
        matching = [
            (index, turn)
            for index, turn in enumerate(turns)
            if turn["role"] == "user"
            and _DIRECT_REFUND.search(turn["content"])
            and not _NOISE.fullmatch(turn["content"])
        ]
        if not matching:
            continue
        index, turn = max(matching, key=lambda item: _candidate_score(item[1]["content"], item[0], len(turns)))
        candidates.append((session_id, index, turn, turns))

    if len(candidates) < limit:
        raise RuntimeError(f"JDDC 退款候选不足：需要 {limit} 条，只有 {len(candidates)} 条")

    random.Random(seed).shuffle(candidates)
    selected = candidates[:limit]
    records: list[dict[str, Any]] = []
    for ordinal, (session_id, index, turn, turns) in enumerate(selected, start=1):
        records.append(
            {
                "id": f"jddc-refund-{ordinal:03d}-{session_id}-{index}",
                "dataset": "JDDC baseline anonymized customer-service corpus",
                "session_id": session_id,
                "turn_index": index,
                "history": turns[max(0, index - 8) : index],
                "query": turn["content"],
            }
        )
    return records


def _load_excluded_session_ids(path: Path | None) -> set[str]:
    if path is None:
        return set()
    with path.open(encoding="utf-8") as handle:
        return {
            str(record.get("session_id"))
            for raw in handle
            if raw.strip()
            for record in [json.loads(raw)]
            if record.get("session_id")
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="JDDC 原始 data/chat.txt")
    parser.add_argument("--output", type=Path, required=True, help="抽取出的 100 条 JSONL")
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
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8"
    )
    print(f"Extracted {len(records)} real JDDC refund cases: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
