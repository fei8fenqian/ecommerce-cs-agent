"""客服 Case 中客户选择的确定性解析。

这个模块只把客户对已展示候选的回复解析成一个服务端已经允许的 subject。
它不做意图路由，也不从数据库重新查询或重新排序候选，避免把自然语言再次交给
模型选择订单。
"""

from __future__ import annotations

import re
from typing import Any

from agent.product_identity import catalog_brand_aliases, catalog_brand_keys

_ORDINALS = {
    "1": 1,
    "一": 1,
    "壹": 1,
    "2": 2,
    "二": 2,
    "两": 2,
    "贰": 2,
    "3": 3,
    "三": 3,
    "叁": 3,
}

_SUBJECT_IDENTITY_EXCLUSION_MARKERS = ("不是", "不要", "排除", "除了")
_SUBJECT_IDENTITY_COMPARATIVE_MARKERS = ("贵一点", "贵的", "金额高", "便宜", "金额低")

# Require either a numeric component or an explicit separator after ``SO`` so
# a brand such as ``Sony`` cannot be mistaken for an order identifier.
_SUBJECT_ORDER_ID = re.compile(r"SO(?:[A-Z0-9]*\d[A-Z0-9_-]*|[-_][A-Z0-9_-]+)", re.IGNORECASE)
_SUBJECT_MODEL = re.compile(r"(?=[a-z0-9_-]*\d)[a-z][a-z0-9_-]*$", re.IGNORECASE)
_SUBJECT_BRANDS = "|".join(
    re.escape(alias)
    for alias in (*catalog_brand_aliases(), "iphone", "macbook")
)
_SUBJECT_CATEGORIES = (
    "电脑|笔记本|手机|耳机|平板|相机|显示器|键盘|鼠标|手表|路由器|处理器|显卡|电视|主机|打印机|硬盘|内存"
)
_COMPONENT_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "solid_state_drive": ("ssd", "固态", "固态硬盘", "固态盘"),
    "memory": ("ram", "内存", "内存条"),
    "cooling_product": ("散热器", "cpu散热器", "风冷", "cooler"),
}
_CATALOG_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "laptops": ("电脑", "笔记本", "笔记本电脑"),
    "phones": ("手机",),
}
_SUBJECT_TRAILING_ATTRIBUTES = re.compile(r"(?:黑色|白色|银色|灰色|金色|蓝色|红色|粉色|绿色|紫色|深空灰|星光色)+$")


def _normalize(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]<>《》]+", "", value).lower()


def _order_id(choice: object) -> str:
    if not isinstance(choice, dict):
        return ""
    value = choice.get("order_id") or choice.get("order_no")
    return str(value).strip() if value else ""


def _product_name(choice: object) -> str:
    if not isinstance(choice, dict):
        return ""
    value = choice.get("product_name") or choice.get("product") or choice.get("title")
    return str(value).strip() if value else ""


def _amount_cents(choice: object) -> int | None:
    if not isinstance(choice, dict):
        return None
    value = choice.get("amount_cents")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _product_tokens(product: str) -> list[str]:
    if not isinstance(product, str) or not product.strip():
        return []
    # 型号/品牌通常以 ASCII 词出现；中文产品名则保留连续汉字片段。短词（如“笔记本”）
    # 不能独立作为唯一匹配依据，避免两个候选都是笔记本时误选。
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", product.lower())
    return [token for token in tokens if token not in {"笔记本", "手机", "电脑", "订单"}]


def _model_tokens(value: str) -> set[str]:
    """Return compact model tokens such as ``iphone16`` or ``kc3000``.

    A bare brand is intentionally not a model token.  This prevents the
    common ``iphone`` fragment from matching both iPhone 16 and iPhone 17,
    while keeping model forms that differ only by spaces or hyphens stable.
    """

    raw = value.casefold()
    separated = {
        re.sub(r"[^a-z0-9]", "", token)
        for token in re.findall(r"[a-z]+[\s_-]*\d+", raw)
    }
    if separated:
        return {token for token in separated if len(token) >= 2}
    normalized = _normalize(value)
    return {
        token
        for token in re.findall(r"[a-z]+\d[a-z0-9_-]*|\d+[a-z][a-z0-9_-]*", normalized)
        if len(token) >= 2
    }


def _choice_identity_values(choice: dict[str, Any]) -> list[str]:
    items = choice.get("items")
    values: list[str] = [] if isinstance(items, list) and items else [_product_name(choice)]
    for key, aliases in (
        ("catalog_category", _CATALOG_CATEGORY_ALIASES),
        ("component_category", _COMPONENT_CATEGORY_ALIASES),
    ):
        category = choice.get(key)
        if isinstance(category, str):
            values.extend(aliases.get(category, (category,)))
    for key in ("catalog_product_id", "product_id"):
        value = choice.get(key)
        if isinstance(value, str):
            values.append(value)
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("product_name")
            if isinstance(name, str):
                values.append(name)
            category = item.get("catalog_category")
            if isinstance(category, str):
                values.extend(_CATALOG_CATEGORY_ALIASES.get(category, (category,)))
            component_category = item.get("component_category")
            if isinstance(component_category, str):
                values.extend(_COMPONENT_CATEGORY_ALIASES.get(component_category, (component_category,)))
            product_id = item.get("catalog_product_id")
            if isinstance(product_id, str):
                values.append(product_id)
    return values


