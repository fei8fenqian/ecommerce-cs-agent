"""运行退款 Intent / Router 的 Pre-RAG OFF/ON 对照实验。

本实验只评估 Query -> Pre-RAG -> IntentRouter，不执行订单、退款或其他业务工具。
Gold 必须是人工审核后的新格式 ``refund-gold-100.jsonl``；脚本不调用模型生成 Gold。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from agent.llm.intent_router import IntentRouter
from agent.llm.llm_client import LLMClient
from agent.rag.knowledge_context import format_knowledge_context
from agent.rag.retrieve import pre_retrieve_knowledge
from agent.rag.runtime_manifest import load_runtime_knowledge_sources
from agent.support_control import resolve_workflow
from config import settings
from infra.circuit_breaker import CircuitBreaker


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(records) != 100:
        raise ValueError(f"Gold 必须包含 100 条，实际为 {len(records)} 条")
    return records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _git_dirty() -> bool:
    try:
        return bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return True


_SPEECH_ACTS = (
    "INFORMATION_QUERY",
    "ACTION_REQUEST",
    "STATEMENT",
    "CLARIFICATION_NEEDED",
    "FUTURE_INTENTION",
    "ACKNOWLEDGEMENT",
)


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


def _gold_requests(record: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"domain": str(item["domain"]), "operation": str(item["operation"])}
        for item in record.get("expected_requests", [])
    ]


def _predicted_requests(intent: Any) -> list[dict[str, str]]:
    # support_requests 会遵守 speech_act 的“非行动型不形成业务请求”语义。
    return [{"domain": request.domain, "operation": request.operation} for request in intent.support_requests]


def _resolved_workflow(request: dict[str, str] | None) -> str | None:
    if request is None:
        return None
    workflow = resolve_workflow(request)
    return workflow.key if workflow is not None else None


def _goal_correct(gold_requests: list[dict[str, str]], predicted_requests: list[dict[str, str]]) -> bool:
    if not gold_requests:
        return not predicted_requests
    return bool(predicted_requests) and predicted_requests[0] == gold_requests[0]


async def _run(
    records: list[dict[str, Any]],
    *,
    model: str,
    pre_rag_enabled: bool,
    interval_seconds: float,
    similarity_threshold: float,
) -> dict[str, Any]:
    router = IntentRouter(_client(model))
    rows: list[dict[str, Any]] = []
    errors = 0
    retrieval_errors = 0

    for index, record in enumerate(records, start=1):
        query = str(record.get("query") or "")
        history = record.get("history") if isinstance(record.get("history"), list) else []
        retrieved_chunks: list[dict[str, Any]] = []
        knowledge_context = ""
        error = ""

        if pre_rag_enabled:
            try:
                retrieved_chunks = await pre_retrieve_knowledge(
                    query,
                    top_k=3,
                    similarity_threshold=similarity_threshold,
                )
                knowledge_context = format_knowledge_context(retrieved_chunks, max_docs=3)
            except Exception as exc:
                retrieval_errors += 1
                raise RuntimeError(f"Pre-RAG 在第 {index} 条失败，实验结果不可比较: {exc}") from exc

        intent = None
        try:
            intent = await router.route(
                query=query,
                history=history,
                knowledge_context=knowledge_context,
            )
        except Exception as exc:  # 单条 LLM 失败保留到产物，不伪装成正确
            errors += 1
            error = f"{type(exc).__name__}: {exc}"[:500]

        gold_requests = _gold_requests(record)
        predicted_requests = _predicted_requests(intent) if intent is not None else []
        predicted_speech_act = intent.speech_act if intent is not None else ""
        predicted_workflow = _resolved_workflow(predicted_requests[0] if predicted_requests else None)
        expected_workflow = record.get("expected_workflow")
        gold_needs_clarification = bool(record.get("needs_clarification"))
        predicted_needs_clarification = predicted_speech_act == "CLARIFICATION_NEEDED"
        goal_correct = _goal_correct(gold_requests, predicted_requests) if not error else False

        rows.append(
            {
                "case_id": record.get("case_id"),
                "gold": {
                    "speech_act": record.get("speech_act"),
                    "expected_requests": gold_requests,
                    "primary_goal": record.get("primary_goal"),
                    "expected_workflow": expected_workflow,
                    "needs_clarification": gold_needs_clarification,
                    "notes": record.get("notes", ""),
                },
                "prediction": (
                    {
                        "speech_act": predicted_speech_act,
                        "requests": predicted_requests,
                        "primary_goal": (
                            f"{predicted_requests[0]['domain']}.{predicted_requests[0]['operation']}"
                            if predicted_requests
                            else None
                        ),
                        "resolved_workflow": predicted_workflow,
                        "needs_clarification": predicted_needs_clarification,
                        "route_source": intent.route_source,
                        "confidence": intent.confidence,
                        "goal_modifier": intent.goal_modifier,
                        "ambiguities": intent.ambiguities,
                    }
                    if intent is not None
                    else None
                ),
                "route_source": intent.route_source if intent is not None else "error",
                "retrieved_chunks": [
                    {
                        "source": str(doc.get("source") or ""),
                        "title": str(doc.get("title") or ""),
                        "similarity": float(doc.get("score") or 0.0),
                    }
                    for doc in retrieved_chunks
                ],
                "speech_act_match": bool(intent is not None and predicted_speech_act == record.get("speech_act")),
                "goal_match": goal_correct,
                "goal_sequence_match": bool(not error and predicted_requests == gold_requests),
                "workflow_route_match": bool(
                    not error and expected_workflow is not None and predicted_workflow == expected_workflow
                ),
                "clarification_match": bool(not error and predicted_needs_clarification == gold_needs_clarification),
                "error": error,
            }
        )
        route = rows[-1]["prediction"]
        route_key = route["primary_goal"] if route else "error"
        print(f"[{index}/{len(records)}] {record.get('case_id')} -> {route_key}", flush=True)
        if interval_seconds:
            await asyncio.sleep(interval_seconds)

    evaluated = [row for row in rows if not row["error"]]
    speech_rows = evaluated
    goal_rows = [row for row in evaluated if row["gold"]["expected_requests"]]
    no_request_rows = [row for row in evaluated if not row["gold"]["expected_requests"]]
    workflow_rows = [row for row in evaluated if row["gold"]["expected_workflow"] is not None]
    speech_act_recall = {}
    for speech_act in _SPEECH_ACTS:
        class_rows = [row for row in evaluated if row["gold"]["speech_act"] == speech_act]
        correct = sum(row["speech_act_match"] for row in class_rows)
        speech_act_recall[speech_act] = {
            "correct": correct,
            "total": len(class_rows),
            "recall": correct / len(class_rows) if class_rows else None,
        }
    summary = {
        "total": len(rows),
        "evaluated": len(evaluated),
        "errors": errors,
        "retrieval_errors": retrieval_errors,
        "speech_act_accuracy": (
            sum(row["speech_act_match"] for row in speech_rows) / len(speech_rows) if speech_rows else 0.0
        ),
        "primary_goal_accuracy": (sum(row["goal_match"] for row in goal_rows) / len(goal_rows) if goal_rows else None),
        "workflow_route_accuracy": (
            sum(row["workflow_route_match"] for row in workflow_rows) / len(workflow_rows) if workflow_rows else None
        ),
        "clarification_accuracy": (
            sum(row["clarification_match"] for row in speech_rows) / len(speech_rows) if speech_rows else 0.0
        ),
        "speech_act_recall": speech_act_recall,
        "no_business_request_accuracy": (
            sum(row["goal_match"] for row in no_request_rows) / len(no_request_rows) if no_request_rows else None
        ),
        "no_business_request_cases": len(no_request_rows),
        "goal_scored_cases": len(goal_rows),
        "workflow_scored_cases": len(workflow_rows),
        "request_sequence_accuracy": (
            sum(row["goal_sequence_match"] for row in goal_rows) / len(goal_rows) if goal_rows else None
        ),
        "request_free_accuracy": (
            sum(row["goal_match"] for row in no_request_rows) / len(no_request_rows) if no_request_rows else None
        ),
        "route_source_distribution": dict(Counter(row["route_source"] for row in rows)),
    }
    return {
        "evaluation_kind": "jddc_refund_intent_router_ab",
        "model": model,
        "temperature": 0.0,
        "pre_rag_enabled": pre_rag_enabled,
        "pre_rag_similarity_threshold": similarity_threshold,
        "agent_execution": False,
        "tool_execution": False,
        "write_actions_executed": False,
        "summary": summary,
        "results": rows,
    }


def _add_help_harm(off: dict[str, Any], on: dict[str, Any]) -> dict[str, int]:
    off_rows = {row["case_id"]: row for row in off["results"]}
    on_rows = {row["case_id"]: row for row in on["results"]}
    common_ids = sorted(set(off_rows) & set(on_rows))
    help_count = 0
    harm_count = 0
    for case_id in common_ids:
        off_correct = bool(off_rows[case_id]["goal_match"])
        on_correct = bool(on_rows[case_id]["goal_match"])
        help_count += int(not off_correct and on_correct)
        harm_count += int(off_correct and not on_correct)
    return {"help": help_count, "harm": harm_count, "comparable_cases": len(common_ids)}


async def _main(args: argparse.Namespace) -> int:
    gold_path = args.gold
    records = _load_jsonl(gold_path)
    runtime_sources = sorted(load_runtime_knowledge_sources())
    print(f"Runtime Knowledge sources: {len(runtime_sources)}")
    print(f"Gold SHA256: {_sha256(gold_path)}")
    result = await _run(
        records,
        model=args.model,
        pre_rag_enabled=args.pre_rag == "on",
        interval_seconds=args.interval_seconds,
        similarity_threshold=args.similarity_threshold,
    )
    result["gold_path"] = str(gold_path)
    result["gold_sha256"] = _sha256(gold_path)
    result["git_commit"] = _git_commit()
    result["working_tree_dirty"] = _git_dirty()
    result["runtime_knowledge_sources"] = runtime_sources
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--pre-rag", choices=("off", "on"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument("--similarity-threshold", type=float, default=settings.pre_rag_similarity_threshold)
    args = parser.parse_args()
    if args.interval_seconds < 0 or not 0.0 <= args.similarity_threshold <= 1.0:
        parser.error("interval 和 similarity threshold 参数无效")
    if not args.gold.is_file():
        parser.error(f"Gold 不存在: {args.gold}")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
