"""
scripts/eval.py — 检索方案消融实验

四种方案:
  A: 纯 BM25 关键词检索
  B: 纯 pgvector 向量检索
  C: BM25 + 向量 RRF 混合
  D: C + Rerank 精排

指标: Hit@1 / Hit@3 / Hit@5 / MRR / NDCG@5

用法:
  python scripts/eval.py                  # 全量 50 题
  python scripts/eval.py --limit 10       # 只跑前 10 题（快速抽查）
  python scripts/eval.py --skip-rerank    # 跳过 D 方案（没 GPU 时用）
"""

import json
import math
import time
from pathlib import Path
from typing import Any

from .db import connect_db

# ── 路径 ──────────────────────────────────────────
ROOT = Path(__file__).parent.parent
QUESTIONS_PATH = ROOT / "data" / "test_questions.json"

# ── 连接 ──────────────────────────────────────────
CONN = connect_db()

# BM25 只返回 (id, score)，需要回查 content 才能判断相关性
_CONTENT_CACHE: dict[str, dict[str, Any]] = {}


def _build_content_cache(table: str):
    """预加载某张表的 id → content/title 映射"""
    if table in _CONTENT_CACHE:
        return
    cur = CONN.cursor()
    if table == "laptop_products":
        cur.execute("select id, description, product_name, brand from laptop_products")
        cache = {}
        for row in cur.fetchall():
            cache[row[0]] = {"content": row[1] or "", "title": f"{row[3]} {row[2]}"}
        _CONTENT_CACHE[table] = cache
    elif table == "phone_products":
        cur.execute("select id, description, product_name, brand from phone_products")
        cache = {}
        for row in cur.fetchall():
            cache[row[0]] = {"content": row[1] or "", "title": f"{row[3]} {row[2]}"}
        _CONTENT_CACHE[table] = cache
    elif table == "knowledge_chunks":
        cur.execute("select id, content, title, source from knowledge_chunks")
        cache = {}
        for row in cur.fetchall():
            cache[row[0]] = {"content": row[1] or "", "title": row[2] or ""}
        _CONTENT_CACHE[table] = cache
    cur.close()


# ── 延迟导入（避免 eval.py 被 import 时加载全部模型）────
_vector_search = None
_hybrid_search = None
_rerank = None
_bm25_products = None
_bm25_phones = None
_bm25_knowledge = None


def _lazy_import():
    """首次调用时加载模型和索引，避免 import eval 就 OOM"""
    global _vector_search, _hybrid_search, _rerank, _bm25_products, _bm25_phones, _bm25_knowledge

    if _vector_search is not None:
        return

    from core.rerank import rerank as rr
    from core.retrieve import _bm25_knowledge as bk
    from core.retrieve import _bm25_phones as bph
    from core.retrieve import _bm25_products as bp
    from core.retrieve import hybrid_search as hs
    from core.retrieve import vector_search as vs

    _vector_search = vs
    _hybrid_search = hs
    _bm25_products = bp
    _bm25_phones = bph
    _bm25_knowledge = bk
    _rerank = rr


# ── 相关性判断 ────────────────────────────────────
def is_relevant(doc: dict, expected_keywords: list[str]) -> bool:
    """doc 的内容是否包含至少一个预期关键词"""
    if not expected_keywords:
        return False
    text = (doc.get("content") or "") + " " + (doc.get("title") or "")
    return any(kw.lower() in text.lower() for kw in expected_keywords)


# ── 指标计算 ─────────────────────────────────────
def dcg(relevances: list[int]) -> float:
    """Discounted Cumulative Gain"""
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def ndcg_at_k(relevances: list[int], k: int) -> float:
    """Normalized DCG@K"""
    ideal = sorted(relevances, reverse=True)
    dcg_val = dcg(relevances[:k])
    idcg_val = dcg(ideal[:k])
    return dcg_val / idcg_val if idcg_val > 0 else 0.0


def calc_metrics(
    results: list[dict],
    expected_keywords: list[str],
    k_vals: tuple[int, ...] = (1, 3, 5),
) -> dict:
    """对单次查询计算所有指标"""
    # 标记每篇文档是否相关
    rel = [1 if is_relevant(doc, expected_keywords) else 0 for doc in results]

    metrics = {}
    for k in k_vals:
        metrics[f"hit@{k}"] = 1.0 if sum(rel[:k]) > 0 else 0.0

    # MRR: 第一个相关文档的倒数排名
    for rank, r in enumerate(rel, start=1):
        if r == 1:
            metrics["mrr"] = 1.0 / rank
            break
    else:
        metrics["mrr"] = 0.0

    # NDCG@5
    metrics["ndcg@5"] = ndcg_at_k(rel, 5)

    return metrics


# ── 方案执行 ─────────────────────────────────────
def run_scheme_a(query: str, table: str, top_k: int = 5) -> list[dict]:
    """纯 BM25"""
    _lazy_import()
    _build_content_cache(table)
    cache = _CONTENT_CACHE[table]
    if table == "laptop_products":
        bm25 = _bm25_products
    elif table == "phone_products":
        bm25 = _bm25_phones
    else:
        bm25 = _bm25_knowledge
    assert bm25 is not None, "BM25 索引未初始化"
    results = bm25.search(query, top_k=top_k)
    return [
        {
            "id": doc_id,
            "content": str(cache.get(doc_id, {}).get("content", "")),
            "title": str(cache.get(doc_id, {}).get("title", "")),
            "score": score,
        }
        for doc_id, score in results
    ]


