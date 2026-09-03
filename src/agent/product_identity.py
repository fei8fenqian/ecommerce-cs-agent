"""Small, server-owned helpers for carrying catalog product identity across turns.

The values accepted here must come from a catalog/tool observation.  This module
does not resolve an order subject and must never turn model-supplied IDs into a
trusted product identity.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

PRODUCT_CATEGORIES = frozenset({"laptops", "phones", "components"})

# Catalog brand aliases are identity normalization only.  They never route a
# Goal, authorize a tool, or select an order outside a server-owned candidate
# set.  Canonical keys mirror catalog-facing brands while aliases cover common
# display-language variants used by customers.
_CATALOG_BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "acer": ("acer", "宏碁"),
    "aigo": ("aigo", "爱国者"),
    "apple": ("apple", "苹果"),
    "asus": ("asus", "华硕"),
    "bose": ("bose",),
    "dell": ("dell", "戴尔"),
    "honor": ("honor", "荣耀"),
    "hp": ("hp", "惠普"),
    "huawei": ("huawei", "华为"),
    "kingston": ("kingston", "金士顿"),
    "lenovo": ("lenovo", "联想"),
    "microsoft": ("microsoft", "微软"),
    "oneplus": ("oneplus", "一加"),
    "oppo": ("oppo",),
    "samsung": ("samsung", "三星"),
    "sony": ("sony", "索尼"),
    "vivo": ("vivo",),
    "xiaomi": ("xiaomi", "小米"),
}

_COMPARISON_METADATA_FIELDS = ("brand", "model", "storage", "screen_size", "ram", "capacity")


def normalize_product_text(value: object) -> str:
    """Normalize public product text for conservative exact candidate matching."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", normalized)


def catalog_brand_aliases() -> tuple[str, ...]:
    """Return bounded display aliases used only by identity normalization."""
    return tuple(dict.fromkeys(alias for aliases in _CATALOG_BRAND_ALIASES.values() for alias in aliases))


def catalog_brand_keys(value: object) -> frozenset[str]:
    """Map catalog/display brand text to canonical identity keys.

    Matching is deliberately lexical and bounded.  Short ASCII aliases such
    as ``hp`` require token boundaries; longer ASCII and CJK aliases may occur
    inside a product display title such as ``HUAWEI Mate 70``.
    """
    raw = unicodedata.normalize("NFKC", str(value or "")).casefold()
    compact = re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]<>《》_-]+", "", raw)
    keys: set[str] = set()
    for canonical, aliases in _CATALOG_BRAND_ALIASES.items():
        for alias in aliases:
            normalized_alias = unicodedata.normalize("NFKC", alias).casefold()
            if normalized_alias.isascii() and len(normalized_alias) <= 2:
                if re.search(rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])", raw):
                    keys.add(canonical)
                    break
            elif normalize_product_text(normalized_alias) in compact:
                keys.add(canonical)
                break
    return frozenset(keys)


def canonical_product_identity(value: Mapping[str, Any]) -> dict[str, str] | None:
    """Extract only the canonical identity fields supplied by the server."""
    product_id = value.get("product_id") or value.get("id")
    category = value.get("product_category") or value.get("category")
    product_name = value.get("product_name") or value.get("title") or value.get("display_title") or value.get("product")
    display_title = value.get("display_title") or value.get("title") or value.get("product") or product_name
    if not all(
        isinstance(item, str) and item.strip()
        for item in (product_id, category, product_name, display_title)
    ):
        return None
    if category not in PRODUCT_CATEGORIES:
        return None

    identity = {
        "product": str(display_title).strip(),
        "product_id": str(product_id).strip(),
        "product_category": str(category).strip(),
        "product_name": str(product_name).strip(),
    }
    component_category = value.get("component_category")
    if isinstance(component_category, str) and component_category.strip():
        identity["component_category"] = component_category.strip()
    return identity


def _trusted_price_cents(value: Mapping[str, Any]) -> int | None:
    """Normalize a server-owned catalog price without trusting model text."""
    price_cents = value.get("price_cents")
    if isinstance(price_cents, int) and not isinstance(price_cents, bool) and price_cents >= 0:
        return price_cents

    price = value.get("price")
    if price is None or isinstance(price, bool):
        return None
    try:
        amount = Decimal(str(price))
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def canonical_product_candidate(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project server-owned candidate identity plus trusted comparison metadata.

    Candidate metadata is intentionally richer than the selected-product
    identity.  It can help the semantic router compare only the products the
    server actually returned, while the canonical product id/category remain
    the only fields promoted into selected product state.
    """
    identity = canonical_product_identity(value)
    if identity is None:
        return None
    candidate: dict[str, Any] = dict(identity)
    price_cents = _trusted_price_cents(value)
    if price_cents is not None:
        candidate["price_cents"] = price_cents
    comparison = value.get("comparison_metadata")
    comparison_source = comparison if isinstance(comparison, Mapping) else value
    for key in _COMPARISON_METADATA_FIELDS:
        metadata_value = comparison_source.get(key)
        if isinstance(metadata_value, (str, int, float)) and not isinstance(metadata_value, bool):
            text = str(metadata_value).strip()
            if text:
                candidate[key] = text[:120]
    return candidate


def dedupe_product_identities(values: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Keep one identity per catalog category/id pair, preserving server order."""
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        identity = canonical_product_identity(value)
        if identity is None:
            continue
        key = (identity["product_category"], identity["product_id"])
        if key in seen:
            continue
        seen.add(key)
        result.append(identity)
    return result


def dedupe_product_candidates(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Keep one server-owned candidate per catalog category/id, with metadata."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        candidate = canonical_product_candidate(value)
        if candidate is None:
            continue
        key = (str(candidate["product_category"]), str(candidate["product_id"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def product_match_names(identity: Mapping[str, str]) -> tuple[str, ...]:
    """Return public names usable for an exact, whitespace-insensitive match."""
    names: list[str] = []
    for key in ("product", "product_name"):
        value = identity.get(key, "").strip()
        if value and value not in names:
            names.append(value)
    return tuple(names)


def match_product_candidate(
    query: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """Resolve a query only when exactly one server-owned candidate matches.

    This intentionally does not fuzzy-match short words or choose the first
    result.  Ambiguous candidates remain unresolved for the normal choice path.
    """
    normalized_query = normalize_product_text(query)
    if not normalized_query:
        return None

    matches: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = canonical_product_identity(candidate)
        if identity is None:
            continue
        key = (identity["product_category"], identity["product_id"])
        if key in seen:
            continue
        names = product_match_names(identity)
        if any(
            normalize_product_text(name) in normalized_query
            for name in names
        ):
            seen.add(key)
            matches.append(identity)
    return matches[0] if len(matches) == 1 else None


def stored_product_identity(entities: Mapping[str, Any]) -> dict[str, str] | None:
    """Read a previously projected product identity without trusting loose text."""
    return canonical_product_identity(
        {
            "product": entities.get("product"),
            "product_id": entities.get("product_id"),
            "product_category": entities.get("product_category"),
            "product_name": entities.get("product_name") or entities.get("product"),
            "component_category": entities.get("component_category"),
        }
    )
