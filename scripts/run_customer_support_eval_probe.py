"""运行客服评测账号的有事实在线探针。

该脚本读取 ``seed_customer_support_eval_account.py`` 生成的 manifest，使用运行时
登录得到的 JWT 调用真实 ``/api/v1/chat/stream``。它不把答案当金标，只检查几个
可验证的契约：本人订单应可见、虚假/他人订单不得泄露、多候选时应要求选择、
查不到时不得编造，并记录实际工具调用供人工复核。

用法：

    export EVAL_CUSTOMER_PASSWORD='创建账号时使用的密码'
    PYTHONPATH=src .venv/bin/python scripts/run_customer_support_eval_probe.py \
        --manifest /tmp/customer-support-eval-fixtures.json \
        --base-url http://127.0.0.1:8000 \
        --json-out /tmp/customer-support-eval-probe.json

输出文件包含合成账号的回答和工具名，不包含 JWT 或密码；默认 stdout 只打印短汇总。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("synthetic_only") is not True:
        raise ValueError("manifest 必须来自 seed_customer_support_eval_account.py，且 synthetic_only=true")
    account = value.get("account")
    cases = value.get("cases")
    if not isinstance(account, dict) or not str(account.get("username") or "").strip():
        raise ValueError("manifest 缺少 account.username")
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest 缺少 cases")
    return value


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith("data: "):
        return None
    try:
        value = json.loads(line[6:].strip())
    except json.JSONDecodeError:
        return {"event": "malformed"}
    return value if isinstance(value, dict) else {"event": "malformed"}


def _check_contract(answer: str, tool_names: list[str], contract: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for needle in contract.get("must_contain", []):
        if str(needle) not in answer:
            failures.append(f"缺少文本:{needle}")
    for needles_key, label in (("must_contain_one_of", "至少一个文本"), ("must_have_one_of", "至少一个提示")):
        needles = [str(item) for item in contract.get(needles_key, [])]
        if needles and not any(item in answer for item in needles):
            failures.append(f"{label}:{'|'.join(needles)}")
    for needle in contract.get("must_not_contain", []):
        if str(needle) in answer:
            failures.append(f"出现禁止文本:{needle}")
    for tool in contract.get("forbid_tools", ["create_ticket"]):
        if str(tool) in tool_names:
            failures.append(f"调用了禁止工具:{tool}")
    return not failures, failures


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"评测账号登录失败 HTTP_{response.status_code}")
    payload = response.json()
    token = str(payload.get("token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise RuntimeError("登录响应缺少 token")
    return token


async def _run_case(client: httpx.AsyncClient, token: str, case: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    answer_parts: list[str] = []
    tool_names: set[str] = set()
    error: str | None = None
    status_code: int | None = None
    try:
        async with client.stream(
            "POST",
            "/api/v1/chat/stream",
            headers={"Authorization": f"Bearer {token}"},
            json={"query": str(case.get("query") or "")},
        ) as response:
            status_code = response.status_code
            if status_code != 200:
                await response.aread()
                error = f"HTTP_{status_code}"
            else:
                async for line in response.aiter_lines():
                    event = _parse_sse_line(line)
                    if event is None:
                        continue
                    if event.get("event") == "tool_call" and event.get("name"):
                        tool_names.add(str(event["name"]))
                    if event.get("event") == "token":
                        answer_parts.append(str(event.get("content") or ""))
                    if event.get("event") == "error":
                        error = str(event.get("code") or "STREAM_ERROR")
    except (httpx.HTTPError, TimeoutError) as exc:
        error = type(exc).__name__

    answer = "".join(answer_parts)
    contract_value = case.get("contract")
    contract: dict[str, Any] = contract_value if isinstance(contract_value, dict) else {}
    passed, failures = _check_contract(answer, sorted(tool_names), contract)
    if error:
        passed = False
        failures.insert(0, f"传输错误:{error}")
    return {
        "id": str(case.get("id") or ""),
        "query": str(case.get("query") or ""),
        "status_code": status_code,
        "answer": answer,
        "tool_names": sorted(tool_names),
        "passed": passed,
        "failures": failures,
        "error": error,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


async def _run(args: argparse.Namespace, manifest: dict[str, Any], password: str) -> dict[str, Any]:
    username = str(manifest["account"]["username"])
    cases = [item for item in manifest["cases"] if isinstance(item, dict)]
    if args.limit:
        cases = cases[: args.limit]
    limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=timeout, limits=limits) as client:
        token = await _login(client, username, password)
        results: list[dict[str, Any]] = []
        for index, case in enumerate(cases, start=1):
            result = await _run_case(client, token, case)
            results.append(result)
            print(f"[{index}/{len(cases)}] {result['id']} {'PASS' if result['passed'] else 'FAIL'}")
            if index < len(cases) and args.interval_seconds:
                await asyncio.sleep(args.interval_seconds)

    passed = sum(1 for result in results if result["passed"])
    return {
        "evaluation_kind": "grounded_customer_support_probe",
        "manifest": str(args.manifest),
        "username": username,
        "synthetic_only": True,
        "evaluated": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--password-env", default="EVAL_CUSTOMER_PASSWORD")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--interval-seconds", type=float, default=6.5)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 0 or args.interval_seconds < 0 or args.timeout <= 0:
        parser.error("limit must be non-negative, interval must be non-negative and timeout must be positive")
    if not args.manifest.is_file():
        parser.error(f"manifest does not exist: {args.manifest}")
    password = os.environ.get(args.password_env, "")
    if not password:
        parser.error(f"请设置 {args.password_env}")
    try:
        manifest = _load_manifest(args.manifest)
        result = asyncio.run(_run(args, manifest, password))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"evaluated": result["evaluated"], "passed": result["passed"], "failed": result["failed"]},
            ensure_ascii=False,
        )
    )
    print(f"详细结果已写入 {args.json_out}")
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
