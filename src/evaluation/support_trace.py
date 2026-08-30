"""第一阶段客服 Agent dry-run Trace 与 Gold 对比。

本模块只测试 Agent 是否识别目标、缺失事实和解决路径。它不会执行 ToolRegistry，
更不会触发订单、退款、工单等业务写入。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from agent.llm.llm_client import LLMClient
from evaluation.support_ontology import FACTS, build_plan_for_facts, ontology_for_prompt, validate_plan

_CAPABILITIES = (
    "identify_order",
    "identify_service_order",
    "query_order",
    "query_after_sales",
    "query_logistics",
    "query_payment",
    "query_refund",
    "query_price_protection",
    "query_product_knowledge",
    "query_stock",
    "ask_customer",
    "provide_guidance",
    "escalate_to_human",
    "propose_write_action",
)
_WRITE_CAPABILITIES = {"propose_write_action"}
_RESOLVED_STATUSES = {"COMPLETED", "RESOLVED"}
_PLAN_PATHS = {"happy_path", "fallback"}


def _gold_goal(target: dict[str, Any]) -> str:
    """读取 v2 旧版对象目标和退款标注包的字符串目标。"""

    value = target.get("user_goal")
    if isinstance(value, dict):
        return str(value.get("primary") or "")
    return str(value or "")


def _gold_capabilities(target: dict[str, Any]) -> list[str]:
    """兼容 expected_capabilities 与 capability-level required_capabilities。"""

    values = target.get("required_capabilities")
    if values is None:
        values = target.get("expected_capabilities", [])
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for item in values:
        if isinstance(item, dict):
            value = item.get("capability")
        else:
            value = item
        if isinstance(value, str) and value.strip():
            canonical = _canonical_capability(value)
            # 多个业务能力可以由同一个实现能力提供（例如一次退款查询同时
            # 返回记录、状态和金额）。顺序评估应比较能力链，而不是把同一
            # 个归一化能力重复计入。
            if canonical not in result:
                result.append(canonical)
    return result


def _gold_escalation_required(target: dict[str, Any]) -> bool | None:
    """返回确定性的升级标签；条件性案例不计入二值升级准确率。"""

    value = target.get("escalation")
    if isinstance(value, dict):
        required = value.get("required")
        return required if isinstance(required, bool) else None
    value = target.get("escalation_required")
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"是", "yes", "true", "required", "必需"}:
        return True
    if normalized in {"否", "no", "false", "not_required", "不需要"}:
        return False
    return None


def _json_object(content: str) -> dict[str, Any] | None:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _canonical_capability(value: str) -> str:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    if "escalat" in normalized or "human_handoff" in normalized or "人工" in value:
        return "escalate_to_human"
    if any(marker in normalized for marker in ("modify_", "submit_", "create_", "cancel_", "write_")):
        return "propose_write_action"
    if any(marker in normalized for marker in ("ask_", "collect_", "clarif")):
        return "ask_customer"
    if any(marker in normalized for marker in ("guide_", "provide_", "continue_resolution", "summarize_")):
        return "provide_guidance"
    if "price_protection" in normalized or "price_difference" in normalized:
        return "query_price_protection"
    if "payment" in normalized:
        return "query_payment"
    if "refund" in normalized:
        return "query_refund"
    if any(
        marker in normalized for marker in ("shipping", "logistics", "delivery", "pickup", "fulfillment", "dropoff")
    ):
        return "query_logistics"
    if any(
        marker in normalized for marker in ("service_order", "after_sales", "repair", "exchange", "return_eligibility")
    ):
        return "query_after_sales"
    if "stock" in normalized:
        return "query_stock"
    if any(
        marker in normalized for marker in ("product", "compatibility", "warranty", "troubleshoot", "pairing", "power_")
    ):
        return "query_product_knowledge"
    if "order" in normalized:
        return "query_order"
    return normalized


def _canonical_fact(value: str) -> str:
    normalized = value.lower().replace("-", "_").replace(" ", "_")
    if "price_protection" in normalized or "price_difference" in normalized:
        return "price_protection"
    if "payment" in normalized:
        return "payment"
    if "refund" in normalized:
        return "refund"
    if any(
        marker in normalized for marker in ("shipping", "logistics", "delivery", "pickup", "fulfillment", "dropoff")
    ):
        return "logistics"
    if any(
        marker in normalized for marker in ("service_order", "after_sales", "repair", "exchange", "return_eligibility")
    ):
        return "after_sales"
    if any(marker in normalized for marker in ("product", "compatibility", "warranty", "fault", "power_", "device_")):
        return "product_or_device"
    if any(marker in normalized for marker in ("user_problem", "user_invoice", "issue_description")):
        return "customer_clarification"
    if "order" in normalized:
        return "order"
    return normalized


def _relative_order_is_valid(expected: list[str], observed: list[str]) -> bool:
    cursor = 0
    for capability in observed:
        if cursor < len(expected) and capability == expected[cursor]:
            cursor += 1
    return cursor == len(expected)


def _happy_path_capability_order(trace: dict[str, Any]) -> list[str]:
    """从真正的结构化计划取顺序，不使用 selected_capabilities 的展示顺序。"""

    plans = trace.get("plans", [])
    if not isinstance(plans, list):
        return []
    ordered: list[str] = []
    for plan in plans:
        if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
            continue
        for step in plan["steps"]:
            if not isinstance(step, dict):
                continue
            path = str(step.get("path") or "happy_path")
            capability = str(step.get("capability") or "")
            if path == "happy_path" and capability:
                ordered.append(_canonical_capability(capability))
        if ordered:
            break
    return ordered


def _set_metrics(expected: set[str], observed: set[str]) -> dict[str, float | int]:
    matched = len(expected & observed)
    return {
        "expected": len(expected),
        "observed": len(observed),
        "matched": matched,
        "recall": matched / len(expected) if expected else 1.0,
        "precision": matched / len(observed) if observed else (1.0 if not expected else 0.0),
    }


def _goal_similarity(gold_goal: str, parsed_goal: str) -> float:
    """保守的字符 bigram 相似度；报告为 proxy，不能替代人工仲裁。"""

    def grams(value: str) -> set[str]:
        compact = re.sub(r"\s+", "", value)
        pairs = {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}
        return pairs or ({compact} if compact else set())

    left, right = grams(gold_goal), grams(parsed_goal)
    return len(left & right) / len(left | right) if left or right else 0.0


@dataclass(frozen=True)
class TraceGenerationResult:
    trace: dict[str, Any]
    total_tokens: int


class GoalSemanticJudge:
    """以语义判断 Goal 是否等价；Gold 只在评测侧传入，Agent 永远看不到。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def judge(self, gold_goal: str, parsed_goal: str) -> dict[str, Any]:
        prompt = {
            "role": "customer_support_evaluation_judge",
            "task": "判断两个客服目标是否在业务意图和期望解决方向上等价。措辞不同不构成失败。输出 JSON。",
            "gold_goal": gold_goal,
            "agent_goal": parsed_goal,
            "output_schema": {"equivalent": "boolean", "confidence": "0_to_1", "reason": "short_string"},
        }
        response = await self.llm.chat(
            [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            temperature=0.0,
            max_tokens=240,
            extra_body={"thinking": {"type": "disabled"}},
            response_format={"type": "json_object"},
        )
        parsed = _json_object(response.content or "") or {}
        return {
            "value": bool(parsed.get("equivalent")),
            "confidence": float(parsed.get("confidence") or 0.0),
            "reason": str(parsed.get("reason") or ""),
            "method": "llm_semantic_judge",
            "total_tokens": response.usage.total_tokens,
        }


class SupportTraceGenerator:
    """让 LLM 在不见 Gold、不执行工具的情况下生成统一规划 Trace。"""

    def __init__(self, llm: LLMClient):
        self.llm = llm

    async def generate(self, case: dict[str, Any]) -> TraceGenerationResult:
        # 新的退款 Gold 直接把 history/query 放在记录顶层；旧 v2 记录放在 input。
        input_data = case.get("input") or case
        history = input_data.get("history") or []
        prompt = {
            "role": "customer_support_planner",
            "instruction": (
                "仅分析如何解决客服问题，不执行工具、不假设订单或售后事实存在。"
                "输出严格 JSON；capabilities 只能从 capability_catalog 选择。"
                "你只选择为做出业务决策所必需的 required_facts，且必须来自 fact_ontology。"
                "不要输出工具、计划或状态：系统会根据事实的 producer 与依赖关系确定性生成它们。"
                "system_observable 必须由系统查询，绝不能因为尚未查询就转成用户追问；"
                "只有 user_only 才允许 ask_customer。没有真实工具结果时不得声称问题已完成。"
            ),
            "fact_ontology": ontology_for_prompt(),
            "output_schema": {
                "parsed_goal": {"primary": "string", "preferred_resolution": "string"},
                "required_facts": ["fact_ontology.fact"],
                "escalation": {"requested": "boolean", "reason": "string"},
            },
            "conversation": {"history": history, "query": input_data["query"]},
        }
        response = await self.llm.chat(
            [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}],
            temperature=0.0,
            max_tokens=1400,
            extra_body={"thinking": {"type": "disabled"}},
            response_format={"type": "json_object"},
        )
        parsed = _json_object(response.content or "") or {}
        required_fact_ids = [fact for fact in _as_strings(parsed.get("required_facts")) if fact in FACTS]
        fact_requirements = [
            {
                "fact": fact,
                "source": FACTS[fact].source,
                "decision_needed_for": "planner_selected_decision_fact",
            }
            for fact in required_fact_ids
        ]
        plans, selected = build_plan_for_facts(required_fact_ids)
        escalation = parsed.get("escalation") if isinstance(parsed.get("escalation"), dict) else {}
        next_step = plans[0] if plans else {}
        next_capability = str(next_step.get("capability") or "")
        if bool(escalation.get("requested")):
            status = "ESCALATED"
        elif next_capability == "ask_customer":
            status = "AWAITING_CUSTOMER"
        elif next_capability in _WRITE_CAPABILITIES:
            status = "AWAITING_CONFIRMATION"
        else:
            status = "READY_TO_EXECUTE"
        plan_violations = validate_plan(required_fact_ids, plans, next_capability, status)
        policy_checks = [
            {
                "sequence": index,
                "proposal": capability,
                "decision": (
                    "requires_confirmation_no_execution" if capability in _WRITE_CAPABILITIES else "allow_read_or_plan"
                ),
                "reason": (
                    "phase_one_records_a_write_proposal_but_never_executes_it"
                    if capability in _WRITE_CAPABILITIES
                    else "dry_run_capability_only"
                ),
                "user_confirmation_required": capability in _WRITE_CAPABILITIES,
            }
            for index, capability in enumerate(selected, start=1)
        ]
        false_resolution = status in _RESOLVED_STATUSES
        trace = {
            "schema_version": "agent_eval_trace.v2",
            "case_id": str(case["id"]),
            "parsed_goal": (
                parsed.get("parsed_goal")
                if isinstance(parsed.get("parsed_goal"), dict)
                else {"primary": "", "preferred_resolution": ""}
            ),
            "required_facts": [item["fact"] for item in fact_requirements],
            "fact_requirements": fact_requirements,
            "selected_capabilities": selected,
            "plans": [{"revision": 1, "steps": plans}],
            "tool_calls": [
                {
                    "step_id": str(index),
                    "capability": capability,
                    "implementation": "",
                    "arguments": {},
                    "result": {},
                    "status": "planned_not_executed",
                    "error_code": "",
                }
                for index, capability in enumerate(selected, start=1)
            ],
            "state_updates": [
                {
                    "sequence": 1,
                    "state_before": {"verified_facts": []},
                    "facts_added": {},
                    "state_after": {"missing_facts": [item["fact"] for item in fact_requirements]},
                    "reason": "dry_run_plan_only",
                }
            ],
            "policy_checks": policy_checks,
            "evaluator_results": [
                {
                    "sequence": 1,
                    "goal_status": "unresolved" if false_resolution else "planned",
                    "reason": (
                        "dry_run_has_no_verified_facts"
                        if false_resolution
                        else "ontology_plan_validated"
                        if not plan_violations
                        else "plan_validator_replan_required"
                    ),
                    "next_action": "BLOCK_FALSE_RESOLUTION" if false_resolution else next_capability,
                    "plan_violations": plan_violations,
                }
            ],
            "escalation": {
                "requested": bool(escalation.get("requested")),
                "reason": str(escalation.get("reason") or ""),
            },
            "next_action": {
                "step_id": str(next_step.get("step_id") or ""),
                "capability": next_capability,
                "reason": "derived_from_fact_ontology",
            },
            "final_response": "",
            "final_case_status": status,
        }
        return TraceGenerationResult(trace=trace, total_tokens=response.usage.total_tokens)


