"""从冻结 v1 Gold 生成仅含两项人工 adjudication 的 v2 Gold。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/benchmarks/jddc/refund-gold-100.jsonl"
OUTPUT = ROOT / "data/benchmarks/jddc/refund-gold-100-adjudicated-v2.jsonl"

UPDATES = {
    "jddc-refund-004-a5aa25e524dd51737d3c45e2c50c5864-1": {
        "speech_act": "INFORMATION_QUERY",
        "expected_requests": [{"domain": "general", "operation": "answer"}],
        "primary_goal": "general.answer",
        "expected_workflow": None,
        "needs_clarification": False,
        "notes": (
            "DISPUTED / LOW-CONFIDENCE ADJUDICATION：已退款后为何仍有来电；当前 taxonomy "
            "缺少退款完成后的异常联系/通知 Goal，general.answer 为最小兜底，不表示物流交叉状态。"
        ),
    },
    "jddc-refund-045-5c16e2f699210882c0de424b9314cb60-1": {
        "speech_act": "INFORMATION_QUERY",
        "expected_requests": [{"domain": "refund", "operation": "anomaly"}],
        "primary_goal": "refund.anomaly",
        "expected_workflow": "refund.anomaly",
        "needs_clarification": False,
        "notes": "客服刚询问需要处理的问题；用户说明第一笔退款未入账而另一笔立即到账，语用上是在报告第一笔退款异常。",
    },
}


def main() -> None:
    records = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed: list[str] = []
    for record in records:
        update = UPDATES.get(record["case_id"])
        if update is not None:
            record.update(update)
            changed.append(record["case_id"])
    if len(records) != 100 or set(changed) != set(UPDATES):
        raise RuntimeError(f"Gold v2 生成异常：records={len(records)} changed={changed}")
    OUTPUT.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)}; changed={len(changed)}")


if __name__ == "__main__":
    main()
