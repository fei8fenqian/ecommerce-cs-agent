from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.evidence import resolve_evidence
from agent.rag import retrieve
from agent.rag.knowledge_context import format_knowledge_context
from agent.tools.search_knowledge import SearchKnowledge
from agent.tools.search_product import SearchProduct


def test_evidence_plan_keeps_live_facts_and_knowledge_separate() -> None:
    status = resolve_evidence(domain="refund", operation="status")
    eta = resolve_evidence(domain="refund", operation="expected_arrival")
    product = resolve_evidence(
        domain="product",
        operation="answer",
        target="rag",
        table="laptop_products",
    )

    assert status.live_facts is True
    assert status.needs_deep_knowledge is False
    assert eta.live_facts is True
    assert eta.needs_deep_knowledge is True
    assert product.catalog is True
    assert product.needs_deep_knowledge is False


def test_knowledge_context_marks_general_knowledge_as_a_source() -> None:
    context = format_knowledge_context(
        [{"source": "refund.md", "title": "退款说明", "content": "退款状态以订单页为准。"}]
    )

    assert "知识来源" in context
    assert "退款说明" in context


@pytest.mark.asyncio
async def test_hybrid_search_keeps_bm25_only_runtime_knowledge_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    vector_doc = {"id": "vector-1", "title": "向量命中", "content": "普通内容", "source": "runtime.md"}
    bm25 = MagicMock()
    bm25.search.return_value = [("bm25-only", 9.0), ("vector-1", 2.0)]
    fetch = AsyncMock(
        return_value=[
            {
                "id": "bm25-only",
                "title": "退款状态码",
                "content": "PROCESSING 表示处理中。",
                "source": "runtime.md",
            }
        ]
    )
    monkeypatch.setattr(retrieve, "load_runtime_knowledge_sources", lambda: frozenset({"runtime.md"}))
    monkeypatch.setattr(retrieve, "_vector_search", AsyncMock(return_value=[vector_doc]))
    monkeypatch.setattr(retrieve, "_get_bm25", AsyncMock(return_value=bm25))
    monkeypatch.setattr(retrieve, "_fetch_documents_by_ids", fetch)

    results = await retrieve.hybrid_search(
        "PROCESSING 是什么",
        table="knowledge_chunks",
        top_k=3,
        use_rerank=False,
    )

    assert {doc["id"] for doc in results} == {"vector-1", "bm25-only"}
    fetch.assert_awaited_once_with(
        "knowledge_chunks",
        ["bm25-only"],
        runtime_sources=frozenset({"runtime.md"}),
        where=None,
    )


@pytest.mark.asyncio
async def test_pre_rag_is_vector_only_and_filters_low_similarity(monkeypatch: pytest.MonkeyPatch) -> None:
    vector_search = AsyncMock(
        return_value=[
            {"id": "high", "score": 0.81},
            {"id": "low", "score": 0.42},
        ]
    )
    hybrid_search = AsyncMock(side_effect=AssertionError("Pre-RAG 不应调用 BM25 Hybrid"))
    monkeypatch.setattr(retrieve, "vector_search", vector_search)
    monkeypatch.setattr(retrieve, "hybrid_search", hybrid_search)

    results = await retrieve.pre_retrieve_knowledge(
        "退款规则",
        top_k=3,
        similarity_threshold=0.55,
    )

    assert [doc["id"] for doc in results] == ["high"]
    vector_search.assert_awaited_once_with("退款规则", table="knowledge_chunks", top_k=3)
    hybrid_search.assert_not_awaited()


@pytest.mark.asyncio
async def test_pre_rag_returns_empty_when_all_candidates_are_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        retrieve,
        "vector_search",
        AsyncMock(return_value=[{"id": "low-1", "score": 0.54}, {"id": "low-2", "score": 0.2}]),
    )

    results = await retrieve.pre_retrieve_knowledge("无关问题", top_k=3, similarity_threshold=0.55)

    assert results == []


@pytest.mark.asyncio
async def test_pre_rag_caps_router_context_to_three_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    vector_search = AsyncMock(return_value=[{"id": str(index), "score": 0.9} for index in range(5)])
    monkeypatch.setattr(retrieve, "vector_search", vector_search)

    results = await retrieve.pre_retrieve_knowledge("退款", top_k=10, similarity_threshold=0.55)

    assert len(results) == 3
    vector_search.assert_awaited_once_with("退款", table="knowledge_chunks", top_k=3)


@pytest.mark.asyncio
async def test_search_knowledge_is_manifest_backed_read_only_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    search = AsyncMock(
        return_value=[{"title": "退款规则", "source": "refund.md", "content": "仅说明一般规则", "score": 0.9}]
    )
    monkeypatch.setattr("agent.tools.search_knowledge.hybrid_search", search)

    result = await SearchKnowledge().execute("退款规则", top_k=99)

    assert result.is_success
    assert result.data["results"][0]["source"] == "refund.md"
    assert search.await_args.kwargs["table"] == "knowledge_chunks"
    assert search.await_args.kwargs["top_k"] == 10


@pytest.mark.asyncio
async def test_search_product_cannot_be_used_as_knowledge_backdoor() -> None:
    result = await SearchProduct().execute("退款规则", table="knowledge_chunks")

    assert result.is_success is False
    assert "仅支持商品目录" in result.error
