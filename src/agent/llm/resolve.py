"""agent/resolve.py — 指代消解

用规则将用户查询中的指代词（"它""这台""那款"等）替换为上一轮识别到的实体名称。
规则做指代消解，比 LLM 快且确定。
"""

from typing import Any, Mapping

PRONOUN_MAP: dict[str, str] = {
    # 复合表达必须在“这款 / 那款”前替换，否则会留下“刚刚商品名”这种残句。
    "刚刚那款": "product",
    "刚才那款": "product",
    "刚才推荐的": "product",
    "上一款": "product",
    "前面那款": "product",
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
_INVENTORY_QUESTION_MARKERS = ("库存", "现货", "有货")
_INVENTORY_OFFER_MARKERS = ("需要我", "要不要我", "是否需要", "要不要")


def resolve_pronouns(query: str, entities: Mapping[str, Any]) -> str:
    """用上一轮识别的实体补全指代词和省略商品的购买命令。"""
    if not entities:
        return query
    for pronoun, key in PRONOUN_MAP.items():
        entity = entities.get(key, "")
        if isinstance(entity, str) and entity and pronoun in query:
            query = query.replace(pronoun, entity)

    product = entities.get("product", "")
    if (
        isinstance(product, str)
        and product
        and product not in query
        and any(action in query for action in _IMPLICIT_PRODUCT_ACTIONS)
    ):
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
        if (
            isinstance(content, str)
            and any(marker in content for marker in _INVENTORY_QUESTION_MARKERS)
            and any(marker in content for marker in _INVENTORY_OFFER_MARKERS)
        ):
            product = entities.get("product", "").strip()
            if not product:
                # Natural-language recommendation prose is presentation, not a
                # product identity protocol.  Without a server-canonicalized
                # selected product, keep the customer utterance unresolved.
                return query
            return f"查询 {product} 的实时库存"
        break
    return query
