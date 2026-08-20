from FlagEmbedding import FlagReranker

from config import settings

_reranker: FlagReranker | None = None


def _get_reranker() -> FlagReranker:
    """在首次实际精排时加载模型，避免非检索命令触发模型下载。"""
    global _reranker
    if _reranker is None:
        # 面试演示环境使用 CPU；FP16 仅适用于受支持的 GPU。
        _reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=False)
    return _reranker


def rerank(query: str, docs: list[dict], top_k: int = settings.rerank_top_k) -> list[dict]:
    """
    对 hybrid_search 返回的 top-20 逐条精排。
    docs: [{"id": ..., "content": ..., ...}, ...]
    返回 top_k 条，按 rerank_score 降序。
    """
    pairs: list[tuple[str, str]] = [(query, doc["content"]) for doc in docs]
    # 每个 score 对应一对 (query, doc)
    scores: list[float] = _get_reranker().compute_score(pairs)

    for doc, score in zip(docs, scores):
        doc["rerank_score"] = float(score)

    docs.sort(key=lambda x: x["rerank_score"], reverse=True)
    return docs[:top_k]
