"""agent/resolve.py — 指代消解

用规则将用户查询中的指代词（"它""这台""那款"等）替换为上一轮识别到的实体名称。
规则做指代消解，比 LLM 快且确定。
"""

import re

PRONOUN_MAP: dict[str, str] = {
    "它": "product",
    "他": "product",
    "这个": "product",
    "这台": "product",
    "那台": "product",
    "这款": "product",
    "该商品": "product",
    "该产品": "product",
    "这单": "order",
    "那个订单": "order",
    "该订单": "order",
}

_IMPLICIT_PRODUCT_ACTIONS = ("下单", "购买", "买下", "就买", "要这个")
_STOCK_CONFIRMATIONS = frozenset({"需要", "要", "查一下", "查下", "查询一下", "好的，需要", "好的，查一下"})


def resolve_pronouns(query: str, entities: dict[str, str]) -> str:
    """用上一轮识别的实体补全指代词和省略商品的购买命令。"""
    if not entities:
        return query
    for pronoun, key in PRONOUN_MAP.items():
        entity = entities.get(key, "")
        if entity and pronoun in query:
            query = query.replace(pronoun, entity)

    product = entities.get("product", "")
    if product and product not in query and any(action in query for action in _IMPLICIT_PRODUCT_ACTIONS):
        query = f"{query}，商品为 {product}"
    return query


def resolve_stock_follow_up(
    query: str,
    entities: dict[str, str],
    history: list[dict[str, object]],
) -> str:
    """将客服刚提出的“要查库存吗”承接为明确库存查询。

    Args:
        query: 当前用户输入。
        entities: 会话中已确认的实体，例如最近推荐的商品。
        history: 当前会话的可见历史，用于确认上一轮确实在询问库存。

    Returns:
        能确定上下文时返回完整库存查询；其他情况保持用户原话。
    """
    normalized = "".join(query.split()).lower()
    if normalized not in _STOCK_CONFIRMATIONS:
        return query

    for message in reversed(history):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str) and "库存" in content and "查询" in content:
            product = entities.get("product", "").strip() or _extract_recommended_product(content)
            return f"查询 {product or '上轮首选机型'} 的实时库存"
        break
    return query


def _extract_recommended_product(reply: str) -> str:
    """从客服刚给出的推荐中提取首选商品，作为短确认的默认操作对象。"""
    match = re.search(r"(?:首选推荐|推荐首选|首推|首选)\s*[：:]\s*([^\n（(]+)", reply)
    if match is None:
        return ""
    return match.group(1).strip().strip("* ")
