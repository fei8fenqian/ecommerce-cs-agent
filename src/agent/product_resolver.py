"""Semantic resolver for server-owned ecommerce candidate subjects.

Recommendation is intentionally *not* owned here.  The Operator LLM may make
open-ended recommendations over the server-owned candidate frame.  This
resolver only answers the identity question: which candidate is the user
referring to, or which candidates remain ambiguous?
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from agent.llm.llm_client import LLMClient
from agent.product_identity import canonical_product_candidate, canonical_product_identity
from exceptions import LLMError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductResolution:
    status: str = "unknown"  # selected | ambiguous | unknown
    selected_ref: str = ""
    ambiguous_refs: list[str] = field(default_factory=list)


def _json_object(text: str) -> dict[str, Any] | None:
    """Parse one JSON object without accepting prose as a business protocol."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]).strip() if len(lines) >= 3 else raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(raw[start : end + 1])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    return value if isinstance(value, dict) else None


class ProductResolver:
    """Resolve semantic product references only inside one server candidate frame."""

    def __init__(self, llm: "LLMClient"):
        self.llm = llm

    async def resolve(
        self,
        *,
        query: str,
        candidates: Sequence[Mapping[str, Any]],
        product_context: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]] | None = None,
        previous_selected: Mapping[str, Any] | None = None,
        purpose: str = "auto",
    ) -> ProductResolution:
        safe_candidates: list[dict[str, Any]] = []
        for index, raw in enumerate(candidates[:12], start=1):
            candidate = canonical_product_candidate(raw)
            if candidate is None:
                continue
            public: dict[str, Any] = {
                "ref": f"candidate_{index}",
                "name": candidate["product"],
            }
            if isinstance(candidate.get("price_cents"), int):
                public["price_yuan"] = candidate["price_cents"] / 100
            attributes = candidate.get("public_attributes")
            if isinstance(attributes, Mapping) and attributes:
                public["attributes"] = {
                    str(key)[:80]: str(value)[:240]
                    for key, value in list(attributes.items())[:48]
                    if str(key).strip() and str(value).strip()
                }
            summary = candidate.get("summary")
            if isinstance(summary, str) and summary.strip():
                public["summary"] = summary[:700]
            safe_candidates.append(public)
        if not safe_candidates:
            return ProductResolution()

        allowed = {item["ref"] for item in safe_candidates}
        previous_selected_ref = ""
        previous_identity = canonical_product_identity(previous_selected or {})
        if previous_identity is not None:
            previous_key = (previous_identity["product_category"], previous_identity["product_id"])
            for index, raw in enumerate(candidates[:12], start=1):
                identity = canonical_product_identity(raw) if isinstance(raw, Mapping) else None
                if identity and (identity["product_category"], identity["product_id"]) == previous_key:
                    previous_selected_ref = f"candidate_{index}"
                    break

        recent: list[dict[str, str]] = []
        for item in (history or [])[-6:]:
            if item.get("role") not in {"user", "assistant"}:
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                recent.append({"role": str(item.get("role")), "content": content[:400]})

        normalized_purpose = purpose if purpose in {"inspect", "purchase", "auto"} else "auto"
        context_payload = {
            "purpose": normalized_purpose,
            "preference_turns": product_context.get("preference_turns", [])[-6:],
            "previous_selected_ref": previous_selected_ref or None,
        }
        system = (
            "你是商品指代解析器，不是推荐器。只能在服务端给出的 candidates 中判断客户当前指的是哪件商品。"
            "如果客户明确选中一个唯一候选，status=selected 并返回 selected_ref；"
            "如果客户指向一个型号/系列但存在多个配置且当前表达不足以区分，status=ambiguous 并返回相关 refs；"
            "如果当前话只是开放式推荐、比较、换一批、性能最好、性价比等，没有明确选择某个候选，必须 status=unknown，"
            "不要替导购做推荐排序。previous_selected_ref 只是已验证的上一轮商品上下文；只有客户继续问这款/它/详情/购买且没有切换证据时才能继续它。"
            "不得输出 product_id，不得生成候选外商品。只返回一个JSON对象。"
        )
        payload = {
            "query": query[:1200],
            "recent_conversation": recent,
            "product_context": context_payload,
            "candidates": safe_candidates,
        }
        try:
            response = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.0,
                max_tokens=220,
                response_format={"type": "json_object"},
            )
        except LLMError as exc:
            logger.warning("product resolver unavailable error_type=%s", type(exc).__name__)
            return ProductResolution()

        data = _json_object(response.content or "")
        if data is None:
            logger.warning("product resolver invalid json")
            return ProductResolution()
        status = str(data.get("status") or "unknown").strip().lower()
        selected_ref = str(data.get("selected_ref") or "").strip()
        ambiguous_refs = [
            str(ref) for ref in data.get("ambiguous_refs", []) if isinstance(ref, str) and ref in allowed
        ][:12]

        if status == "selected" and selected_ref in allowed:
            result = ProductResolution(status="selected", selected_ref=selected_ref)
        elif status == "ambiguous" and len(set(ambiguous_refs)) >= 2:
            result = ProductResolution(
                status="ambiguous",
                ambiguous_refs=list(dict.fromkeys(ambiguous_refs)),
            )
        else:
            result = ProductResolution()

        logger.info(
            "product subject resolution status=%s purpose=%s candidates=%s ambiguous=%s selected=%s",
            result.status,
            normalized_purpose,
            len(safe_candidates),
            len(result.ambiguous_refs),
            bool(result.selected_ref),
        )
        return result


def candidate_for_ref(ref: str, candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Bind one opaque candidate_N back to the same server-owned frame."""
    if not re.fullmatch(r"candidate_[1-9][0-9]*", ref or ""):
        return None
    index = int(ref.split("_", 1)[1]) - 1
    if not 0 <= index < len(candidates):
        return None
    return canonical_product_candidate(candidates[index])
