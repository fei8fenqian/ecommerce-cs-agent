"""src/core/retrieve.py — pgvector 向量检索引擎

用法：
    results = vector_search("8000以内游戏本", table="laptop_products",
                            where="price <= 8000 AND product_type = '游戏本'")
    # → [(id, content, score), ...]
"""

from sentence_transformers import SentenceTransformer

from agent.rag.bm25 import BM25Index
from agent.rag.rerank import rerank
from agent.rag.rrf import rrf_fuse
from config import settings
from infra.db_pool import get_connection, put_connection

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """首次实际向量检索时加载 Embedding 模型。"""
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.embedding_model)
    return _model


# 模块级 表名:bm25
_bm25_cache: dict[str, BM25Index] = {}

# 不同表的文本列名 表名:列名
_BM25_TABLE_TEXT = {
    "laptop_products": "description",
    "phone_products": "description",
    "knowledge_chunks": "content",
    "component_products": "description",
}


async def _get_bm25(table: str) -> BM25Index:
    """懒加载 BM25，首次调用建索引，后续命中缓存"""
    if table not in _BM25_TABLE_TEXT:
        raise ValueError(f"BM25 不支持: {table}，可选: {list(_BM25_TABLE_TEXT)}")

    if table in _bm25_cache:
        return _bm25_cache[table]

    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        bm25 = await BM25Index.build_from_db(conn, table=table, text_col=_BM25_TABLE_TEXT[table])
    finally:
        await put_connection(conn)
    _bm25_cache[table] = bm25
    return bm25


async def vector_search(
    query: str,
    *,
    table: str = "laptop_products",
    where: str | None = None,
    top_k: int = settings.retrieval_top_k,
) -> list[dict]:
    """
        向量相似度检索。

        query:       用户问题
        table:       查哪张表（laptop_products / knowledge_chunks）
        where:       附加 SQL 过滤（如 "price <= 8000"），None 表示不加
        top_k:       返回几条，默认走 settings.retrieval_top_k

        返回 [{"id": ..., "content": ..., "score": ..., "source": ..., "title": ...,
    ...}, ...]
    """

    q_vec = _get_model().encode(inputs=[query], normalize_embeddings=True)[0].tolist()
    q_vec_str = str(q_vec)
    where = where or "1=1"  # 没有过滤条件时查全表

    if table in ("laptop_products", "phone_products"):
        cols = "id, product_name, brand, price, description"
    elif table == "knowledge_chunks":
        cols = "id, source, title, content"
    elif table == "component_products":
        cols = "id, product_name, category, price, description, normalized"
    else:
        raise ValueError(f"不支持的表: {table}")

    # %s 会自动转义、加引号
    sql = f"""
        select {cols}, 1 - (embedding <=> %s::vector) as score
        from {table}
        where {where}
        order by embedding <=> %s::vector
        limit %s
    """

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        if table in ("laptop_products", "phone_products"):
            cur = await conn.execute(sql, (q_vec_str, q_vec_str, top_k))
            res = []
            async for row in cur:
                id, product_name, brand, price, description, score = row
                res.append(
                    {
                        "id": id,
                        "content": description,
                        "score": score,
                        "title": f"{brand} {product_name}",
                        "price": price,
                    }
                )
            return res

        elif table == "knowledge_chunks":
            cur = await conn.execute(sql, (q_vec_str, q_vec_str, top_k))
            res = []
            async for row in cur:
                id, source, title, content, score = row
                res.append(
                    {
                        "id": id,
                        "content": content,
                        "score": score,
                        "title": title,
                        "source": source,
                    }
                )
            return res

        elif table == "component_products":
            cur = await conn.execute(sql, (q_vec_str, q_vec_str, top_k))
            res = []
            async for row in cur:
                id, product_name, category, price, description, normalized, score = row
                res.append(
                    {
                        "id": id,
                        "content": description,
                        "score": score,
                        "title": product_name,
                        "category": category,
                        "price": price,
                        "normalized": normalized,
                    }
                )
            return res

    finally:
        if conn is not None:
            await put_connection(conn)

    return []


async def hybrid_search(
    query: str,
    *,
    table: str = "laptop_products",
    where: str | None = None,
    top_k: int = settings.retrieval_top_k,
    use_rerank: bool = True,
) -> list[dict]:
    """
    混合检索：向量 + BM25 → RRF 融合 → (可选) rerank 精排。

    use_rerank=False 时跳过精排直接返回 RRF 融合结果，用于消融实验。
    """
    retrieve_vector = await vector_search(query, table=table, where=where, top_k=top_k)
    bm25 = await _get_bm25(table)
    retrieve_bm25 = bm25.search(query, top_k=20)
    rank_vector = [doc["id"] for doc in retrieve_vector if doc.get("id", 0)]
    rank_bm25 = [doc[0] for doc in retrieve_bm25]
    rrf_rank = rrf_fuse(ranking_a=rank_vector, ranking_b=rank_bm25)

    # 根据doc_id索引完整doc
    doc_map = {doc["id"]: doc for doc in retrieve_vector}
    res: list[dict] = []
    for doc_id, rrf_score in rrf_rank:
        # BM25 独有的，跳过
        doc = doc_map.get(doc_id)
        if doc is None:
            continue
        doc["rrf_score"] = rrf_score
        res.append(doc)

    if not use_rerank:
        return res
    return rerank(query, res)
