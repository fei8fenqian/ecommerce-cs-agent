"""在 DataCLUE/CIC 公开测试集上做客户支持安全扫描。

DataCLUE/CIC 的原始标签是 118 类单轮客户意图，不是本项目的业务动作金标。
本脚本保留原始 ``label``/``label_des``，只验证把一条消息误判为 ``ticket`` 时，
确定性动作闸门是否仍能拦住危险动作。结果是安全扫描，不是意图准确率。

运行示例：
    PYTHONPATH=src .venv/bin/python scripts/run_dataclue_cic_support_scan.py \
        --source /tmp/dataclue/datasets/raw_cic/test_public.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from service.customer_support_policy import CustomerSupportAction, decide_customer_support_action

_EXPLICIT_SUPPORT_SIGNAL = re.compile(
    r"人工|真人|客服|投诉|纠纷|争议|赔偿|报修|维修|售后|保修|检测|换货|换新|"
    r"冒烟|起火|着火|爆炸|鼓包|漏电|烧焦|坏了|故障|不能用|无法使用|开不了机|"
    r"没反应|损坏|不灵敏|退款失败|退款没到账|退款未到账|支付失败|付款失败|订单异常|"
    r"扣款|少退|退少|改地址|修改地址|地址填错|取消订单|申请取消|不能申请退款|"
    r"退款申请不了|退不了",
)
_REFUND_PROGRESS = re.compile(
    r"已经退|已退|退过|已退款|退款了|已退货|退货了|已经申请退款|已申请退款|申请过退款|"
    r"退款进度|退款状态|到账了吗|到账没|哪里看退款|怎么看退款|查退款|没到账|未到账|"
    r"还没退款|没有退款|还没有退款|退款点了没反应",
)
_DEVICE_DANGER = re.compile(r"冒烟|起火|着火|爆炸|电池鼓包|漏电|烧焦")


def _read_records(path: Path) -> Iterable[dict[str, Any]]:
    """读取 DataCLUE 的 JSONL，也兼容 JSON 数组镜像。"""

    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("CIC JSON array must contain a list")
        records = data
    else:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]

    for line_number, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"record {line_number} is not an object")
        if "expected_action" in record or "human_gold_action" in record:
            raise ValueError(
                f"record {line_number} contains a project action label; CIC source labels must stay separate"
            )
        if not isinstance(record.get("sentence"), str) or not record["sentence"].strip():
            raise ValueError(f"record {line_number} is missing sentence")
        if "label" not in record:
            raise ValueError(f"record {line_number} is missing source label")
        yield record


def _scan(records: list[dict[str, Any]]) -> dict[str, object]:
    action_counts: Counter[str] = Counter()
    source_label_counts: Counter[str] = Counter()
    violations: list[dict[str, object]] = []

    for record in records:
        query = str(record["sentence"]).strip()
        decision = decide_customer_support_action(
            intent_target="ticket",
            role="customer",
            query=query,
            history=[],
        )
        action = CustomerSupportAction(decision.action).value
        action_counts[action] += 1
        source_label_counts[str(record["label"])] += 1

        has_support_signal = bool(_EXPLICIT_SUPPORT_SIGNAL.search(query))
        has_refund_progress = bool(_REFUND_PROGRESS.search(query))
        violation: str | None = None
        if action == CustomerSupportAction.CREATE_TICKET.value and not has_support_signal:
            violation = "CREATE_TICKET_WITHOUT_EXPLICIT_SUPPORT_SIGNAL"
        elif action == CustomerSupportAction.OFFER_REFUND_SELF_SERVICE.value and has_refund_progress:
            violation = "REFUND_PROGRESS_ROUTED_TO_FIRST_REFUND_GUIDANCE"
        elif action == CustomerSupportAction.SHOW_REFUND_PROGRESS.value and not has_refund_progress:
            violation = "REFUND_PROGRESS_WITHOUT_PROGRESS_SIGNAL"
        elif _DEVICE_DANGER.search(query) and action != CustomerSupportAction.CREATE_TICKET.value:
            violation = "DANGER_NOT_ESCALATED"

        if violation:
            violations.append(
                {
                    "id": f"dataclue-cic-{record.get('id', len(violations))}",
                    "source_label": str(record["label"]),
                    "source_label_des": str(record.get("label_des") or ""),
                    "query": query,
                    "action": action,
                    "reason": decision.reason,
                    "violation": violation,
                }
            )

    return {
        "evaluated": len(records),
        "unique_queries": len({str(record["sentence"]).strip() for record in records}),
        "source_labels": len(source_label_counts),
        "actions": dict(sorted(action_counts.items())),
        "violations": violations,
        "violation_count": len(violations),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="DataCLUE raw_cic/test_public.json")
    parser.add_argument("--limit", type=int, default=0, help="最多评测多少条；0 表示全部（默认）")
    parser.add_argument("--json-out", type=Path, help="可选：把扫描结果写到本地文件")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if not args.source.is_file():
        parser.error(f"CIC source does not exist: {args.source}")

    try:
        source_records = list(_read_records(args.source))
    except (OSError, ValueError) as exc:
        parser.error(f"invalid CIC source: {exc}")
    records = source_records[: args.limit] if args.limit else source_records
    if not records:
        parser.error("CIC source contains no records")

    result = {
        "dataset": "DataCLUE Customer Intent Classification (CIC)",
        "evaluation_kind": "deterministic_gate_invariant_scan",
        "agent_execution": False,
        "llm_calls": 0,
        "source_labels_compared": False,
        "action_accuracy": None,
        "source": str(args.source),
        "source_records": len(source_records),
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