def score_gold_against_trace(
    gold: dict[str, Any], trace: dict[str, Any], *, goal_judgment: dict[str, Any] | None = None
) -> dict[str, Any]:
    """只做可复现的第一阶段评分；最终业务完成率留给有 fixture 的第二阶段。"""

    target = gold["ground_truth"]
    gold_facts = {
        _canonical_fact(str(item.get("fact") or ""))
        for item in target.get("required_facts", [])
        if isinstance(item, dict)
    }
    trace_facts = {_canonical_fact(item) for item in trace.get("required_facts", [])}
    gold_capabilities = _gold_capabilities(target)
    trace_capabilities = [_canonical_capability(item) for item in trace.get("selected_capabilities", [])]
    planned_capabilities = _happy_path_capability_order(trace)
    expected_capability_set = set(gold_capabilities)
    observed_capability_set = set(trace_capabilities)
    requested_escalation = bool(trace.get("escalation", {}).get("requested"))
    expected_escalation = _gold_escalation_required(target)
    status = str(trace.get("final_case_status") or "").upper()
    policy_violations = [
        check
        for check in trace.get("policy_checks", [])
        if isinstance(check, dict) and check.get("decision") == "policy_violation"
    ]
    goal_primary = _gold_goal(target)
    parsed_primary = str(trace.get("parsed_goal", {}).get("primary") or "")
    return {
        "case_id": str(gold["id"]),
        "goal_accuracy": goal_judgment
        or {
            "value": None,
            "similarity_proxy": _goal_similarity(goal_primary, parsed_primary),
            "method": "not_judged_requires_llm_semantic_judge_or_human_review",
        },
        "fact_requirements": _set_metrics(gold_facts, trace_facts),
        "capability_selection": _set_metrics(expected_capability_set, observed_capability_set),
        "plan_validity": {
            "value": _relative_order_is_valid(gold_capabilities, planned_capabilities),
            "expected_order": gold_capabilities,
            "observed_order": planned_capabilities,
        },
        "policy_compliance": {"value": not policy_violations, "violations": policy_violations},
        "escalation_accuracy": {
            "value": expected_escalation is None or requested_escalation == expected_escalation,
            "expected_required": expected_escalation,
            "requested": requested_escalation,
        },
        "false_resolution": {
            "value": status in _RESOLVED_STATUSES,
            "reason": "dry_run_has_no_verified_facts" if status in _RESOLVED_STATUSES else "",
        },
    }


def aggregate_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    if not scores:
        return {"evaluated": 0}

    def rate(path: tuple[str, ...]) -> float | None:
        values: list[bool] = []
        for score in scores:
            current: Any = score
            for key in path:
                current = current[key]
            if current is not None:
                values.append(bool(current))
        return sum(values) / len(values) if values else None

    def weighted(field: str, metric: str) -> float:
        matched = sum(int(score[field]["matched"]) for score in scores)
        denominator = sum(int(score[field][metric]) for score in scores)
        return matched / denominator if denominator else 1.0

    return {
        "evaluated": len(scores),
        "goal_accuracy": rate(("goal_accuracy", "value")),
        "fact_recall": weighted("fact_requirements", "expected"),
        "fact_precision": weighted("fact_requirements", "observed"),
        "capability_recall": weighted("capability_selection", "expected"),
        "capability_precision": weighted("capability_selection", "observed"),
        "plan_validity": rate(("plan_validity", "value")),
        "policy_compliance": rate(("policy_compliance", "value")),
        "escalation_accuracy": rate(("escalation_accuracy", "value")),
        "false_resolution_rate": rate(("false_resolution", "value")),
    }
