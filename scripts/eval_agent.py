"""scripts/eval_agent.py — Agent 管线评估

分两层:
  L1: 意图分类准确率（IntentRouter 单次 LLM 调用，快）
  L2: Plan 结构校验（planner 输出是否合法，需跑完整图）

用法:
  python scripts/eval_agent.py                  # L1 全量
  python scripts/eval_agent.py --level 2        # L1 + L2
  python scripts/eval_agent.py --limit 20       # 只跑前 20 题
  python scripts/eval_agent.py --intent-only    # 只测新加的 agent/plan 题
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from config import settings
from infra.db_pool import init_pool

QUESTIONS_PATH = ROOT / "data" / "test_questions.json"

# 只测有 expected_intent 的题（有明确意图标注的）
# 老的 75 题大部分没标注 expected_intent，默认当 rag
INTENT_TYPES = {"rag", "agent", "ticket", "plan_execute"}


def load_questions(intent_only: bool = False) -> list[dict]:
    qs = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if intent_only:
        qs = [q for q in qs if "expected_intent" in q]
    return qs


# ── L1: 意图分类准确率 ────────────────────────────


async def eval_intent_router(questions: list[dict], limit: int | None = None):
    """评估 IntentRouter 的分类准确率。"""
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    router = IntentRouter(llm)

    if limit:
        questions = questions[:limit]

    # 混淆矩阵: actual → predicted
    matrix: dict[str, dict[str, int]] = {t: {t: 0 for t in INTENT_TYPES} for t in INTENT_TYPES}
    correct = 0
    total = 0
    errors: list[dict] = []

    print(f"{'=' * 70}")
    print(f"L1: 意图分类评估 — {len(questions)} 题")
    print(f"{'=' * 70}")

    for idx, q in enumerate(questions):
        query = q["query"]
        expected = q.get("expected_intent", "rag")
        if expected not in INTENT_TYPES:
            expected = "rag"

        t0 = time.perf_counter()
        intent = await router.route(query)
        elapsed = (time.perf_counter() - t0) * 1000

        predicted = intent.target
        matrix[expected][predicted] += 1
        total += 1
        if predicted == expected:
            correct += 1
        else:
            errors.append(
                {
                    "query": query[:60],
                    "expected": expected,
                    "predicted": predicted,
                    "confidence": intent.confidence,
                }
            )

        marker = "✓" if predicted == expected else "✗"
        scenario_ok = ""
        if expected == "plan_execute" and predicted == "plan_execute":
            exp_scenario = q.get("expected_scenario", "")
            if exp_scenario and intent.scenario != exp_scenario:
                scenario_ok = f" [scenario: got={intent.scenario} want={exp_scenario}]"

        print(f"  [{idx + 1:3d}] {marker} {query[:55]:55s} → {predicted:15s} {elapsed:6.0f}ms{scenario_ok}")

    accuracy = correct / total if total > 0 else 0
    print(f"\n{'─' * 70}")
    print(f"准确率: {correct}/{total} = {accuracy:.1%}")
    print()

    # 混淆矩阵
    print("混淆矩阵 (行=期望, 列=预测):")
    header = f"  {'':>16s}" + "".join(f"{t:>14s}" for t in sorted(INTENT_TYPES))
    print(header)
    for exp in sorted(INTENT_TYPES):
        row = f"  {exp:>16s}" + "".join(f"{matrix[exp][pred]:>14d}" for pred in sorted(INTENT_TYPES))
        print(row)

    # 各类别精确率/召回率
    print(f"\n{'─' * 70}")
    print(f"  {'类别':<16s} {'精确率':>8s} {'召回率':>8s} {'F1':>8s}")
    for t in sorted(INTENT_TYPES):
        tp = matrix[t][t]
        pred_sum = sum(matrix[exp][t] for exp in INTENT_TYPES)
        actual_sum = sum(matrix[t][pred] for pred in INTENT_TYPES)
        precision = tp / pred_sum if pred_sum > 0 else 0
        recall = tp / actual_sum if actual_sum > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
        print(f"  {t:<16s} {precision:8.1%} {recall:8.1%} {f1:8.3f}")

    # 最常混淆的
    if errors:
        print(f"\n{'─' * 70}")
        print(f"分类错误 ({len(errors)} 个):")
        for e in errors[:10]:
            print(f"  ✗ [{e['expected']}→{e['predicted']}] {e['query']} (conf={e['confidence']:.2f})")

    return accuracy


# ── L2: Plan 结构校验（仅 plan_execute 题目）────────


async def eval_plan_structure(questions: list[dict], limit: int | None = None):
    """测试 planner 生成的 plan 结构是否合法。"""
    from agent.engines.plan_execute import PlanAndExecuteAgent
    from agent.tools.create_ticket import CreateTicket
    from agent.tools.search_component import SearchComponent
    from agent.tools.search_product import SearchProduct
    from agent.tools.track_order import TrackOrder
    from agent.tools_registry import ToolRegistry

    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    registry = ToolRegistry()
    registry.register(SearchComponent())
    registry.register(SearchProduct())
    registry.register(TrackOrder())
    registry.register(CreateTicket())

    agent = PlanAndExecuteAgent(llm, registry)

    # 只测 plan_execute 的题
    plan_qs = [q for q in questions if q.get("expected_intent") == "plan_execute"]
    if limit:
        plan_qs = plan_qs[:limit]

    print(f"\n{'=' * 70}")
    print(f"L2: Plan 结构校验 — {len(plan_qs)} 题")
    print(f"{'=' * 70}")

    valid_count = 0
    total = 0

    for idx, q in enumerate(plan_qs):
        query = q["query"]
        scenario = q.get("expected_scenario", "build_pc")

        state = await agent.run(query, scenario=scenario)
        plan = state.get("plan", [])
        total += 1

        # 检查 plan 结构
        issues = []
        if not plan:
            issues.append("空 plan")
        else:
            seen_ids = set()
            for step in plan:
                sid = step.get("id")
                action = step.get("action", "")
                if not isinstance(sid, int):
                    issues.append(f"id 不是 int: {sid}")
                if not action:
                    issues.append("缺 action")
                if sid in seen_ids:
                    issues.append(f"重复 id: {sid}")
                if isinstance(sid, int) and sid in step.get("depends_on", []):
                    issues.append(f"自依赖: step {sid}")
                seen_ids.add(sid)

        valid = len(issues) == 0
        if valid:
            valid_count += 1

        status = "✓" if valid else "✗"
        steps_preview = [f"{s['id']}.{s.get('component', s.get('action', '?'))}" for s in plan[:5]]
        print(f"  [{idx + 1:2d}] {status} {query[:50]:50s} → {len(plan)}步 {steps_preview}")
        if issues:
            for issue in issues:
                print(f"      {issue}")

    rate = valid_count / total if total > 0 else 0
    print(f"\n{'─' * 70}")
    print(f"Plan 结构合法率: {valid_count}/{total} = {rate:.1%}")


# ── CLI ───────────────────────────────────────────


async def main(level: int = 1, limit: int | None = None, intent_only: bool = False):
    init_pool()
    questions = load_questions(intent_only=intent_only)

    if level >= 1:
        await eval_intent_router(questions, limit=limit)

    if level >= 2:
        await eval_plan_structure(questions, limit=limit)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Agent 管线评估")
    p.add_argument("--level", type=int, default=1, help="1=意图分类, 2=+plan校验")
    p.add_argument("--limit", type=int, default=None, help="限制题目数")
    p.add_argument("--intent-only", action="store_true", help="只测有 expected_intent 标注的题")
    args = p.parse_args()
    asyncio.run(main(level=args.level, limit=args.limit, intent_only=args.intent_only))