def _matching_choices(query: str, choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_query = _normalize(query)
    query_models = _model_tokens(query)
    query_has_model_number = bool(query_models)
    query_brand_keys = catalog_brand_keys(query)
    matches: list[dict[str, Any]] = []
    for choice in choices:
        identities = _choice_identity_values(choice)
        if any((product := _normalize(value)) and product in normalized_query for value in identities):
            matches.append(choice)
            continue
        identity_models = {model for value in identities for model in _model_tokens(value)}
        if query_models and query_models.intersection(identity_models):
            matches.append(choice)
            continue
        # When the customer supplied a model number, a generic brand token is
        # not sufficient evidence.  ``iphone16`` must not match iPhone 17.
        if query_has_model_number:
            continue
        identity_brand_keys = frozenset(
            brand_key
            for value in identities
            for brand_key in catalog_brand_keys(value)
        )
        if query_brand_keys and query_brand_keys.intersection(identity_brand_keys):
            matches.append(choice)
            continue
        tokens = [token for value in identities for token in _product_tokens(value)]
        if tokens and any(token in normalized_query for token in tokens):
            matches.append(choice)
    return matches


def match_pending_subject_choices(raw_query: str, choices: object) -> list[dict[str, Any]]:
    """Return the server-side candidate set for a subject description.

    ``resolve_pending_subject_choice`` remains the authority for selecting one
    choice.  This companion helper is only used when a caller needs to
    distinguish zero candidates from an ambiguous candidate set (for example,
    while preparing a controlled subject correction).  It reuses the same
    normalization, product matching, exclusion, and amount-comparison rules;
    it never returns an order outside the supplied server-owned choices.
    """
    valid = _valid_choices(choices)
    if not valid:
        return []
    query = _normalize(raw_query)

    exact = [choice for choice in valid if _order_id(choice).lower() in query]
    if exact:
        return exact

    matches = _matching_choices(raw_query, valid)
    if matches:
        return matches

    if any(marker in query for marker in ("贵一点", "贵的", "金额高")):
        valued = [(choice, _amount_cents(choice)) for choice in valid]
        if all(amount is not None for _, amount in valued):
            maximum = max(int(amount) for _, amount in valued if amount is not None)
            return [choice for choice, amount in valued if amount == maximum]
    if any(marker in query for marker in ("便宜", "便宜的", "便宜一点", "金额低")):
        valued = [(choice, _amount_cents(choice)) for choice in valid]
        if all(amount is not None for _, amount in valued):
            minimum = min(int(amount) for _, amount in valued if amount is not None)
            return [choice for choice, amount in valued if amount == minimum]
    return []


def match_subject_identity_choices(
    raw_query: str,
    choices: object,
    *,
    trusted_exclusion_applied: bool = False,
) -> list[dict[str, Any]]:
    """只按订单号或商品身份匹配 correction 描述。

    这是 ``subject_correction_description`` 专用的 discovery helper。它不解释
    已展示候选的序号、金额比较或排除语义；这些能力只属于
    ``resolve_pending_subject_choice``，因为只有该状态已经把候选展示给客户。
    """
    valid = _valid_choices(choices)
    if not valid:
        return []
    query = _normalize(raw_query)
    if any(marker in query for marker in _SUBJECT_IDENTITY_COMPARATIVE_MARKERS):
        return []
    if not trusted_exclusion_applied and any(marker in query for marker in _SUBJECT_IDENTITY_EXCLUSION_MARKERS):
        return []

    exact = [choice for choice in valid if _order_id(choice).lower() in query]
    if exact:
        return exact
    return _matching_choices(raw_query, valid)


def looks_like_bare_subject_description(raw_query: str) -> bool:
    """判断一轮输入是否像是在补充商品描述，而不是新的业务请求。

    该判断只服务于 ``subject_correction_description`` pending 的 continuation。它不
    选择订单，也不参与普通意图路由；无法确认时返回 False，让原有 Router 处理。
    """
    if not isinstance(raw_query, str):
        return False
    value = raw_query.strip()
    if not value or len(value) > 80 or re.search(r"[。！？?!\n]", value):
        return False
    normalized = _normalize(value)
    if not normalized:
        return False
    # This is a positive identity grammar.  It intentionally rejects a bare
    # brand and business questions such as ``Sony价格``; false negatives are
    # safer than allowing a hidden candidate pool to select an order.
    identity = _SUBJECT_TRAILING_ATTRIBUTES.sub("", normalized)
    if _SUBJECT_ORDER_ID.fullmatch(identity):
        return True
    if _SUBJECT_MODEL.fullmatch(identity):
        return True
    return bool(
        re.fullmatch(rf"(?:{_SUBJECT_BRANDS})(?:{_SUBJECT_CATEGORIES})", identity, re.IGNORECASE)
        or re.fullmatch(rf"(?:{_SUBJECT_BRANDS})[a-z][a-z0-9_-]*", identity, re.IGNORECASE)
    )


def _valid_choices(raw_choices: object) -> list[dict[str, Any]]:
    if not isinstance(raw_choices, list):
        return []
    choices: list[dict[str, Any]] = []
    for raw in raw_choices:
        if not isinstance(raw, dict) or not _order_id(raw):
            continue
        choices.append(dict(raw))
    return choices


def looks_like_pending_subject_choice(raw_query: str, choices: object) -> bool:
    """判断本轮是否像是在回答已展示的候选，而不是创建新业务请求。"""
    valid = _valid_choices(choices)
    if not valid:
        return False
    query = _normalize(raw_query)
    if any(_order_id(choice).lower() in query for choice in valid):
        return True
    if re.search(r"(?:选)?第[一二两三123](?:个|笔|单|件)?$", query):
        return True
    # 这些省略回复没有足够信息完成选择，但在一个已展示候选的 pending 中，
    # 它们明确是在指代候选，而不是新的业务请求；保持原 Case 并重新提问。
    if query in {"这个", "那个", "这笔", "那笔", "这一个", "那一个", "刚才那个", "我说的那个"}:
        return True
    if any(marker in query for marker in ("贵一点", "贵的", "金额高", "便宜的", "便宜一点", "金额低")):
        return True
    if any(marker in query for marker in ("不是", "不要", "排除", "除了")) and _matching_choices(raw_query, valid):
        return True
    # A semantic resolver may narrow an ambiguous natural-language reply to a
    # subset.  Treat any candidate-bearing description as a reply to this
    # choice frame, without selecting an order here.
    return bool(_matching_choices(raw_query, valid))


def resolve_pending_subject_choice(
    raw_query: str,
    choices: object,
) -> dict[str, Any] | None:
    """把客户对持久化候选的回复解析为唯一订单。

    返回值包含原候选的最小副本和解析来源，供 Case 审计和后续 Tool 绑定使用；
    无法唯一确定时返回 ``None``。
    """
    valid = _valid_choices(choices)
    if not valid:
        return None
    query = _normalize(raw_query)

    # 显式订单号优先级最高；订单号来自本轮服务端展示的候选集合。
    exact = [choice for choice in valid if _order_id(choice).lower() in query]
    if len(exact) == 1:
        return {"choice": exact[0], "selection_source": "exact_id"}
    if len(exact) > 1:
        return None

    ordinal_match = re.search(r"(?:选)?第([一二两三123])(?:个|笔|单|件)?$", query)
    if ordinal_match:
        index = _ORDINALS.get(ordinal_match.group(1))
        if index is not None and index <= len(valid):
            return {"choice": valid[index - 1], "selection_source": "ordinal"}
        return None

    # 排除语义只在被排除对象唯一匹配且剩余候选唯一时成立。
    exclusion = any(marker in query for marker in ("不是", "不要", "排除", "除了"))
    matches = _matching_choices(raw_query, valid)
    if exclusion and len(matches) == 1:
        remaining = [choice for choice in valid if _order_id(choice) != _order_id(matches[0])]
        if len(remaining) == 1:
            return {"choice": remaining[0], "selection_source": "exclusion"}
        return None

    if len(matches) == 1:
        return {"choice": matches[0], "selection_source": "product"}
    if len(matches) > 1:
        return None

    if any(marker in query for marker in ("贵一点", "贵的", "金额高")):
        valued = [(choice, _amount_cents(choice)) for choice in valid]
        if all(amount is not None for _, amount in valued):
            maximum = max(int(amount) for _, amount in valued if amount is not None)
            selected = [choice for choice, amount in valued if amount == maximum]
            if len(selected) == 1:
                return {"choice": selected[0], "selection_source": "comparative_higher_amount"}
    if any(marker in query for marker in ("便宜", "便宜的", "便宜一点", "金额低")):
        valued = [(choice, _amount_cents(choice)) for choice in valid]
        if all(amount is not None for _, amount in valued):
            minimum = min(int(amount) for _, amount in valued if amount is not None)
            selected = [choice for choice, amount in valued if amount == minimum]
            if len(selected) == 1:
                return {"choice": selected[0], "selection_source": "comparative_lower_amount"}
    return None
