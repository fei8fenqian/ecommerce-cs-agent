"""src/core/retrieve.py — pgvector 向量检索引擎

用法：
    results = vector_search("8000以内游戏本", table="laptop_products",
                            where="price <= 8000 AND product_type = '游戏本'")
    # → [(id, content, score), ...]
"""

import asyncio
import logging

from sentence_transformers import SentenceTransformer

from agent.rag.bm25 import BM25Index
from agent.rag.rerank import rerank
from agent.rag.rrf import rrf_fuse
from agent.rag.runtime_manifest import load_runtime_knowledge_sources
from config import settings
from infra.db_pool import get_connection, put_connection
from infra.model_device import resolve_model_device

_model: SentenceTransformer | None = None
logger = logging.getLogger(__name__)


def _get_model() -> SentenceTransformer:
    """首次实际向量检索时加载 Embedding 模型。"""
    global _model
    if _model is None:
        _model = SentenceTransformer(
            settings.embedding_model,
            device=resolve_model_device(settings.rag_device),
        )
    return _model


# 模块级 表名:bm25
_bm25_cache: dict[tuple[str, tuple[str, ...] | None], BM25Index] = {}

# 不同表的文本列名 表名:列名
_BM25_TABLE_TEXT = {
    "laptop_products": "description",
    "phone_products": "description",
    "knowledge_chunks": "content",
    "component_products": "description",
}


async def _fetch_documents_by_ids(
    table: str,
    ids: list[object],
    *,
    runtime_sources: frozenset[str] | None,
    where: str | None = None,
) -> list[dict]:
    """补齐 BM25-only 候选的完整文档。

    只接受由白名单表和 RRF 产生的 ID；知识库额外强制 runtime manifest 来源过滤，
    因而 BM25 不会成为绕过运行时知识清单的入口。
    """
    if not ids:
        return []
    if table in ("laptop_products", "phone_products"):
        cols = "id, product_name, brand, price, description"
    elif table == "knowledge_chunks":
        if runtime_sources is None:
            raise RuntimeError("知识检索缺少 runtime manifest 来源白名单")
        cols = "id, source, title, content"
    elif table == "component_products":
        cols = "id, product_name, category, price, description, normalized"
    else:
        raise ValueError(f"不支持的表: {table}")

    source_filter = ""
    params: list[object] = [ids]
    where_filter = f" and ({where})" if where else ""
    if table == "knowledge_chunks":
        source_filter = " and source = any(%s)"
        params.append(sorted(runtime_sources))
    sql = f"select {cols} from {table} where id = any(%s){where_filter}{source_filter}"

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        cur = await conn.execute(sql, params)
        results: list[dict] = []
        async for row in cur:
            if table in ("laptop_products", "phone_products"):
                id, product_name, brand, price, description = row
                results.append({"id": id, "content": description, "title": f"{brand} {product_name}", "price": price})
            elif table == "knowledge_chunks":
                id, source, title, content = row
                results.append({"id": id, "content": content, "title": title, "source": source})
            else:
                id, product_name, category, price, description, normalized = row
                results.append(
                    {
                        "id": id,
                        "content": description,
                        "title": product_name,
                        "category": category,
                        "price": price,
                        "normalized": normalized,
                    }
                )
        return results
    finally:
        if conn is not None:
            await put_connection(conn)


async def _get_bm25(
    table: str,
    *,
    runtime_sources: frozenset[str] | None = None,
) -> BM25Index:
    """懒加载 BM25，首次调用建索引，后续命中缓存"""
    if table not in _BM25_TABLE_TEXT:
        raise ValueError(f"BM25 不支持: {table}，可选: {list(_BM25_TABLE_TEXT)}")

    if table == "knowledge_chunks":
        runtime_sources = runtime_sources or load_runtime_knowledge_sources()
    cache_key = (table, tuple(sorted(runtime_sources)) if runtime_sources is not None else None)
    if cache_key in _bm25_cache:
        return _bm25_cache[cache_key]

    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        bm25 = await BM25Index.build_from_db(
            conn,
            table=table,
            text_col=_BM25_TABLE_TEXT[table],
            source_allowlist=runtime_sources,
        )
    finally:
        await put_connection(conn)
    _bm25_cache[cache_key] = bm25
    return bm25


