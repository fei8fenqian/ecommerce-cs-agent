"""Bounded ecommerce-role state helpers.

This module owns product constraints and candidate-frame metadata only.  It does
not know about SupportCase, orders, payment, or refund state.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from agent.product_identity import canonical_product_candidate, catalog_brand_keys

_PRODUCT_CONTEXT_KEY = "product_context"
_MAX_PREFERENCE_TURNS = 6


def product_context_from_entities(entities: Mapping[str, Any]) -> dict[str, Any]:
    raw = entities.get(_PRODUCT_CONTEXT_KEY)
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if key
        in {
            "category",
            "min_price_cents",
            "max_price_cents",
            "target_price_cents",
            "brand_keys",
            "preference_turns",
            "frame_id",
            "choice_refs",
        }
    }


def _money_to_cents(value: str) -> int | None:
    try:
        amount = int(value.replace(",", ""))
    except ValueError:
        return None
    if amount < 0 or amount > 1_000_000:
        return None
    return amount * 100


def extract_price_update(query: str) -> dict[str, int | None]:
    """Extract only explicit numeric budget syntax.

    This is intentionally a narrow parameter parser, not a product recommender.
    It never infers a product, brand, or business action from keywords.
    """
    text = unicodedata.normalize("NFKC", query).casefold().replace(",", "")
    range_match = re.search(
        r"(?<!\d)(\d{3,6})\s*(?:元|块|快|rmb|¥)?\s*(?:[-~～—–]|到|至)\s*"
        r"(\d{3,6})\s*(?:元|块|快|rmb|¥)?(?!\d)",
        text,
    )
    if range_match:
        first = _money_to_cents(range_match.group(1))
        second = _money_to_cents(range_match.group(2))
        if first is not None and second is not None:
            low, high = sorted((first, second))
            return {"min_price_cents": low, "max_price_cents": high, "target_price_cents": None}

    upper_patterns = (
        r"(?<!\d)(\d{3,6})\s*(?:元|块|快|rmb|¥)?\s*(?:以内|以下|之内|不超过|最多)",
        r"(?:预算|价格|价位)[^\d]{0,6}(\d{3,6})\s*(?:元|块|快|rmb|¥)?\s*(?:封顶|上限)",
    )
    for pattern in upper_patterns:
        match = re.search(pattern, text)
        if match:
            value = _money_to_cents(match.group(1))
            if value is not None:
                return {"min_price_cents": None, "max_price_cents": value, "target_price_cents": None}

    lower_match = re.search(r"(?<!\d)(\d{3,6})\s*(?:元|块|快|rmb|¥)?\s*(?:以上|起|起步|至少)", text)
    if lower_match:
        value = _money_to_cents(lower_match.group(1))
        if value is not None:
            return {"min_price_cents": value, "max_price_cents": None, "target_price_cents": None}

    target_match = re.search(
        r"(?:预算|价位|价格)?[^\d]{0,5}(\d{3,6})\s*(?:元|块|快|rmb|¥)?\s*(?:左右|上下|附近)",
        text,
    )
    if target_match:
        value = _money_to_cents(target_match.group(1))
        if value is not None:
            return {"target_price_cents": value}
    return {}


def category_from_table(table: str) -> str:
    return {
        "phone_products": "phones",
        "laptop_products": "laptops",
        "component_products": "components",
    }.get(str(table or ""), "")


def update_product_context(
    existing: Mapping[str, Any] | None,
    *,
    query: str,
    table: str,
    category: str = "",
    is_product_turn: bool,
) -> tuple[dict[str, Any], bool]:
    """Return ecommerce-role context and whether hard candidate eligibility changed."""
    context = dict(existing or {})
    # Role-state isolation: Support/Service turns may retain ProductContext as
    # conversational history, but they must never mutate ecommerce constraints
    # merely because an order/refund utterance mentions a price or product.
    if not is_product_turn:
        return context, False

    before = {
        key: context.get(key)
        for key in ("category", "min_price_cents", "max_price_cents", "target_price_cents", "brand_keys")
    }

    resolved_category = category if category in {"phones", "laptops", "components"} else category_from_table(table)
    if resolved_category:
        context["category"] = resolved_category

    price_update = extract_price_update(query)
    if price_update:
        # Explicit range/bound replaces stale bounds rather than accumulating an
        # impossible intersection from prior turns.
        if "min_price_cents" in price_update:
            if price_update["min_price_cents"] is None:
                context.pop("min_price_cents", None)
            else:
                context["min_price_cents"] = price_update["min_price_cents"]
        if "max_price_cents" in price_update:
            if price_update["max_price_cents"] is None:
                context.pop("max_price_cents", None)
            else:
                context["max_price_cents"] = price_update["max_price_cents"]
        if "target_price_cents" in price_update:
            if price_update["target_price_cents"] is None:
                context.pop("target_price_cents", None)
            else:
                context["target_price_cents"] = price_update["target_price_cents"]

    # A brand mention is not automatically an eligibility constraint: "vivo S60
    # 也有吧" and "只要 vivo" are different semantics.  ProductResolver may
    # use brand/model text inside the current candidate frame; a future explicit
    # semantic brand parameter can populate brand_keys after server validation.

    preference_turns = [
        str(item)[:400] for item in context.get("preference_turns", []) if isinstance(item, str) and item.strip()
    ]
    normalized = query.strip()
    if normalized and (not preference_turns or preference_turns[-1] != normalized):
        preference_turns.append(normalized[:400])
    context["preference_turns"] = preference_turns[-_MAX_PREFERENCE_TURNS:]

    after = {
        key: context.get(key)
        for key in ("category", "min_price_cents", "max_price_cents", "target_price_cents", "brand_keys")
    }
    changed = before != after
    if changed:
        context.pop("frame_id", None)
        context.pop("choice_refs", None)
    return context, changed


def filter_candidates_by_context(
    candidates: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Apply server-verifiable hard constraints to a trusted candidate set."""
    result: list[dict[str, Any]] = []
    expected_category = str(context.get("category") or "")
    allowed_brands = set(context.get("brand_keys") or [])
    min_price = context.get("min_price_cents")
    max_price = context.get("max_price_cents")
    for raw in candidates:
        candidate = canonical_product_candidate(raw)
        if candidate is None:
            continue
        if expected_category and candidate.get("product_category") != expected_category:
            continue
        price = candidate.get("price_cents")
        if isinstance(min_price, int) and (not isinstance(price, int) or price < min_price):
            continue
        if isinstance(max_price, int) and (not isinstance(price, int) or price > max_price):
            continue
        if allowed_brands:
            keys = catalog_brand_keys(candidate.get("brand") or candidate.get("product") or "")
            if not keys.intersection(allowed_brands):
                continue
        result.append(candidate)
    return result


