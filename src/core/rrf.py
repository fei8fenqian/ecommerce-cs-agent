def rrf_fuse(ranking_a: list[str], ranking_b: list[str], k: int = 60) -> list[tuple[str, float]]:
    """
    Reciprocal Rank Fusion 把 BM25 和向量检索两个排名融合到一起
    RRF = Σ 1/(k+rank)，返回 [(路径, 分数), ...] 按分降序
    """
    scores: dict[str, float] = {}
    for rank, item_id in enumerate(ranking_a, start=1):
        scores[item_id] = scores.get(item_id, 0) + 1 / (k + rank)
    for rank, item_id in enumerate(ranking_b, start=1):
        scores[item_id] = scores.get(item_id, 0) + 1 / (k + rank)
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_scores