async def warmup_customer_catalog_retrieval() -> None:
    """在服务启动期加载客户商品咨询会使用的检索缓存。

    不预热 reranker：普通商品咨询已经跳过精排，避免额外占用 GPU/内存。预热完成后，
    首个“预算推荐”请求只需执行查询，不再承担 embedding 模型和 BM25 索引初始化。
    """
    await asyncio.to_thread(_get_model)
    await asyncio.gather(
        _get_bm25("laptop_products"),
        _get_bm25("phone_products"),
    )
    logger.info("customer catalog retrieval warmed")


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

    runtime_sources = load_runtime_knowledge_sources() if table == "knowledge_chunks" else None
    return await _vector_search(
        query,
        table=table,
        where=where,
        top_k=top_k,
        runtime_sources=runtime_sources,
    )


async def _vector_search(
    query: str,
    *,
    table: str,
    where: str | None,
    top_k: int,
    runtime_sources: frozenset[str] | None,
) -> list[dict]:
    """执行向量候选召回；knowledge_chunks 必须携带已验证的来源白名单。"""

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

    source_filter = ""
    params: list[object] = [q_vec_str]
    if table == "knowledge_chunks":
        if runtime_sources is None:
            raise RuntimeError("知识检索缺少 runtime manifest 来源白名单")
        source_filter = " and source = any(%s)"
        params.append(sorted(runtime_sources))
    params.extend([q_vec_str, top_k])

    # 表名与列名由上方白名单选择；值和来源列表均通过参数化传递。
    sql = f"""
        select {cols}, 1 - (embedding <=> %s::vector) as score
        from {table}
        where {where}{source_filter}
        order by embedding <=> %s::vector
        limit %s
    """

    conn = None
    try:
        conn = await get_connection()
        await conn.set_autocommit(True)
        if table in ("laptop_products", "phone_products"):
            cur = await conn.execute(sql, params)
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
            cur = await conn.execute(sql, params)
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
            cur = await conn.execute(sql, params)
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
    runtime_sources = load_runtime_knowledge_sources() if table == "knowledge_chunks" else None
    retrieve_vector = await _vector_search(
        query,
        table=table,
        where=where,
        top_k=top_k,
        runtime_sources=runtime_sources,
    )
    bm25 = await _get_bm25(table, runtime_sources=runtime_sources)
    retrieve_bm25 = bm25.search(query, top_k=20)
    rank_vector = [doc["id"] for doc in retrieve_vector if doc.get("id", 0)]
    rank_bm25 = [doc[0] for doc in retrieve_bm25]
    rrf_rank = rrf_fuse(ranking_a=rank_vector, ranking_b=rank_bm25)

    # RRF 的候选是向量和 BM25 的并集。此前只在 vector top-k 里找完整文档，导致
    # 仅由 BM25 命中的专有名词、状态码和 SOP 名称被静默丢弃。
    doc_map = {doc["id"]: doc for doc in retrieve_vector}
    missing_ids = [doc_id for doc_id, _ in rrf_rank if doc_id not in doc_map]
    # ``where`` 是现有商品检索使用的受控 SQL 片段。BM25-only 文档也必须经过同一
    # 过滤条件，不能绕过价格、品类等商品约束。
    if missing_ids:
        fetched_docs = await _fetch_documents_by_ids(
            table,
            missing_ids,
            runtime_sources=runtime_sources,
            where=where,
        )
        doc_map.update({doc["id"]: doc for doc in fetched_docs})
    res: list[dict] = []
    for doc_id, rrf_score in rrf_rank:
        doc = doc_map.get(doc_id)
        if doc is None:
            continue
        doc["rrf_score"] = rrf_score
        res.append(doc)

    if not use_rerank:
        return res
    return rerank(query, res)


async def pre_retrieve_knowledge(
    query: str,
    *,
    top_k: int = 3,
    similarity_threshold: float = settings.pre_rag_similarity_threshold,
) -> list[dict]:
    """为语义路由提供轻量、只读的项目知识摘要。

    Pre-RAG 只使用向量检索，不建立 BM25 索引，也不调用 reranker。相似度不足时
    返回空列表，避免把“最像但无关”的文档强行注入 Router；所有来源仍由
    ``runtime_manifest`` 限制。
    """
    limit = max(1, min(top_k, 3))
    candidates = await vector_search(
        query,
        table="knowledge_chunks",
        top_k=limit,
    )
    return [doc for doc in candidates if float(doc.get("score") or 0.0) >= similarity_threshold][:limit]
