"""tests/test_rrf.py — RRF 融合算法单元测试"""

from agent.rag.rrf import rrf_fuse


def test_rrf_identical_rankings():
    """两个排名完全一样，top-1 应该最高分"""
    a = ["doc1", "doc2", "doc3"]
    b = ["doc1", "doc2", "doc3"]
    result = rrf_fuse(a, b)
    assert result[0][0] == "doc1"
    assert result[1][0] == "doc2"


def test_rrf_reversed_rankings():
    """两个排名反过来，各自的 top-1 总分应该持平"""
    a = ["doc1", "doc2"]
    b = ["doc2", "doc1"]
    result = rrf_fuse(a, b)
    # doc1: 1/(60+1) + 1/(60+2)  vs  doc2: 1/(60+2) + 1/(60+1)
    # 分数一样，谁在前面取决于排序稳定性，但不会丢
    ids = {r[0] for r in result}
    assert ids == {"doc1", "doc2"}


def test_rrf_bm25_only_docs():
    """BM25 有的 doc 向量没有，doc 不会丢"""
    a = ["doc1", "doc2"]  # 向量
    b = ["doc3", "doc1"]  # BM25（doc3 只在 BM25 里出现）
    result = rrf_fuse(a, b)
    ids = {r[0] for r in result}
    assert "doc3" in ids
    assert len(result) == 3


def test_rrf_empty_input():
    """空排名不报错"""
    result = rrf_fuse([], ["doc1", "doc2"])
    assert result == [("doc1", 1 / 61), ("doc2", 1 / 62)]


def test_rrf_scores_descending():
    """分数必须降序排列"""
    a = ["a", "b", "c", "d"]
    b = ["a", "b", "c", "d"]
    result = rrf_fuse(a, b)
    scores = [s for _, s in result]
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], f"第 {i} 个分数 < 第 {i + 1} 个"
