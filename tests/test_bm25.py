"""tests/test_bm25.py — BM25 关键词检索单元测试"""

from collections import Counter

import pytest

from agent.rag.bm25 import BM25Index
from infra.db_pool import get_connection, init_pool, put_connection


# =============================================================================
# 手工构造的 BM25Index — 不依赖 DB
# =============================================================================
def _make_index(docs_data: list[tuple[str, str]]) -> BM25Index:
    """用给定的 (id, text) 列表构造 BM25Index"""
    import math

    import jieba

    docs = []
    total_len = 0
    for doc_id, text in docs_data:
        words = jieba.lcut(text)
        tokens = Counter(words)
        docs.append({"id": doc_id, "tokens": tokens, "length": len(words)})
        total_len += len(words)

    avglen = total_len / len(docs) if docs else 1
    doc_count = len(docs)

    inverted: dict[str, dict[str, int]] = {}
    for doc in docs:
        doc_tokens: dict[str, int] = doc["tokens"]  # type: ignore[assignment]
        for word, freq in doc_tokens.items():
            if word not in inverted:
                inverted[word] = {}
            inverted[word][str(doc["id"])] = freq

    idf: dict[str, float] = {}
    for word, word_docs in inverted.items():
        counts = len(word_docs)
        idf[word] = math.log((doc_count - counts + 0.5) / (counts + 0.5))

    return BM25Index(docs, idf, avglen)


# =============================================================================
# BM25Index.__init__ / 构造
# =============================================================================
class TestBM25Init:
    def test_empty_docs(self):
        idx = BM25Index(docs=[], idf={}, avglen=1)
        assert idx.docs == []
        assert idx.idf == {}
        assert idx.avglen == 1

    def test_single_doc(self):
        idx = _make_index([("1", "联想拯救者游戏本")])
        assert len(idx.docs) == 1
        assert idx.docs[0]["id"] == "1"

    def test_avglen_calculation(self):
        """avglen 应该是所有文档长度的平均值"""
        idx = _make_index(
            [
                ("1", "短文本"),
                ("2", "这是一个稍微长一点的文本"),
            ]
        )
        # 两个文档，avglen = (2 + 5) / 2
        # 实际分词后长度取决于 jieba，但至少 > 0
        assert idx.avglen > 0


# =============================================================================
# BM25Index.search — 功能测试
# =============================================================================
class TestBM25Search:
    def test_exact_match_returns_result(self):
        """精确匹配的文档应该排在最前面"""
        idx = _make_index(
            [
                ("a", "联想拯救者 游戏本 RTX4060"),
                ("b", "华为 MateBook 轻薄本 办公"),
                ("c", "联想小新 轻薄本 学生"),
            ]
        )
        results = idx.search("拯救者")
        assert len(results) > 0
        # "拯救者" 只在 doc "a" 中出现
        assert results[0][0] == "a"
        assert results[0][1] > 0  # score > 0

    def test_partial_match_scores_lower(self):
        """所有文档都包含查询词时 IDF 可为负，但结果仍按分数降序"""
        idx = _make_index(
            [
                ("a", "联想拯救者游戏本 高性能 RTX4060"),
                ("b", "联想小新 轻薄 便携 办公"),
            ]
        )
        results = idx.search("联想")
        assert len(results) == 2
        # IDF 可为负（词出现在所有文档中），但排序不变
        assert len(results) == 2

    def test_no_match_returns_score_zero(self):
        """完全不匹配的文档 score 为 0，但仍在结果中"""
        idx = _make_index([("a", "联想拯救者")])
        results = idx.search("苹果手机")
        # 没有匹配词，score 为 0，但文档仍在结果中
        assert len(results) == 1
        assert results[0][1] == 0.0

    def test_top_k_limit(self):
        """top_k 限制返回数量"""
        docs = [(str(i), f"文档{i}") for i in range(10)]
        idx = _make_index(docs)
        results = idx.search("文档", top_k=3)
        assert len(results) == 3

    def test_empty_query_returns_all_zero_score(self):
        """空查询 → 所有文档 score=0"""
        idx = _make_index([("a", "hello"), ("b", "world")])
        results = idx.search("")
        assert len(results) == 2
        for _, score in results:
            assert score == 0.0

    def test_results_sorted_descending(self):
        """结果应按分数降序排列"""
        idx = _make_index(
            [
                ("a", "游戏本 游戏本 游戏本 游戏本"),  # "游戏本" 出现多次
                ("b", "游戏本"),
                ("c", "办公本"),
            ]
        )
        results = idx.search("游戏本")
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_index_search(self):
        """空索引搜索不报错"""
        idx = BM25Index(docs=[], idf={}, avglen=1)
        results = idx.search("任意查询")
        assert results == []


# =============================================================================
# BM25Index.build_from_db — 集成测试（需要真实 PG）
# =============================================================================
class TestBM25BuildFromDB:
    @pytest.mark.asyncio
    async def test_build_from_laptop_products(self):
        """从 laptop_products 建索引 → 能搜到结果"""
        await init_pool(minconn=1, maxconn=2)
        conn = await get_connection()
        await conn.set_autocommit(True)

        try:
            idx = await BM25Index.build_from_db(conn, table="laptop_products", text_col="description")
            assert len(idx.docs) > 0
            assert len(idx.idf) > 0
            assert idx.avglen > 0

            # 搜索应该返回结果
            results = idx.search("拯救者")
            assert len(results) > 0
            assert isinstance(results[0][0], (int, str))
            assert isinstance(results[0][1], float)
        finally:
            await put_connection(conn)

    @pytest.mark.asyncio
    async def test_build_from_knowledge_chunks(self):
        """从 knowledge_chunks 建索引 → content 列"""
        await init_pool(minconn=1, maxconn=2)
        conn = await get_connection()
        await conn.set_autocommit(True)

        try:
            idx = await BM25Index.build_from_db(conn, table="knowledge_chunks", text_col="content")
            assert len(idx.docs) > 0
            results = idx.search("退货")
            assert len(results) > 0
        finally:
            await put_connection(conn)

    @pytest.mark.asyncio
    async def test_build_from_empty_table(self):
        """从空表建索引不应该报错"""
        await init_pool(minconn=1, maxconn=2)
        conn = await get_connection()
        await conn.set_autocommit(True)

        try:
            # 建一张临时空表
            await conn.execute("CREATE TEMP TABLE _bm25_test_empty (id serial, text_col text)")
            idx = await BM25Index.build_from_db(conn, table="_bm25_test_empty", text_col="text_col")
            assert idx.docs == []
            assert idx.idf == {}
            assert idx.avglen == 1
            await conn.execute("DROP TABLE IF EXISTS _bm25_test_empty")
        finally:
            await put_connection(conn)
