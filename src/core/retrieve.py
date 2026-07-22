"""src/core/retrieve.py — pgvector 向量检索引擎

用法：
    results = vector_search("8000以内游戏本", table="laptop_products",
                            where="price <= 8000 AND product_type = '游戏本'")
    # → [(id, content, score), ...]
"""

import psycopg2
from sentence_transformers import SentenceTransformer

from config import settings

from .bm25 import BM25Index
from .rrf import rrf_fuse


def _connect() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        dbname=settings.pg_dbname,
    )


# 启动时加载一次模型（模块级，不每次请求加载）
_model = SentenceTransformer(settings.embedding_model)

# 用一个短连接建完 BM25 索引就关
_conn = _connect()
_bm25_products = BM25Index(_conn, table="laptop_products", text_col="description")
_bm25_phones = BM25Index(_conn, table="phone_products", text_col="description")
_bm25_knowledge = BM25Index(_conn, table="knowledge_chunks", text_col="content")
_conn.close()


def vector_search(
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

    q_vec = _model.encode(inputs=[query], normalize_embeddings=True)[0].tolist()
    q_vec_str = str(q_vec)
    where = where or "1=1"  # 没有过滤条件时查全表

    if table in ("laptop_products", "phone_products"):
        cols = "id, product_name, brand, price, description"
    elif table == "knowledge_chunks":
        cols = "id, source, title, content"
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

    conn = _connect()
    cur = conn.cursor()

    try:
        if table in ("laptop_products", "phone_products"):
            cur.execute(sql, (q_vec_str, q_vec_str, top_k))
            res = []
            for row in cur.fetchall():
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
            cur.execute(sql, (q_vec_str, q_vec_str, top_k))
            res = []
            for row in cur.fetchall():
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

    # 兜底
    finally:
        cur.close()
        conn.close()

    return []


def hybrid_search(
    query: str,
    *,
    table: str = "laptop_products",
    where: str | None = None,
    top_k: int = 5,
):
    """
    混合检索：向量 + BM25 → RRF 融合 → top-k 结果。
    """
    retrieve_vector = vector_search(query, table=table, where=where, top_k=top_k)
    if table == "laptop_products":
        retrieve_bm25 = _bm25_products.search(query, top_k=20)
    elif table == "phone_products":
        retrieve_bm25 = _bm25_phones.search(query, top_k=20)
    elif table == "knowledge_chunks":
        retrieve_bm25 = _bm25_knowledge.search(query, top_k=20)
    else:
        raise ValueError(f"无法检索表{table}")
    rank_vector = [doc["id"] for doc in retrieve_vector if doc.get("id", 0)]
    rank_bm25 = [doc[0] for doc in retrieve_bm25]
    # rank: list[tuple[doc_id, score]]
    rrf_rank = rrf_fuse(ranking_a=rank_vector, ranking_b=rank_bm25)

    # 根据doc_id索引完整doc
    doc_map = {doc["id"]: doc for doc in retrieve_vector}
    # 返回[{"id": ..., "content": ..., "score": ..., "source": ..., "title": ..., ...}, ...]
    res: list[dict] = []
    for doc_id, rrf_score in rrf_rank:
        # BM25 独有的，跳过
        doc = doc_map.get(doc_id)
        if doc is None:
            continue
        doc["rrf_score"] = rrf_score
        res.append(doc)
    return res[:top_k]
