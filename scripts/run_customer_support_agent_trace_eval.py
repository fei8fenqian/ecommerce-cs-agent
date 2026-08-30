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

默认情况下，输入中的 ``history`` 只用于说明和输出元数据；公开聊天 API 不接受客户端伪造历史。
要做真实多轮 replay，必须显式指定测试账号和临时服务端 session：

    EVAL_AUTH_TOKEN='开发环境 JWT' \\
    PYTHONPATH=src .venv/bin/python scripts/run_customer_support_agent_trace_eval.py \\
      --source /tmp/jddc-3c-independent-selection.jsonl \\
      --base-url http://127.0.0.1:8000 \\
      --allow-side-effects --replay-history --owner-user-id 73 \\
      --limit 30 --quiet --summary-only \\
      --json-out /tmp/customer-support-agent-trace-replayed.json

replay 模式由评测进程把 ``history`` 写入临时服务端 session，然后请求只发送 ``session_id`` 和最后一句
``query``；每条样本完成后删除该临时 session。因此 replay 结果才是多轮入口轨迹评测，未指定 replay
时仍是单轮对照组。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from service.customer_support_policy import CustomerSupportAction

_logger = logging.getLogger(__name__)

_GUIDANCE_MARKERS: tuple[tuple[str, CustomerSupportAction], ...] = (
    ("[前往我的订单申请退款]", CustomerSupportAction.OFFER_REFUND_SELF_SERVICE),
    ("[我的订单查看退款进度]", CustomerSupportAction.SHOW_REFUND_PROGRESS),
    ("[设备故障排查与保修说明]", CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING),
    ("为了避免误建售后工单", CustomerSupportAction.ASK_FOR_CLARIFICATION),
)
_VALID_ACTIONS = {action.value for action in CustomerSupportAction}
_REPLAY_ROLES = {"user", "assistant"}


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


def _history_messages_for_replay(record: dict[str, Any]) -> list[dict[str, str]]:
    """Validate and normalize the prefix history before writing a test session.

    JDDC records contain only the conversation prefix.  The target user turn is
    deliberately not part of this list; the HTTP request appends it as the next
    user message.  ``system`` and ``tool`` messages are not accepted because
    they are not source turns in this replay format and could change the agent's
    trusted instruction/tool boundary.
    """

    raw_history = record.get("history")
    if raw_history in (None, []):
        return []
    if not isinstance(raw_history, list):
        raise ValueError(f"id={record.get('id')} 的 history 必须是数组")

    history: list[dict[str, str]] = []
    for turn_index, raw_turn in enumerate(raw_history):
        if not isinstance(raw_turn, dict):
            raise ValueError(f"id={record.get('id')} 的 history[{turn_index}] 必须是对象")
        role = str(raw_turn.get("role") or "").strip()
        content = raw_turn.get("content")
        if role not in _REPLAY_ROLES:
            raise ValueError(f"id={record.get('id')} 的 history[{turn_index}] role={role!r} 不在 user/assistant 范围内")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"id={record.get('id')} 的 history[{turn_index}] 缺少 content")
        history.append({"role": role, "content": content})
    return history


async def _create_replay_session(record: dict[str, Any], owner_user_id: int) -> str:
    """Create a temporary server-side session containing only the JDDC prefix."""

    from store.session_store import append_messages, create_session

    history = _history_messages_for_replay(record)
    session = await create_session(owner_user_id, title="JDDC eval replay")
    session_id = str(session["id"])
    try:
        if history:
            await append_messages(
                session_id,
                owner_user_id,
                history,
                title="JDDC eval replay",
            )
    except Exception:
        from store.session_store import delete_session

        await delete_session(session_id, owner_user_id)
        raise
    return session_id


async def _delete_replay_session(session_id: str, owner_user_id: int) -> None:
    """Delete only a session created by this evaluator; cleanup is best effort."""

    from store.session_store import delete_session

    try:
        deleted = await delete_session(session_id, owner_user_id)
        if not deleted:
            _logger.warning("replay session cleanup returned false")
    except Exception:
        # A cleanup failure must not rewrite an otherwise valid model trace, but
        # it is visible in stderr so the test database can be inspected.
        _logger.warning("replay session cleanup failed", exc_info=True)


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