def attach_candidate_frame(context: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    updated = dict(context)
    identities = [
        (str(item.get("product_category") or ""), str(item.get("product_id") or ""))
        for item in candidates
        if isinstance(item, Mapping)
    ]
    payload = {
        "category": updated.get("category"),
        "min": updated.get("min_price_cents"),
        "max": updated.get("max_price_cents"),
        "brands": updated.get("brand_keys", []),
        "ids": identities,
    }
    updated["frame_id"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return updated


def set_choice_refs(context: Mapping[str, Any], refs: Sequence[str]) -> dict[str, Any]:
    updated = dict(context)
    bounded = [ref for ref in refs if re.fullmatch(r"candidate_[1-9][0-9]*", str(ref))][:12]
    if bounded:
        updated["choice_refs"] = list(dict.fromkeys(bounded))
    else:
        updated.pop("choice_refs", None)
    return updated


def ordinal_choice_ref(query: str, context: Mapping[str, Any]) -> str | None:
    refs = [ref for ref in context.get("choice_refs", []) if isinstance(ref, str)]
    if not refs:
        return None
    text = unicodedata.normalize("NFKC", query).strip().casefold()
    match = re.fullmatch(r"(?:第\s*)?([1-9][0-9]*)(?:\s*(?:个|款|台|项))?", text)
    if not match:
        chinese = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        match_cn = re.fullmatch(r"第?([一二三四五六七八九])(?:个|款|台|项)?", text)
        index = chinese.get(match_cn.group(1)) if match_cn else None
    else:
        index = int(match.group(1))
    if not index or index < 1 or index > len(refs):
        return None
    return refs[index - 1]


def product_context_entity(context: Mapping[str, Any]) -> dict[str, Any]:
    return {_PRODUCT_CONTEXT_KEY: dict(context)}
