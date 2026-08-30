"""在正式冻结前应用已审查的 execution fixture contract 修正。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data/benchmarks/execution/execution-cases-v1.jsonl"


def main() -> None:
    records = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = set()
    for record in records:
        if record["id"] == "exec-refund-request-entry-revalidation":
            record["expected"]["expected_block_reason"] = "capability_unavailable"
            record["expected"]["reason_category"] = "capability_unavailable"
            record["expected"]["must_not_include_claims"] = [
                "已生成可用退款入口",
                "退款已提交",
                "退款已成功",
                "已为你申请退款",
            ]
            changed.add(record["id"])
        elif record["id"] == "exec-refund-resume-selected-order":
            for refund in record["fixture"]["checkout_refunds"]:
                if refund["order_key"] == "order_2":
                    refund["storage_status"] = "SUCCEEDED"
                    changed.add(record["id"])
        elif record["id"] == "exec-refund-multiple-orders":
            record["expected"]["required_facts"] = ["order_identified", "refund_status"]
            changed.add(record["id"])
        elif record["id"] == "exec-refund-other-customer-order":
            fixture = record["fixture"]
            fixture["other_customers"] = [{"key": "TEST_CUSTOMER_B"}]
            other_order = fixture["other_customer_orders"][0]
            other_order.pop("refund_status", None)
            other_order["payment"] = {
                "status": "SUCCEEDED",
                "amount_cents": 240000,
                "provider": "alipay_sandbox",
            }
            other_order["fulfillment"] = {"status": "PENDING_FULFILLMENT"}
            fixture["checkout_refunds"] = [
                {
                    "order_key": "order_b",
                    "customer_key": "TEST_CUSTOMER_B",
                    "storage_status": "PROCESSING",
                    "amount_cents": 240000,
                }
            ]
            record["expected"]["must_include_claims"] = ["无法核验订单 SOEXEC020B，请核对本人订单"]
            record["expected"]["must_not_include_claims"] = [
                "PROCESSING",
                "240000",
                "退款已处理",
                "支付信息",
                "履约信息",
            ]
            changed.add(record["id"])
    if changed != {
        "exec-refund-request-entry-revalidation",
        "exec-refund-resume-selected-order",
        "exec-refund-multiple-orders",
        "exec-refund-other-customer-order",
    }:
        raise RuntimeError(f"unexpected contract changes: {sorted(changed)}")
    CASES.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    print(f"Updated {len(changed)} execution cases")


if __name__ == "__main__":
    main()