def _validate_replay_database() -> None:
    """Keep temporary replay sessions out of development and production DBs."""

    from config import settings

    if settings.env == "prod":
        raise ValueError("history replay 禁止在 prod 环境运行")
    if not settings.pg_dbname.endswith("_test"):
        raise ValueError(f"history replay 只允许写入 *_test 数据库，当前 PG_DBNAME={settings.pg_dbname!r}")


async def _run_case(
    client: httpx.AsyncClient,
    record: dict[str, Any],
    *,
    expected_field: str | None,
    expected_labels: dict[str, str] | None,
    index: int,
    replay_history: bool = False,
    owner_user_id: int | None = None,
    include_answer: bool = False,
) -> dict[str, Any]:
    case_id = str(record["id"])
    started = time.perf_counter()
    # raw_events 仅在当前进程内用于推断动作；评测产物不保存模型全文或客户内容。
    raw_events: list[dict[str, Any]] = []
    safe_events: list[dict[str, Any]] = []
    error: str | None = None
    status_code: int | None = None
    replay_session_id: str | None = None
    server_session_id: str | None = None
    try:
        if replay_history:
            if owner_user_id is None:
                raise ValueError("history replay requires owner_user_id")
            replay_session_id = await _create_replay_session(record, owner_user_id)

        payload: dict[str, str] = {"query": str(record["query"])}
        if replay_session_id is not None:
            payload["session_id"] = replay_session_id
        async with client.stream(
            "POST",
            "/api/v1/chat/stream",
            # Replay mode sends only a server-created session id.  The API
            # still loads history from PostgreSQL and never accepts raw client
            # history.
            json=payload,
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
                        if event.get("event") == "start" and event.get("session_id"):
                            server_session_id = str(event["session_id"])
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
    finally:
        cleanup_session_id = replay_session_id or server_session_id
        if cleanup_session_id is not None and owner_user_id is not None:
            await _delete_replay_session(cleanup_session_id, owner_user_id)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    observed = _observed_action(raw_events) if error is None else None
    answer = "".join(str(event.get("content") or "") for event in raw_events if event.get("event") == "token")
    if not answer:
        answer = next(
            (str(event.get("answer") or "") for event in raw_events if event.get("event") == "done"),
            "",
        )
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
        "history_replayed": replay_history,
    }
    if include_answer:
        result["answer"] = answer
    return result


async def _run(
    args: argparse.Namespace,
    records: list[dict[str, Any]],
    token: str,
    expected_labels: dict[str, str] | None,
    *,
    owner_user_id: int | None = None,
    include_answer: bool = False,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(args.timeout)
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    replay_pool_open = False
    if args.replay_history or owner_user_id is not None:
        from infra.db_pool import init_pool

        await init_pool(minconn=1, maxconn=2)
        replay_pool_open = True

    try:
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
                    replay_history=args.replay_history,
                    owner_user_id=owner_user_id,
                    include_answer=include_answer,
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
    finally:
        if replay_pool_open:
            from infra.db_pool import close_pool

            await close_pool()

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
        "history_replayed": args.replay_history,
        "answers_included": include_answer,
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
    parser.add_argument(
        "--replay-history",
        action="store_true",
        help="把每条记录的 history 写入临时服务端 session，再发送 query（仅开发/测试环境）",
    )
    parser.add_argument(
        "--owner-user-id",
        type=int,
        help="评测 session 所属客户用户 ID；用于 replay 写入和清理，必须与 JWT 所属用户一致",
    )
    parser.add_argument(
        "--include-answer",
        action="store_true",
        help="显式保存 Agent 完整回复；仅用于脱敏测试样本，默认不保存",
    )
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
    if args.replay_history and args.owner_user_id is None:
        parser.error("--replay-history 需要同时传 --owner-user-id，并确保它与 JWT 所属客户一致")
    if args.replay_history and args.owner_user_id <= 0:
        parser.error("--owner-user-id 必须为正整数")
    if args.replay_history:
        try:
            _validate_replay_database()
        except ValueError as exc:
            parser.error(str(exc))
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
    result = asyncio.run(
        _run(
            args,
            records,
            token,
            expected_labels,
            owner_user_id=args.owner_user_id,
            include_answer=args.include_answer,
        )
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.summary_only:
        summary_keys = (
            "dataset",
            "evaluation_kind",
            "history_replayed",
            "answers_included",
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
