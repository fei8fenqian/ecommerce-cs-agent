"""对 JDDC 真实会话做盲审，填写项目动作金标。

输入应是 ``build_jddc_provisional_annotations.py`` 生成的 JSONL。标注时只显示真实原话和
上下文，不显示 ``provisional_action``、规则原因或来源标志，避免“看答案出题”。每次提交
都会立即写盘，使用 ``--output`` 传入同一路径即可中断后续审。

示例（先标 300 条，输出建议放在仓库外）：
    PYTHONPATH=src .venv/bin/python scripts/review_jddc_annotations.py \
        --input /tmp/jddc-3c-provisional.jsonl \
        --output /tmp/jddc-3c-human-review.jsonl \
        --limit 300 --annotator fei8
"""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ACTIONS: dict[str, tuple[str, str]] = {
    "1": ("CONTINUE", "普通咨询/查询，继续正常回答或调用只读工具"),
    "2": ("OFFER_REFUND_SELF_SERVICE", "首次退款/退货，先引导订单自助申请"),
    "3": ("SHOW_REFUND_PROGRESS", "已经申请/已退款但查询进度或未到账"),
    "4": ("OFFER_WARRANTY_TROUBLESHOOTING", "设备故障先做安全排查并收集事实"),
    "5": ("ASK_FOR_CLARIFICATION", "信息不足，先澄清诉求或关键事实"),
    "6": ("CREATE_TICKET", "明确人工/报修/投诉或已确认的异常，创建工单"),
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON") from exc
            if not isinstance(record, dict) or not record.get("id"):
                raise ValueError(f"{path}:{line_number} 缺少 id")
            records.append(record)
    return records


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """原子写入，避免终端中断造成半行 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _stratified_order(records: list[dict[str, Any]], *, seed: int) -> list[dict[str, Any]]:
    """按机器初标主题轮询，避免 300 条全被澄清样本占满。"""

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        topics = record.get("topics") or ["other_3c"]
        groups[str(topics[0])].append(record)
    rng = random.Random(seed)
    keys = list(groups)
    rng.shuffle(keys)
    for group in groups.values():
        rng.shuffle(group)

    ordered: list[dict[str, Any]] = []
    while keys:
        next_keys: list[str] = []
        for key in keys:
            group = groups[key]
            if group:
                ordered.append(group.pop())
            if group:
                next_keys.append(key)
        keys = next_keys
    return ordered


def _merge_existing(input_records: list[dict[str, Any]], output_path: Path) -> list[dict[str, Any]]:
    if not output_path.is_file():
        return input_records
    existing = {record["id"]: record for record in _load_jsonl(output_path)}
    merged: list[dict[str, Any]] = []
    for record in input_records:
        previous = existing.get(record["id"])
        if previous:
            for key in (
                "human_gold_action",
                "human_review_status",
                "human_annotator",
                "human_confidence",
                "human_note",
                "human_reviewed_at",
            ):
                if key in previous:
                    record[key] = previous[key]
        merged.append(record)
    return merged


def _print_case(record: dict[str, Any], ordinal: int, total: int) -> None:
    print(f"\n{'=' * 72}\n案例 {ordinal}/{total}  id={record['id']}")
    history = record.get("history") or []
    if history:
        print("上下文：")
        for message in history:
            role = "用户" if message.get("role") == "user" else "客服"
            print(f"  {role}: {message.get('content', '')}")
    print(f"当前用户：{record.get('query', '')}")
    print("\n动作选项（不显示机器初标）：")
    for key, (action, description) in _ACTIONS.items():
        print(f"  {key}. {action}: {description}")
    print("  s. SKIP：暂不标注，q. QUIT：保存并退出")


def _review(
    records: list[dict[str, Any]],
    *,
    annotator: str,
    limit: int,
    output_path: Path,
    preserve_order: bool = False,
) -> int:
    pending = [record for record in records if record.get("human_review_status") != "reviewed"]
    ordered = pending if preserve_order else _stratified_order(pending, seed=20260827)
    selected = ordered[:limit] if limit else ordered
    if not selected:
        print("没有待盲审记录。")
        return 0

    # 返回值让调用方在每次保存后写入完整记录，保证可以随时恢复。
    for index, record in enumerate(selected, start=1):
        _print_case(record, index, len(selected))
        while True:
            choice = input("选择动作: ").strip().lower()
            if choice == "q":
                _write_jsonl(output_path, records)
                return index - 1
            if choice == "s":
                record.update(
                    {
                        "human_review_status": "skipped",
                        "human_annotator": annotator,
                        "human_reviewed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                _write_jsonl(output_path, records)
                break
            if choice not in _ACTIONS:
                print("请输入 1-6、s 或 q。")
                continue

            action, _description = _ACTIONS[choice]
            confidence = input("置信度 1=低 2=中 3=高（默认 2）: ").strip() or "2"
            if confidence not in {"1", "2", "3"}:
                print("置信度只能是 1、2、3，请重新选择动作。")
                continue
            note = input("备注（可空）: ").strip()
            record.update(
                {
                    "human_gold_action": action,
                    "human_review_status": "reviewed",
                    "human_annotator": annotator,
                    "human_confidence": int(confidence),
                    "human_note": note,
                    "human_reviewed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            _write_jsonl(output_path, records)
            break
    return len(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="机器初标 JSONL")
    parser.add_argument("--output", type=Path, required=True, help="人工复核 JSONL，建议放在仓库外")
    parser.add_argument("--limit", type=int, default=300, help="本次最多标注多少条；0 表示全部")
    parser.add_argument("--annotator", default="local-reviewer", help="标注者标识，不要写真实敏感信息")
    parser.add_argument(
        "--preserve-order",
        action="store_true",
        help="按输入文件原顺序标注；用于和外部 trace 的前 N 条样本对齐",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if not args.input.is_file():
        parser.error(f"input does not exist: {args.input}")

    records = _merge_existing(_load_jsonl(args.input), args.output)
    reviewed = _review(
        records,
        annotator=args.annotator,
        limit=args.limit,
        output_path=args.output,
        preserve_order=args.preserve_order,
    )
    _write_jsonl(args.output, records)
    print(f"已保存 {args.output}；本次处理 {reviewed} 条。下次使用同一 --output 可继续。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
