from FlagEmbedding import FlagReranker

from config import settings

# 启动时加载一次精排模型
_reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)


def rerank(query: str, docs: list[dict], top_k: int = settings.rerank_top_k) -> list[dict]:
    """
    对 hybrid_search 返回的 top-20 逐条精排。
    docs: [{"id": ..., "content": ..., ...}, ...]
    返回 top_k 条，按 rerank_score 降序。
    """
    pairs: list[tuple[str, str]] = [(query, doc["content"]) for doc in docs]
    # 每个 score 对应一对 (query, doc)
    scores: list[float] = _reranker.compute_score(pairs)

    for doc, score in zip(docs, scores):
        doc["rerank_score"] = float(score)

    docs.sort(key=lambda x: x["rerank_score"], reverse=True)
    return docs[:top_k]
