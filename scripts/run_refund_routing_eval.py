"""用真实 IntentRouter 对退款相关 JDDC Gold 做路由评测。

该脚本只调用意图路由器，不查询订单、不读取退款数据、不执行任何写操作。
它回答的是：Gold 已经定义的 domain/operation，当前 Router 是否识别正确。
这与 Planner 的事实和路径评测分开，便于区分路由错误与 Workflow 错误。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from agent.goal_taxonomy import LEGACY_OPERATION_ALIASES, canonicalize_goal, workflow_key_for_goal
from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from config import settings
from infra.circuit_breaker import CircuitBreaker


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _input_data(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("input")
    return value if isinstance(value, dict) else record


def _gold_route(record: dict[str, Any]) -> tuple[str, str]:
    ground_truth = record.get("ground_truth") or {}
    domain = str(ground_truth.get("domain") or "").strip()
    operation = str(ground_truth.get("operation") or "").strip()
    return domain, operation


def _predicted_route(domain: str, operation: str) -> tuple[str, str]:
    return domain, operation


def _canonical_operation(operation: str) -> str:
    return LEGACY_OPERATION_ALIASES.get(operation, (operation, ""))[0]


def _workflow_route(domain: str, operation: str) -> str:
    _, canonical = canonicalize_goal(domain, _canonical_operation(operation))
    workflow_key = workflow_key_for_goal(domain, canonical)
    if workflow_key is not None:
        return workflow_key
    return f"{domain or 'unknown'}.{canonical or 'unknown'}"


def _route_key(domain: str, operation: str) -> str:
    return f"{domain or 'unknown'}.{operation or 'unknown'}"


def _client(model: str) -> LLMClient:
    return LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=model,
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


async def _run(records: list[dict[str, Any]], *, model: str, interval_seconds: float) -> dict[str, Any]:
    router = IntentRouter(_client(model))
    rows: list[dict[str, Any]] = []
    errors = 0
    for index, record in enumerate(records, start=1):
        input_data = _input_data(record)
        gold_domain, gold_operation = _gold_route(record)
        try:
            intent = await router.route(
                query=str(input_data.get("query") or ""),
                history=input_data.get("history") if isinstance(input_data.get("history"), list) else [],
            )
            predicted_domain, predicted_operation = _predicted_route(intent.domain, intent.operation)
            error = ""
        except Exception as exc:  # 单条失败要进入产物，不掩盖整体结果
            errors += 1
            predicted_domain, predicted_operation = "", ""
            intent = None
            error = f"{type(exc).__name__}: {exc}"[:500]

        rows.append(
            {
                "case_id": str(record.get("id") or ""),
                "gold": {"domain": gold_domain, "operation": gold_operation},
                "predicted": {"domain": predicted_domain, "operation": predicted_operation},
                "domain_match": bool(gold_domain) and predicted_domain == gold_domain,
                "exact_operation_match": bool(gold_operation) and predicted_operation == gold_operation,
                "canonical_operation_match": bool(gold_operation)
                and _canonical_operation(predicted_operation) == _canonical_operation(gold_operation),
                "exact_route_match": bool(gold_domain and gold_operation)
                and (predicted_domain, predicted_operation) == (gold_domain, gold_operation),
                "workflow_route_match": bool(gold_domain and gold_operation)
                and _workflow_route(predicted_domain, predicted_operation)
                == _workflow_route(gold_domain, gold_operation),
                "intent": (
                    {
                        "target": intent.target,
                        "query": intent.query,
                        "confidence": intent.confidence,
                        "route_source": intent.route_source,
                        "goal_modifier": intent.goal_modifier,
                        "ambiguities": intent.ambiguities,
                        "state": intent.state,
                        "next_step": intent.next_step,
                        "required_tools": intent.required_tools,
                        "requests": [request.to_case_payload() for request in intent.requests],
                    }
                    if intent is not None
                    else None
                ),
                "error": error,
            }
        )
        print(
            f"[{index}/{len(records)}] {record.get('id', '')} -> {_route_key(predicted_domain, predicted_operation)}",
            flush=True,
        )
        if interval_seconds:
            await asyncio.sleep(interval_seconds)

    evaluated = [row for row in rows if not row["error"]]
    domain_matches = sum(bool(row["domain_match"]) for row in evaluated)
    exact_operation_matches = sum(bool(row["exact_operation_match"]) for row in evaluated)
    canonical_operation_matches = sum(bool(row["canonical_operation_match"]) for row in evaluated)
    exact_route_matches = sum(bool(row["exact_route_match"]) for row in evaluated)
    workflow_route_matches = sum(bool(row["workflow_route_match"]) for row in evaluated)
    exact_confusion: dict[str, dict[str, int]] = defaultdict(Counter)
    workflow_confusion: dict[str, dict[str, int]] = defaultdict(Counter)
    for row in evaluated:
        exact_confusion[_route_key(row["gold"]["domain"], row["gold"]["operation"])][
            _route_key(row["predicted"]["domain"], row["predicted"]["operation"])
        ] += 1
        workflow_confusion[_workflow_route(row["gold"]["domain"], row["gold"]["operation"])][
            _workflow_route(row["predicted"]["domain"], row["predicted"]["operation"])
        ] += 1

    return {
        "evaluation_kind": "intent_router_refund_routing",
        "model": model,
        "agent_execution": False,
        "tool_execution": False,
        "write_actions_executed": False,
        "summary": {
            "total": len(rows),
            "evaluated": len(evaluated),
            "errors": errors,
            "domain_accuracy": domain_matches / len(evaluated) if evaluated else 0.0,
            "operation_accuracy": exact_operation_matches / len(evaluated) if evaluated else 0.0,
            "exact_annotated_operation_accuracy": exact_operation_matches / len(evaluated) if evaluated else 0.0,
            "canonical_operation_accuracy": canonical_operation_matches / len(evaluated) if evaluated else 0.0,
            "exact_route_accuracy": exact_route_matches / len(evaluated) if evaluated else 0.0,
            "workflow_route_accuracy": workflow_route_matches / len(evaluated) if evaluated else 0.0,
            "route_accuracy": workflow_route_matches / len(evaluated) if evaluated else 0.0,
        },
        "exact_confusion_matrix": {gold: dict(predicted) for gold, predicted in sorted(exact_confusion.items())},
        "workflow_confusion_matrix": {gold: dict(predicted) for gold, predicted in sorted(workflow_confusion.items())},
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True, help="退款相关人工 Gold JSONL")
    parser.add_argument("--output", type=Path, required=True, help="路由评测 JSON 产物")
    parser.add_argument("--limit", type=int, default=0, help="最多运行多少条；0 表示全部")
    parser.add_argument(
        "--model",
        default=os.getenv("EVAL_ROUTER_LLM_MODEL") or settings.intent_llm_model,
        help="路由模型，默认 EVAL_ROUTER_LLM_MODEL 或 INTENT_LLM_MODEL",
    )
    parser.add_argument("--interval-seconds", type=float, default=0.0, help="请求之间的间隔")
    args = parser.parse_args()
    if args.limit < 0 or args.interval_seconds < 0:
        parser.error("--limit 和 --interval-seconds 必须非负")
    if not args.gold.is_file():
        parser.error(f"gold 不存在：{args.gold}")
    records = _load_jsonl(args.gold)
    if args.limit:
        records = records[: args.limit]
    if not records:
        parser.error("gold 为空")
    result = asyncio.run(_run(records, model=args.model, interval_seconds=args.interval_seconds))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
