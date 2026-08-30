"""
src/core/bm25.py — BM25 关键词检索

从 PG 表读文本，jieba 分词建倒排索引。
用法：
    bm25_prods = BM25Index(conn, table="laptop_products", text_col="description")
    ids = bm25_prods.search("8000以内游戏本", top_k=20)  # → ["abc123", ...]
"""

import math
from collections import Counter
from collections.abc import Collection
from typing import Any

import jieba
from psycopg import AsyncConnection


class BM25Index:
    def __init__(self, docs: list[dict[str, Any]], idf: dict[str, float], avglen: float = 1.0):
        self.docs = docs
        self.idf = idf
        self.avglen = avglen

    @classmethod
    async def build_from_db(
        cls,
        conn: AsyncConnection,
        table: str,
        text_col: str = "description",
        *,
        source_allowlist: Collection[str] | None = None,
    ):
        """
        从 PG 表读取文本，建 BM25 索引。

        conn:      psycopg3 连接
        table:    表名（laptop_products / knowledge_chunks）
        text_col: 文本列名（description / content）
        """
        if source_allowlist is not None and table != "knowledge_chunks":
            raise ValueError("source_allowlist 仅适用于 knowledge_chunks")

        docs: list[dict[str, Any]] = []
        total_len = 0
        sql = f"select id,{text_col} from {table}"
        params: tuple[list[str], ...] = ()
        if source_allowlist is not None:
            sql += " where source = any(%s)"
            params = (sorted(source_allowlist),)

        async for row in await conn.execute(sql, params):
            id, content = row
            words = jieba.lcut(content)
            tokens = Counter(words)
            docs.append({"id": id, "tokens": tokens, "length": len(words)})
            total_len += len(words)

        # 文档平均长度
        avglen = total_len / len(docs) if docs else 1
        # 文档数量
        doc_count = len(docs)

        # 建倒排索引  {词: {doc_id: 词频}}
        inverted: dict[str, dict[str, int]] = {}
        for doc in docs:
            for word, freq in doc["tokens"].items():
                # 为新词建空词典
                if word not in inverted:
                    inverted[word] = {}
                inverted[word][doc["id"]] = freq

        # 预计算每个词的 IDF(逆文档频率 计算词存在于在哪些文档中 文档数越低得分越高)
        idf: dict[str, float] = {}
        for word, word_docs in inverted.items():
            counts = len(word_docs)
            # 使用始终为正的 Robertson/Sparck Jones 变体，避免常见词的负 IDF
            # 让“未命中 score=0”的文档排在真正命中文档前面。
            idf[word] = math.log(1 + (doc_count - counts + 0.5) / (counts + 0.5))

        return cls(docs, idf, avglen)

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """
        BM25 检索，返回 [(doc_id, bm25_score), ...] 按分降序。
        """
        k1, b = 1.5, 0.75
        words = jieba.lcut(query)
        doc_score: list[tuple[str, float]] = []
        for doc in self.docs:
            score = 0.0
            for word in words:
                # 词在当前文档出现频率
                tf = doc["tokens"].get(word, 0)
                if tf == 0:
                    continue
                # 全局词频
                idf = self.idf.get(word, 0)
                # BM25 公式
                numerator = tf * (k1 + 1)
                denominator = tf + k1 * (1 - b + b * doc["length"] / self.avglen)
                score += idf * numerator / denominator
            doc_score.append((doc["id"], score))

        # BM25-only 检索只返回至少命中一个 query token 的文档；零分候选不能
        # 因为 top_k 被填满而污染 Hybrid 的 RRF 候选集合。
        doc_score = [(doc_id, score) for doc_id, score in doc_score if score > 0]
        doc_score.sort(key=lambda x: x[1], reverse=True)
        return doc_score[:top_k]
