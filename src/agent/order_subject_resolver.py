"""Resolve a natural-language order reference against server-owned candidates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class OrderSubjectResolution:
    status: str
    selected_ref: str = ""
    ambiguous_refs: tuple[str, ...] = ()


def _strip_code_fence(value: str) -> str:
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return value


def parse_order_subject_resolution(value: object, valid_refs: set[str]) -> OrderSubjectResolution:
    """Strictly validate resolver output and reject unknown candidate refs."""

    payload: object = value
    if isinstance(value, str):
        try:
            payload = json.loads(_strip_code_fence(value.strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return OrderSubjectResolution("unknown")
    if not isinstance(payload, Mapping):
        return OrderSubjectResolution("unknown")
    status = payload.get("status")
    selected = payload.get("selected_ref")
    ambiguous = payload.get("ambiguous_refs")
    if status not in {"resolved", "ambiguous", "unknown"}:
        return OrderSubjectResolution("unknown")
    if not isinstance(selected, str) or not isinstance(ambiguous, list):
        return OrderSubjectResolution("unknown")
    ambiguous_refs = tuple(dict.fromkeys(item for item in ambiguous if isinstance(item, str)))
    if any(ref not in valid_refs for ref in ambiguous_refs):
        return OrderSubjectResolution("unknown")
    if status == "resolved":
        if selected not in valid_refs or ambiguous_refs:
            return OrderSubjectResolution("unknown")
        return OrderSubjectResolution("resolved", selected_ref=selected)
    if status == "ambiguous":
        if selected or len(ambiguous_refs) < 2:
            return OrderSubjectResolution("unknown")
        return OrderSubjectResolution("ambiguous", ambiguous_refs=ambiguous_refs)
    if selected or ambiguous_refs:
        return OrderSubjectResolution("unknown")
    return OrderSubjectResolution("unknown")


def _candidate_for_prompt(candidate: Mapping[str, Any], ref: str) -> dict[str, Any]:
    """Expose descriptive metadata, never an order or product identifier."""

    items_payload: list[dict[str, str]] = []
    items = candidate.get("items")
    if isinstance(items, list):
        for item in items[:10]:
            if not isinstance(item, Mapping):
                continue
            safe_item = {
                key: str(item[key])
                for key in ("product_name", "catalog_category", "component_category")
                if isinstance(item.get(key), str) and item[key].strip()
            }
            if safe_item:
                items_payload.append(safe_item)
    payload: dict[str, Any] = {"ref": ref, "items": items_payload}
    for key in ("product_name", "catalog_category", "component_category", "recency_rank"):
        value = candidate.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value != "":
            payload[key] = value
    amount = candidate.get("amount_cents")
    if isinstance(amount, int) and not isinstance(amount, bool):
        payload["amount_cents"] = amount
    return payload


_SYSTEM_PROMPT = """你是电商客服的订单指代解析器，只返回 JSON，不调用工具。
请只在给定的当前客户订单候选中判断客户消息指向哪一笔。候选 ref 是服务端临时引用，
不要输出订单号、商品 ID、退款资格、支付状态或任何候选之外的 ref。
你是订单自然语言 Subject 的唯一语义 owner：必须直接根据 customer_message 判断当前话是在继续上一笔、
切换到另一笔，还是无法唯一确定；不要依赖其他 Router 给出的 same/changed 标签。
previous_subject_ref 只是上一笔服务端已验证订单在当前候选集里的临时 ref，用于理解跨轮连续性。
recent_product_context 是 Ecommerce Role 提供的最近服务端已验证商品身份摘要，只能帮助理解跨 Role 的省略指代；
它不是订单 authority，也不能直接提供 order_id。当前消息可能同时包含多个目标：只把与当前退款、物流、支付等
Service Goal 语义相连的身份信息用于订单解析，其他目标中的商品偏好不得污染订单 Subject。若当前 Service Goal
自己明确给出了新的品牌、型号、品类或切换证据，应以当前消息为准，recent_product_context 只能退居上下文。
如果当前消息只是继续追问/确认同一业务（例如只问“退款现在怎么样”“可以”“那现在呢”），并且没有出现新的
品牌、品类、型号、序号、时间修饰或“另一笔/剩下那笔”等切换证据，可以继续 previous_subject_ref。
一旦当前消息出现新的身份或切换证据，应以当前消息为准；如果与 previous_subject_ref 冲突，不能因为上一轮
选中过它而继续绑定。
“刚下单/最近”等明确时间修饰只能参考候选提供的 recency_rank；没有足够身份信息时，不得仅因为某笔更新、
更贵或排在前面就偷偷选择。无法唯一判断就返回 ambiguous 或 unknown。
previous_subject_ref 不包含任何交易事实；不得从它推断退款资格、支付状态或订单状态。
严格格式：
{"status":"resolved","selected_ref":"order_candidate_1","ambiguous_refs":[]}
{"status":"ambiguous","selected_ref":"","ambiguous_refs":["order_candidate_1","order_candidate_2"]}
{"status":"unknown","selected_ref":"","ambiguous_refs":[]}
"""


async def resolve_order_subject(
    llm: Any,
    customer_message: str,
    candidates: list[Mapping[str, Any]],
    *,
    previous_subject_ref: str = "",
    recent_product_context: Mapping[str, Any] | None = None,
) -> OrderSubjectResolution:
    """Ask the model for a candidate ref, then validate it server-side."""

    if llm is None or not candidates:
        return OrderSubjectResolution("unknown")
    refs = {f"order_candidate_{index}" for index in range(1, len(candidates) + 1)}
    prompt_candidates = [
        _candidate_for_prompt(candidate, f"order_candidate_{index}")
        for index, candidate in enumerate(candidates, start=1)
        if isinstance(candidate, Mapping)
    ]
    if not prompt_candidates:
        return OrderSubjectResolution("unknown")
    previous_ref = previous_subject_ref if previous_subject_ref in refs else ""
    product_context: dict[str, str] = {}
    if isinstance(recent_product_context, Mapping):
        for key in ("product", "product_category", "component_category"):
            value = recent_product_context.get(key)
            if isinstance(value, str) and value.strip():
                product_context[key] = value.strip()[:240]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "customer_message": customer_message[:1000],
                    "previous_subject_ref": previous_ref,
                    "recent_product_context": product_context,
                    "candidates": prompt_candidates,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        response = await llm.chat(
            messages,
            temperature=0.0,
            max_tokens=160,
            response_format={"type": "json_object"},
        )
    except TypeError:
        # Small test doubles may implement the older chat signature.  The
        # response remains strictly parsed and server-validated below.
        try:
            response = await llm.chat(messages, temperature=0.0, max_tokens=160)
        except Exception:
            return OrderSubjectResolution("unknown")
    except Exception:
        return OrderSubjectResolution("unknown")
    return parse_order_subject_resolution(getattr(response, "content", response), refs)