def run_scheme_b(query: str, table: str, top_k: int = 5) -> list[dict]:
    """纯向量检索"""
    _lazy_import()
    assert _vector_search is not None
    return _vector_search(query, table=table, top_k=top_k)


def run_scheme_c(query: str, table: str, top_k: int = 5) -> list[dict]:
    """BM25 + 向量 RRF 混合"""
    _lazy_import()
    assert _hybrid_search is not None
    return _hybrid_search(query, table=table, top_k=top_k)


def run_scheme_d(query: str, table: str, top_k: int = 5) -> list[dict]:
    """Hybrid + Rerank"""
    _lazy_import()
    assert _hybrid_search is not None and _rerank is not None
    candidates = _hybrid_search(query, table=table, top_k=20)
    return _rerank(query, candidates, top_k=top_k)


SCHEMES = {
    "A: BM25": run_scheme_a,
    "B: Vector": run_scheme_b,
    "C: BM25+Vector(RRF)": run_scheme_c,
    "D: C+Rerank": run_scheme_d,
}


# ── 主流程 ───────────────────────────────────────
def main(limit: int | None = None, skip_rerank: bool = False):
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if limit:
        questions = questions[:limit]

    # 聚合指标
    agg: dict[str, dict[str, float]] = {
        name: {
            "hit@1": 0.0,
            "hit@3": 0.0,
            "hit@5": 0.0,
            "mrr": 0.0,
            "ndcg@5": 0.0,
            "latency_ms": 0.0,
        }
        for name in SCHEMES
    }
    # 按难度统计
    difficulty_agg: dict[str, dict[str, dict[str, float]]] = {}

    total = len(questions)
    print(f"{'=' * 80}")
    print(f"检索消融实验 — {total} 题")
    print(f"{'=' * 80}")

    for idx, q in enumerate(questions, 1):
        query = q["query"]
        expected = q.get("expected_keywords", [])
        table = q.get("expected_source", "laptop_products")
        difficulty = q.get("difficulty", "medium")

        if difficulty not in difficulty_agg:
            difficulty_agg[difficulty] = {
                name: {
                    "hit@1": 0.0,
                    "hit@3": 0.0,
                    "hit@5": 0.0,
                    "mrr": 0.0,
                    "ndcg@5": 0.0,
                    "latency_ms": 0.0,
                }
                for name in SCHEMES
            }

        # 无关键词的开放题跳过指标计算，只展示结果
        skip_metrics = not expected

        print(f"\n[{idx}/{total}] {query}")
        print(
            f"  type={q['type']}  difficulty={difficulty}"
            f"{' (开放题，跳过打分)' if skip_metrics else ''}"
        )

        for scheme_name, scheme_fn in SCHEMES.items():
            if skip_rerank and scheme_name == "D: C+Rerank":
                continue

            t0 = time.perf_counter()
            try:
                results = scheme_fn(query, table=table)
            except Exception as exc:
                print(f"    {scheme_name}: ERROR — {exc}")
                continue
            elapsed = (time.perf_counter() - t0) * 1000

            if not skip_metrics:
                metrics = calc_metrics(results, expected)
                for key in agg[scheme_name]:
                    if key == "latency_ms":
                        agg[scheme_name][key] += elapsed
                    else:
                        agg[scheme_name][key] += metrics.get(key, 0.0)
                # 按难度
                for key in difficulty_agg[difficulty][scheme_name]:
                    if key != "latency_ms":
                        difficulty_agg[difficulty][scheme_name][key] += metrics.get(key, 0.0)

            # 打印 Top-3 预览
            top_titles = [d.get("title", "?")[:30] for d in results[:3]]
            print(f"    {scheme_name:24s}  {elapsed:7.0f}ms  →  {top_titles}")

    # ── 汇总报告 ──────────────────────────────
    print(f"\n{'=' * 80}")
    print("评估结果汇总")

    print_agg(agg, total, "全量")

    for diff in ["easy", "medium", "hard"]:
        if diff in difficulty_agg:
            d_total = sum(1 for q in questions if q.get("difficulty") == diff)
            if d_total > 0:
                print_agg(difficulty_agg[diff], d_total, f"难度={diff}")

    # ── 结论 ───────────────────────────────────
    print(f"\n{'=' * 80}")
    print("推荐方案: ", end="")
    best_name = max(agg, key=lambda n: agg[n]["mrr"] + agg[n]["ndcg@5"])
    print(f"{best_name}  (MRR+NDCG 综合最优)")


def print_agg(agg: dict, total: int, label: str):
    print(f"\n  [{label} — {total} 题]")
    header = (
        f"  {'方案':<28s} {'Hit@1':>7s} {'Hit@3':>7s} "
        f"{'Hit@5':>7s} {'MRR':>7s} {'NDCG@5':>7s} {'延迟':>8s}"
    )
    print(header)
    print(f"  {'-' * 68}")
    for name in SCHEMES:
        m = {k: v / total for k, v in agg[name].items()}
        print(
            f"  {name:<28s} "
            f"{m['hit@1']:7.1%} {m['hit@3']:7.1%} {m['hit@5']:7.1%} "
            f"{m['mrr']:7.3f} {m['ndcg@5']:7.3f} "
            f"{m['latency_ms']:6.0f}ms"
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="检索消融实验")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 题")
    parser.add_argument("--skip-rerank", action="store_true", help="跳过 D 方案")
    args = parser.parse_args()
    main(limit=args.limit, skip_rerank=args.skip_rerank)
