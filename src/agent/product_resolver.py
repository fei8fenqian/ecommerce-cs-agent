"""Semantic resolver for one server-owned ecommerce candidate frame.

The resolver owns semantic product selection, ambiguity preservation and
open-ended recommendation ordering *inside* that frame.  It never receives or
returns canonical product ids; the server validates opaque refs and owns all
navigation/actions.
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
    recommended_refs: list[str] = field(default_factory=list)


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

        normalized_purpose = purpose if purpose in {"inspect", "purchase", "recommend", "auto"} else "auto"
        previous_choice_refs = [
            str(ref) for ref in product_context.get("choice_refs", []) if isinstance(ref, str) and ref in allowed
        ][:12]
        context_payload = {
            "purpose": normalized_purpose,
            "preference_turns": product_context.get("preference_turns", [])[-6:],
            "previous_selected_ref": previous_selected_ref or None,
            "previous_choice_refs": previous_choice_refs,
        }
        system = (
            "你是商品候选解析与推荐排序器。只能在服务端给出的 candidates 中工作，永远不能创造候选外商品。"
            "如果客户明确选中一个唯一候选，status=selected 并只返回 selected_ref；"
            "如果客户指向一个型号/系列但存在多个配置且当前表达不足以区分，status=ambiguous 并返回相关 ambiguous_refs；"
            "如果当前是开放式推荐、换一批、预算内推荐、性能/影像/便携等偏好比较，status=unknown，"
            "并用 recommended_refs 按最符合客户当前诉求的顺序返回候选。通常推荐 3-6 个；客户明确要求全部时才可更多。"
            "推荐排序只依据客户语言、product_context 与 candidates 的真实公开属性；缺失属性不得脑补。"
            "selected/ambiguous 与 recommendation 是不同语义：selected 或 ambiguous 时 recommended_refs 必须为空。"
            "previous_selected_ref 只是已验证的上一轮单品上下文；只有客户继续问这款/它/详情/购买且没有切换证据时才能继续它。"
            "previous_choice_refs 是同一个候选帧里上一轮服务端实际展示/推荐的有序集合；如果客户本轮整体指代‘这些/刚才推荐的/对应链接/对比它们’且没有新约束，"
            "可以 status=unknown 并原顺序返回这些 recommended_refs。"
            "不得输出 product_id、URL 或候选外商品。只返回一个JSON对象，字段仅使用 status、selected_ref、ambiguous_refs、recommended_refs。"
        )
        payload = {
            "query": query[:1200],
            "recent_conversation": recent,
            "product_context": context_payload,
            "candidates": safe_candidates,
        }
        fallback_recommendations = (
            [item["ref"] for item in safe_candidates[: min(5, len(safe_candidates))]]
            if normalized_purpose == "recommend"
            else []
        )
        try:
            response = await self.llm.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.0,
                max_tokens=260,
                response_format={"type": "json_object"},
            )
        except LLMError as exc:
            logger.warning("product resolver unavailable error_type=%s", type(exc).__name__)
            return ProductResolution(recommended_refs=fallback_recommendations)

        data = _json_object(response.content or "")
        if data is None:
            logger.warning("product resolver invalid json")
            return ProductResolution(recommended_refs=fallback_recommendations)
        status = str(data.get("status") or "unknown").strip().lower()
        selected_ref = str(data.get("selected_ref") or "").strip()
        ambiguous_refs = [
            str(ref) for ref in data.get("ambiguous_refs", []) if isinstance(ref, str) and ref in allowed
        ][:12]
        recommended_refs = [
            str(ref) for ref in data.get("recommended_refs", []) if isinstance(ref, str) and ref in allowed
        ][:12]
        recommended_refs = list(dict.fromkeys(recommended_refs))

        if status == "selected" and selected_ref in allowed:
            result = ProductResolution(status="selected", selected_ref=selected_ref)
        elif status == "ambiguous" and len(set(ambiguous_refs)) >= 2:
            result = ProductResolution(
                status="ambiguous",
                ambiguous_refs=list(dict.fromkeys(ambiguous_refs)),
            )
        else:
            result = ProductResolution(recommended_refs=recommended_refs or fallback_recommendations)

        logger.info(
            "product resolution status=%s purpose=%s candidates=%s ambiguous=%s selected=%s recommended=%s",
            result.status,
            normalized_purpose,
            len(safe_candidates),
            len(result.ambiguous_refs),
            bool(result.selected_ref),
            len(result.recommended_refs),
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
