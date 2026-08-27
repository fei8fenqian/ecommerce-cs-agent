"""在运行中的客服 API 上执行真实 Agent 流，并记录可观察轨迹。

与 ``run_*_support_scan.py`` 不同，本脚本会真正请求 ``/api/v1/chat/stream``，因此会调用
当前配置的意图路由、RAG/Agent 和工具。它只从 SSE 事件记录工具名称、最终动作和错误类型，
不会保存工具参数、完整回复或客户身份信息。默认每条样本使用独立会话，避免评测样本互相污染。

运行前请使用开发/测试环境，不要指向生产；包含退款、人工或异常语句时可能创建工单等业务副作用，
因此必须显式传 ``--allow-side-effects``：

    EVAL_AUTH_TOKEN='开发环境 JWT' \\
    PYTHONPATH=src .venv/bin/python scripts/run_customer_support_agent_trace_eval.py \\
      --source /tmp/jddc-3c-independent-selection.jsonl \\
      --base-url http://127.0.0.1:8000 \\
      --allow-side-effects \\
      --quiet --summary-only \\
      --json-out /tmp/customer-support-agent-trace.json

输入中的 ``history`` 只用于说明和输出元数据；公开聊天 API 不接受客户端伪造历史，默认不会重放它。
所以这是一轮“真实入口单轮轨迹评测”，不是多轮会话准确率证明。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from service.customer_support_policy import CustomerSupportAction

_GUIDANCE_MARKERS: tuple[tuple[str, CustomerSupportAction], ...] = (
    ("[前往我的订单申请退款]", CustomerSupportAction.OFFER_REFUND_SELF_SERVICE),
    ("[我的订单查看退款进度]", CustomerSupportAction.SHOW_REFUND_PROGRESS),
    ("[设备故障排查与保修说明]", CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING),
    ("为了避免误建售后工单", CustomerSupportAction.ASK_FOR_CLARIFICATION),
)
_VALID_ACTIONS = {action.value for action in CustomerSupportAction}


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
            if not isinstance(record, dict) or not str(record.get("id") or "").strip():
                raise ValueError(f"{path}:{line_number} 缺少 id")
            if not isinstance(record.get("query"), str) or not record["query"].strip():
                raise ValueError(f"{path}:{line_number} 缺少 query")
            records.append(record)
    return records


def _load_expected_labels(path: Path, field: str) -> dict[str, str]:
    """读取与原话分离的动作标签文件，只按 id 对齐，不读取客户原话。"""

    labels: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} 不是合法 JSON") from exc
            record_id = str(record.get("id") or "").strip() if isinstance(record, dict) else ""
            if not record_id:
                raise ValueError(f"{path}:{line_number} 缺少 id")
            value = record.get(field)
            if value in (None, ""):
                continue
            action = str(value).strip()
            if action not in _VALID_ACTIONS:
                raise ValueError(f"{path}:{line_number} 的 {field} 不是项目动作: {action}")
            if record_id in labels:
                raise ValueError(f"{path}:{line_number} 重复 id={record_id}")
            labels[record_id] = action
    return labels


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data: "):
        return None
    payload = line[6:].strip()
    if not payload:
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {"event": "malformed", "raw_length": len(payload)}
    return value if isinstance(value, dict) else {"event": "malformed"}


def _observed_action(events: list[dict[str, Any]]) -> CustomerSupportAction:
    if any(event.get("event") == "tool_call" and event.get("name") == "create_ticket" for event in events):
        return CustomerSupportAction.CREATE_TICKET
    answer = "".join(str(event.get("content") or "") for event in events if event.get("event") == "token")
    for marker, action in _GUIDANCE_MARKERS:
        if marker in answer:
            return action
    return CustomerSupportAction.CONTINUE


def _safe_tool_names(events: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {str(event.get("name")) for event in events if event.get("event") == "tool_call" and event.get("name")}
    )


async def _run_case(
    client: httpx.AsyncClient,
    record: dict[str, Any],
    *,
    expected_field: str | None,
    expected_labels: dict[str, str] | None,
    index: int,
) -> dict[str, Any]:
    case_id = str(record["id"])
    started = time.perf_counter()
    # raw_events 仅在当前进程内用于推断动作；评测产物不保存模型全文或客户内容。
    raw_events: list[dict[str, Any]] = []
    safe_events: list[dict[str, Any]] = []
    error: str | None = None
    status_code: int | None = None
    try:
        async with client.stream(
            "POST",
            "/api/v1/chat/stream",
            # 不传 session_id：每个独立样本由服务端创建新的标准 UUID 会话。
            json={"query": str(record["query"])},
        ) as response:
            status_code = response.status_code
            if status_code != 200:
                body = await response.aread()
                error = f"HTTP_{status_code}"
                try:
                    payload = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(payload, dict):
                        error = str(payload.get("error", {}).get("code") or error)
                except json.JSONDecodeError:
                    pass
            else:
                async for line in response.aiter_lines():
                    event = _parse_sse_line(line)
                    if event is not None:
                        # 只保留评测需要的结构化事件，避免把客户原文/模型全文写入评测产物。
                        safe_event: dict[str, Any] = {"event": str(event.get("event") or "")}
                        if event.get("name"):
                            safe_event["name"] = str(event["name"])
                        if event.get("code"):
                            safe_event["code"] = str(event["code"])
                        if event.get("event") == "done":
                            safe_event["has_answer"] = bool(str(event.get("answer") or ""))
                            safe_event["total_steps"] = int(event.get("total_steps") or 0)
                        if event.get("event") == "token":
                            safe_event["content"] = str(event.get("content") or "")
                        raw_events.append(event)
                        safe_event.pop("content", None)
                        if event.get("event") == "token":
                            safe_event["content_length"] = len(str(event.get("content") or ""))
                        safe_events.append(safe_event)
    except (httpx.HTTPError, TimeoutError):
        error = "HTTP_CLIENT_ERROR"
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    observed = _observed_action(raw_events) if error is None else None
    expected_value = (
        expected_labels.get(case_id)
        if expected_labels is not None
        else record.get(expected_field)
        if expected_field
        else None
    )
    expected = str(expected_value).strip() if expected_value not in (None, "") else None
    observed_action = observed.value if observed is not None else None
    result: dict[str, Any] = {
        "id": case_id,
        "status_code": status_code,
        "observed_action": observed_action,
        "expected_action": expected,
        "proxy_agreement": expected == observed_action if expected else None,
        "tool_names": _safe_tool_names(raw_events),
        "event_names": [str(event.get("event") or "") for event in safe_events],
        "error": error,
        "duration_ms": elapsed_ms,
        "history_turns_available": len(record.get("history") or []),
        "history_replayed": False,
    }
    return result


async def _run(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    token: str,
    expected_labels: dict[str, str] | None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        limits=limits,
    ) as client:
        results: list[dict[str, Any]] = []
        for index, record in enumerate(records, start=1):
            result = await _run_case(
                client,
                record,
                expected_field=args.expected_field,
                expected_labels=expected_labels,
                index=index,
            )
            results.append(result)
            if not args.quiet and not args.summary_only:
                print(
                    f"[{index}/{len(records)}] {result['id']} "
                    f"observed={result['observed_action'] or '-'} "
                    f"expected={result['expected_action'] or '-'} "
                    f"error={result['error'] or '-'}"
                )
            if index < len(records) and args.interval_seconds:
                await asyncio.sleep(args.interval_seconds)

    observed_counts = Counter(str(result["observed_action"]) for result in results if result["observed_action"])
    comparable = [result for result in results if result["expected_action"] and not result["error"]]
    agreements = sum(1 for result in comparable if result["proxy_agreement"] is True)
    errors = sum(1 for result in results if result["error"])
    rate_limited = sum(1 for result in results if result["error"] == "RATE_LIMITED")
    return {
        "dataset": str(args.source),
        "evaluation_kind": "online_agent_trace_eval",
        "agent_execution": True,
        "llm_calls": None,
        "source_labels_compared": bool(args.expected_field),
        "expected_label_field": args.expected_field,
        "expected_label_file": str(args.expected_file) if args.expected_file else None,
        "expected_label_provenance": "caller_supplied; not upgraded to gold",
        "history_replayed": False,
        "side_effects_allowed": True,
        "interval_seconds": args.interval_seconds,
        "evaluated": len(results),
        "completed_without_transport_error": len(results) - errors,
        "transport_error_count": errors,
        "rate_limited_count": rate_limited,
        "observed_actions": dict(sorted(observed_counts.items())),
        "comparable": len(comparable),
        "agreement_count": agreements,
        "proxy_agreement": agreements / len(comparable) if comparable else None,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="带 id/query 的 JSONL 标注或案例文件")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="正在运行的 API 根地址")
    parser.add_argument("--token", default=None, help="JWT；不传则读取 EVAL_AUTH_TOKEN")
    parser.add_argument("--expected-field", default=None, help="可选动作字段，例如 human_gold_action")
    parser.add_argument(
        "--expected-file",
        type=Path,
        help="可选：与 source 分离的标签 JSONL，按 id 对齐（默认字段 human_gold_action）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多执行多少条；0 表示全部")
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=6.5,
        help="请求之间的间隔；默认 6.5 秒以避开聊天接口每分钟 10 次限制，传 0 可关闭",
    )
    parser.add_argument("--timeout", type=float, default=90.0, help="单条 HTTP 超时秒数")
    parser.add_argument("--allow-side-effects", action="store_true", help="确认在开发/测试环境允许创建工单等副作用")
    parser.add_argument("--quiet", action="store_true", help="不逐条打印结果；详细结果仍写入 --json-out")
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="stdout 只打印汇总；详细逐条轨迹必须通过 --json-out 保存",
    )
    parser.add_argument("--json-out", type=Path, help="可选 JSON 输出路径")
    args = parser.parse_args()
    if args.limit < 0 or args.timeout <= 0 or args.interval_seconds < 0:
        parser.error("limit must be non-negative, timeout must be positive and interval must be non-negative")
    if args.summary_only and not args.json_out:
        parser.error("--summary-only 需要同时传 --json-out 保存逐条轨迹")
    if not args.allow_side_effects:
        parser.error("真实 Agent 评测可能写入工单；请确认使用开发/测试环境后传 --allow-side-effects")
    if not args.source.is_file():
        parser.error(f"source does not exist: {args.source}")
    if args.expected_file and not args.expected_file.is_file():
        parser.error(f"expected file does not exist: {args.expected_file}")
    token = args.token or os.environ.get("EVAL_AUTH_TOKEN", "").strip()
    if not token:
        parser.error("请传 --token 或设置 EVAL_AUTH_TOKEN")
    try:
        records = _load_jsonl(args.source)
        expected_labels = None
        if args.expected_file:
            args.expected_field = args.expected_field or "human_gold_action"
            expected_labels = _load_expected_labels(args.expected_file, args.expected_field)
            source_ids = {str(record["id"]) for record in records}
            if not source_ids & expected_labels.keys():
                parser.error("source 与 expected-file 没有可对齐的 id")
        elif args.expected_field:
            for record in records:
                expected = record.get(args.expected_field)
                if expected not in (None, "") and str(expected).strip() not in _VALID_ACTIONS:
                    parser.error(
                        f"id={record['id']} 的 {args.expected_field} 不是项目动作；"
                        "请确认没有把外部数据集原始标签当作 expected_action"
                    )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.limit:
        records = records[: args.limit]
    if not records:
        parser.error("source contains no records")
    result = asyncio.run(_run(args, records, token, expected_labels))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.summary_only:
        summary_keys = (
            "dataset",
            "evaluation_kind",
            "evaluated",
            "completed_without_transport_error",
            "transport_error_count",
            "rate_limited_count",
            "observed_actions",
            "comparable",
            "agreement_count",
            "proxy_agreement",
        )
        print(json.dumps({key: result[key] for key in summary_keys}, ensure_ascii=False, indent=2))
    else:
        print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
