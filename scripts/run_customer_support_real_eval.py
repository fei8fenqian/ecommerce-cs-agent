"""在真实中文电商客服语料上做客户支持安全扫描。

ECD (E-commerce Dialogue Corpus) 是检索式对话数据，不提供本项目的工单动作金标。
因此本脚本不把结果伪装成准确率，而是检查当前策略必须满足的安全不变量，并输出
需要人工复核的真实用户原话。

下载 ECD 后运行：
    PYTHONPATH=src .venv/bin/python scripts/run_customer_support_real_eval.py \
        --source /path/to/test.txt --limit 500
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from service.customer_support_policy import CustomerSupportAction, decide_customer_support_action

_EXPLICIT_TICKET_SIGNAL = re.compile(
    r"人工|真人|客服介入|官方介入|平台介入|投诉|纠纷|争议|赔偿|报修|保修|维修|故障|坏了|"
    r"退款失败|退款没到账|退款未到账|退款申请不了|支付失败|付款失败|订单异常|页面报错|"
    r"扣款|少退|退少|改地址|修改地址|地址填错",
)
_REFUND_PROGRESS = re.compile(
    r"已经退|已退|退过|已退款|退款了|已退货|退货了|已经申请退款|已申请退款|申请过退款|"
    r"退款进度|退款状态|到账了吗|到账没|哪里看退款|怎么看退款|查退款|没到账|未到账|还没退款|"
    r"退款点了没反应",
)


@dataclass(frozen=True)
class EcdRecord:
    line_number: int
    source_label: str
    history: tuple[str, ...]
    query: str
    candidate_response: str


def _detokenize(value: str) -> str:
    # ECD 用空格分隔中文 token；评测输入应恢复为用户实际看到的自然文本。
    return re.sub(r"\s+", "", value).strip()


def _read_ecd(path: Path) -> Iterable[EcdRecord]:
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            context = tuple(_detokenize(item) for item in parts[1:-1])
            if not context or not context[-1]:
                continue
            yield EcdRecord(
                line_number=line_number,
                source_label=parts[0],
                history=context[:-1],
                query=context[-1],
                candidate_response=_detokenize(parts[-1]),
            )


def _unique_records(records: Iterable[EcdRecord], *, seed: int) -> list[EcdRecord]:
    by_query: dict[str, EcdRecord] = {}
    for record in records:
        by_query.setdefault(record.query, record)
    result = list(by_query.values())
    random.Random(seed).shuffle(result)
    return result


def _scan(records: list[EcdRecord]) -> dict[str, object]:
    action_counts: Counter[str] = Counter()
    violations: list[dict[str, object]] = []

    for record in records:
        decision = decide_customer_support_action(
            intent_target="ticket",
            role="customer",
            query=record.query,
            history=[],
        )
        action = CustomerSupportAction(decision.action).value
        action_counts[action] += 1
        query_has_exception = bool(_EXPLICIT_TICKET_SIGNAL.search(record.query))
        query_has_progress = bool(_REFUND_PROGRESS.search(record.query))

        violation: str | None = None
        if action == CustomerSupportAction.CREATE_TICKET.value and not query_has_exception:
            violation = "CREATE_TICKET_WITHOUT_EXPLICIT_SUPPORT_SIGNAL"
        elif action == CustomerSupportAction.OFFER_REFUND_SELF_SERVICE.value and query_has_progress:
            violation = "REFUND_PROGRESS_ROUTED_TO_FIRST_REFUND_GUIDANCE"
        elif action == CustomerSupportAction.SHOW_REFUND_PROGRESS.value and not query_has_progress:
            violation = "REFUND_PROGRESS_WITHOUT_PROGRESS_SIGNAL"

        if violation:
            violations.append(
                {
                    "id": f"ecd-test-{record.line_number:05d}",
                    "line_number": record.line_number,
                    "query": record.query,
                    "history": list(record.history),
                    "action": action,
                    "reason": decision.reason,
                    "violation": violation,
                }
            )

    return {
        "evaluated": len(records),
        "actions": dict(sorted(action_counts.items())),
        "violations": violations,
        "violation_count": len(violations),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="ECD train/dev/test.txt")
    parser.add_argument("--limit", type=int, default=0, help="去重后评测条数；0 表示使用全部样本（默认）")
    parser.add_argument("--seed", type=int, default=20260827, help="抽样种子")
    parser.add_argument("--json-out", type=Path, help="可选：把扫描结果写到本地文件")
    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if not args.source.is_file():
        parser.error(f"ECD source does not exist: {args.source}")

    source_records = list(_read_ecd(args.source))
    unique_records = _unique_records(source_records, seed=args.seed)
    records = unique_records
    if args.limit:
        records = records[: args.limit]
    if not records:
        parser.error("ECD source contains no usable records")
    result = {
        "dataset": "E-commerce Dialogue Corpus (ECD)",
        "evaluation_kind": "deterministic_gate_invariant_scan",
        "agent_execution": False,
        "llm_calls": 0,
        "source_labels_compared": False,
        "action_accuracy": None,
        "source": str(args.source),
        "seed": args.seed,
        "source_records": len(source_records),
        "unique_queries": len(unique_records),
        **_scan(records),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 1 if result["violation_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
