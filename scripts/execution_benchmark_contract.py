"""Execution benchmark 的输入/评估 payload 隔离契约。

该模块供 harness 使用：生产执行只接收 case input，Gold 只在执行结束后交给 evaluator。
"""

from __future__ import annotations

from typing import Any, Literal

_FORBIDDEN_EXECUTION_KEYS = {
    "expected",
    "must_include_claims",
    "must_not_include_claims",
    "expected_facts",
    "expected_block_reason",
}


def validate_claim_assertions(case: dict[str, Any]) -> None:
    """禁止 substring matcher 下 must/include 与 must/not 互相矛盾。"""
    expected = case.get("expected")
    if not isinstance(expected, dict):
        raise AssertionError(f"case {case.get('id')} 缺少 expected")
    must_include = [str(item) for item in expected.get("must_include_claims", [])]
    must_not_include = [str(item) for item in expected.get("must_not_include_claims", [])]
    conflicts = [
        (positive, negative)
        for positive in must_include
        for negative in must_not_include
        if positive in negative or negative in positive
    ]
    if conflicts:
        raise AssertionError(f"case {case.get('id')} 存在 claim substring 冲突: {conflicts}")


def validate_cases_contract(cases: list[dict[str, Any]]) -> None:
    """对整套 cases 做执行输入与 outcome claim 的静态校验。"""
    for case in cases:
        validate_claim_assertions(case)
        execution_payload(case, pass_name="router")


def execution_payload(
    case: dict[str, Any],
    *,
    pass_name: Literal["oracle", "router"],
) -> dict[str, Any]:
    """构造传入生产执行链的 payload，并断言 Gold 字段没有泄漏。"""
    payload = {key: case[key] for key in ("id", "query", "history", "role", "fixture") if key in case}
    if pass_name == "oracle":
        payload["oracle_intent"] = case["oracle"]
    assert_execution_payload_isolated(payload)
    return payload


def evaluator_payload(case: dict[str, Any]) -> dict[str, Any]:
    """构造执行结束后使用的 Gold payload，不可传给 Router 或 Workflow。"""
    return {"id": case["id"], "oracle": case["oracle"], "expected": case["expected"]}


def assert_execution_payload_isolated(payload: dict[str, Any]) -> None:
    """供 harness 在调用 /chat 前显式检查 payload 仍未带入 Gold。"""
    leaked = set(payload) & _FORBIDDEN_EXECUTION_KEYS
    if leaked:
        raise AssertionError(f"evaluation Gold 泄漏到 execution payload: {sorted(leaked)}")
