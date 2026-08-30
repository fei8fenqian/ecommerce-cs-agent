"""从人工编写的 execution cases 提取冻结 Gold；不读取任何模型输出。"""

from __future__ import annotations

import json
from pathlib import Path

from execution_benchmark_contract import validate_cases_contract

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "data/benchmarks/execution/execution-cases-v1.jsonl"
GOLD = ROOT / "data/benchmarks/execution/execution-gold-v1.jsonl"


def main() -> None:
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(cases) != 20 or len({case["id"] for case in cases}) != 20:
        raise RuntimeError("execution cases 必须有 20 条唯一 case")
    validate_cases_contract(cases)
    gold = [{"id": case["id"], "oracle": case["oracle"], "expected": case["expected"]} for case in cases]
    GOLD.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in gold), encoding="utf-8")
    print(f"Wrote {GOLD.relative_to(ROOT)} ({len(gold)} cases)")


if __name__ == "__main__":
    main()
