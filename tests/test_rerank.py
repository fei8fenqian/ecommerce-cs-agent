from unittest.mock import Mock, patch

from agent.rag.rerank import rerank


def test_rerank_empty_candidates_skips_model_loading():
    """没有候选不是模型故障，也不应触发精排模型加载。"""
    with patch("agent.rag.rerank._get_reranker") as get_reranker:
        assert rerank("Wi-Fi 信号很差", []) == []
    get_reranker.assert_not_called()


def test_rerank_accepts_scalar_score_for_single_candidate():
    """兼容 FlagEmbedding 对单条 pair 返回标量分数的版本。"""
    model = Mock()
    model.compute_score.return_value = 0.87
    docs = [{"id": 1, "content": "检查路由器与无线网卡驱动。"}]

    with patch("agent.rag.rerank._get_reranker", return_value=model):
        result = rerank("Wi-Fi 信号很差", docs)

    assert result[0]["rerank_score"] == 0.87
