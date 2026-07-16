"""
src/core/bm25.py — BM25 关键词检索

从 PG 表读文本，jieba 分词建倒排索引。
用法：
    bm25_prods = BM25Index(conn, table="laptop_products", text_col="description")
    ids = bm25_prods.search("8000以内游戏本", top_k=20)  # → ["abc123", ...]
"""

import math
from collections import Counter

import jieba


class BM25Index:
    def __init__(self, conn, *, table: str, text_col: str = "description"):
        """
        从 PG 表读取文本，建 BM25 索引。

        conn:      psycopg2 连接
        table:    表名（laptop_products / knowledge_chunks）
        text_col: 文本列名（description / content）
        """

        # 从 PG 读数据
        cur = conn.cursor()
        cur.execute(f"select id,{text_col} from {table}")
        rows = cur.fetchall()
        cur.close()

        # 对每条文本 jieba 分词，统计词频
        # self.docs = [{"id": str, "tokens": Counter, "length": int}, ...]
        self.docs = []
        total_len = 0
        for id, content in rows:
            # tokens:Counter({word: freq, ...})
            words = jieba.lcut(content)
            tokens = Counter(words)
            self.docs.append({"id": id, "tokens": tokens, "length": len(words)})
            total_len += len(words)

        # 文档平均长度
        self.avglen = total_len / len(self.docs) if self.docs else 1
        # 文档数量
        self.doc_count = len(self.docs)

        # 建倒排索引  {词: {doc_id: 词频}}
        self.inverted: dict[str, dict[str, int]] = {}
        for doc in self.docs:
            for word, freq in doc["tokens"].items():
                # 为新词建空词典
                if word not in self.inverted:
                    self.inverted[word] = {}
                self.inverted[word][doc["id"]] = freq

        # 预计算每个词的 IDF(逆文档频率 计算词存在于在哪些文档中 文档数越低得分越高)
        self.idf: dict[str, float] = {}
        for word, word_docs in self.inverted.items():
            counts = len(word_docs)
            self.idf[word] = math.log((self.doc_count - counts + 0.5) / (counts + 0.5))

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

        doc_score.sort(key=lambda x: x[1], reverse=True)
        return doc_score[:top_k]
