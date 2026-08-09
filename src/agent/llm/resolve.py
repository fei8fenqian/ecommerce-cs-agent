"""agent/resolve.py — 指代消解

用规则将用户查询中的指代词（"它""这台""那款"等）替换为上一轮识别到的实体名称。
规则做指代消解，比 LLM 快且确定。
"""

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


def resolve_pronouns(query: str, entities: dict[str, str]) -> str:
    """用上一轮识别的实体替换指代词。"""
    if not entities:
        return query
    for pronoun, key in PRONOUN_MAP.items():
        entity = entities.get(key, "")
        if entity and pronoun in query:
            query = query.replace(pronoun, entity)
    return query
