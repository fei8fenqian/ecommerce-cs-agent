"""运行单标注者检索试点的文档级 Recall@5、MRR 和 nDCG@5。"""

import asyncio
import json
import math
import time
from pathlib import Path

from agent.rag.retrieve import vector_search
from infra.db_pool import close_pool, init_pool

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_PATH = ROOT / "data" / "test_questions.json"
LABELS_PATH = ROOT / "data" / "relevance_labels_pilot_v1.json"


def _ndcg_at_5(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
    relevances = [int(doc_id in relevant_ids) for doc_id in retrieved_ids[:5]]
    dcg = sum(relevance / math.log2(rank + 2) for rank, relevance in enumerate(relevances))
    ideal_count = min(5, len(relevant_ids))
    idcg = sum(1 / math.log2(rank + 2) for rank in range(ideal_count))
    return dcg / idcg if idcg else 0.0


async def main() -> None:
    questions = {question["id"]: question for question in json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))}
    dataset = json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    labels = dataset["labels"]

    await init_pool()
    try:
        recall_total = mrr_total = ndcg_total = 0.0
        latencies_ms: list[float] = []
        misses: list[int] = []
        for label in labels:
            question = questions[label["question_id"]]
            started = time.perf_counter()
            results = await vector_search(question["query"], table=question["expected_source"], top_k=5)
            latencies_ms.append((time.perf_counter() - started) * 1000)
            retrieved_ids = [result["id"] for result in results]
            relevant_ids = set(label["relevant_doc_ids"])
            ranks = [rank for rank, doc_id in enumerate(retrieved_ids, start=1) if doc_id in relevant_ids]
            if ranks:
                recall_total += 1
                mrr_total += 1 / min(ranks)
            else:
                misses.append(question["id"])
            ndcg_total += _ndcg_at_5(retrieved_ids, relevant_ids)

        count = len(labels)
        ordered_latencies = sorted(latencies_ms)
        p95_index = math.ceil(count * 0.95) - 1
        print(f"dataset={dataset['dataset_version']} annotation_scope={dataset['annotation_scope']}")
        print(f"evaluated={count} excluded={len(dataset['excluded'])}")
        print(f"Recall@5={recall_total / count:.4f}")
        print(f"MRR={mrr_total / count:.4f}")
        print(f"nDCG@5={ndcg_total / count:.4f}")
        print(f"latency_p50_ms={ordered_latencies[(count - 1) // 2]:.2f}")
        print(f"latency_p95_ms={ordered_latencies[p95_index]:.2f}")
        print(f"missed_question_ids={misses}")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
