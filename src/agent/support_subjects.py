"""客服 Case 中客户选择的确定性解析。

这个模块只把客户对已展示候选的回复解析成一个服务端已经允许的 subject。
它不做意图路由，也不从数据库重新查询或重新排序候选，避免把自然语言再次交给
模型选择订单。
"""

from __future__ import annotations

import re
from typing import Any

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


def _matching_choices(query: str, choices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized_query = _normalize(query)
    matches: list[dict[str, Any]] = []
    for choice in choices:
        product = _normalize(_product_name(choice))
        if product and product in normalized_query:
            matches.append(choice)
            continue
        tokens = _product_tokens(_product_name(choice))
        if tokens and any(token in normalized_query for token in tokens):
            matches.append(choice)
    return matches


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
    return len(_matching_choices(raw_query, valid)) == 1


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
