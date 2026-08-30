"""在不执行任何业务工具的前提下生成客服 Agent 第一阶段 Trace。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from agent.llm.llm_client import LLMClient
from config import settings
from evaluation.support_trace import (
    GoalSemanticJudge,
    SupportTraceGenerator,
    aggregate_scores,
    score_gold_against_trace,
)
from infra.circuit_breaker import CircuitBreaker


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _gold_goal(record: dict[str, Any]) -> str:
    value = record.get("ground_truth", {}).get("user_goal")
    if isinstance(value, dict):
        return str(value.get("primary") or "")
    return str(value or "")


async def _run(records: list[dict[str, Any]], *, judge_goals: bool, judge_model: str) -> dict[str, Any]:
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        timeout=settings.llm_timeout_seconds,
        max_attempts=settings.llm_max_attempts,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        sdk_max_retries=settings.llm_sdk_max_retries,
        stream_timeout=settings.llm_stream_timeout_seconds,
        circuit_breaker=CircuitBreaker(
            failure_threshold=settings.llm_circuit_failure_threshold,
            open_seconds=settings.llm_circuit_open_seconds,
        ),
    )
    generator = SupportTraceGenerator(llm)
    judge = (
        GoalSemanticJudge(
            LLMClient(
                api_key=settings.llm_api_key.get_secret_value(),
                base_url=settings.llm_base_url,
                model=judge_model,
                timeout=settings.llm_timeout_seconds,
                max_attempts=settings.llm_max_attempts,
                retry_backoff_seconds=settings.llm_retry_backoff_seconds,
                sdk_max_retries=settings.llm_sdk_max_retries,
                stream_timeout=settings.llm_stream_timeout_seconds,
                circuit_breaker=CircuitBreaker(
                    failure_threshold=settings.llm_circuit_failure_threshold,
                    open_seconds=settings.llm_circuit_open_seconds,
                ),
            )
        )
        if judge_goals
        else None
    )
    traces: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    total_tokens = 0
    for index, record in enumerate(records, start=1):
        generated = await generator.generate(record)
        total_tokens += generated.total_tokens
        traces.append(generated.trace)
        goal_judgment = None
        if judge:
            goal_judgment = await judge.judge(
                _gold_goal(record),
                str(generated.trace["parsed_goal"].get("primary") or ""),
            )
            total_tokens += int(goal_judgment.pop("total_tokens", 0))
        scores.append(score_gold_against_trace(record, generated.trace, goal_judgment=goal_judgment))
        print(f"[{index}/{len(records)}] {record['id']}")
    return {
        "evaluation_kind": "support_reasoning_dry_run",
        "agent_execution": False,
        "tool_execution": False,
        "write_actions_executed": False,
        "total_tokens": total_tokens,
        "summary": aggregate_scores(scores),
        "scores": scores,
        "traces": traces,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True, help="v2 人工金标 JSONL")
    parser.add_argument("--output", type=Path, required=True, help="JSON 评测产物")
    parser.add_argument("--limit", type=int, default=0, help="最多运行多少条；0 表示全部")
    parser.add_argument("--no-goal-judge", action="store_true", help="关闭 LLM Goal 语义 Judge")
    parser.add_argument(
        "--judge-model",
        default=os.getenv("EVAL_JUDGE_LLM_MODEL") or settings.llm_model,
        help="Goal Judge 模型，默认 EVAL_JUDGE_LLM_MODEL 或 LLM_MODEL",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit 必须非负")
    if not args.gold.is_file():
        parser.error(f"gold 不存在：{args.gold}")
    records = _load_jsonl(args.gold)
    if args.limit:
        records = records[: args.limit]
    if not records:
        parser.error("gold 为空")
    result = asyncio.run(_run(records, judge_goals=not args.no_goal_judge, judge_model=args.judge_model))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
