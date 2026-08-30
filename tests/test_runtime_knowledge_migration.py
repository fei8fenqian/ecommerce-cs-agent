"""Source-First 知识运行时迁移的安全边界测试。"""

import importlib.util
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from agent.rag import retrieve
from agent.rag.runtime_manifest import load_runtime_knowledge_sources, runtime_markdown_paths


def _load_ingest_module() -> ModuleType:
    path = Path("scripts/ingest/knowledge.py")
    spec = importlib.util.spec_from_file_location("knowledge_ingest_for_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SyncCursor:
    def __init__(self, conn: "_SyncConnection"):
        self.conn = conn
        self.rows: list[tuple[str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.conn.executed.append((sql, params))
        if sql.lower().startswith("select id"):
            self.rows = [(chunk_id, *self.conn.records[chunk_id]) for chunk_id in self.conn.ids]
        elif sql.lower().startswith("insert into knowledge_chunks"):
            assert params is not None
            self.conn.records[params[0]] = (params[1], params[2], params[3])
        elif sql.lower().startswith("delete from knowledge_chunks"):
            assert params is not None
            self.conn.records.pop(params[0], None)
        elif sql.lower().startswith("update knowledge_chunks set id"):
            assert params is not None
            old_id, new_id = params[1], params[0]
            self.conn.records[new_id] = self.conn.records.pop(old_id)
        elif sql.lower().startswith("update knowledge_chunks set source"):
            assert params is not None
            self.conn.records[params[4]] = (params[0], params[1], params[2])

    def fetchall(self) -> list[tuple[str]]:
        return self.rows


class _SyncConnection:
    def __init__(self, ids: Iterable[str], records: dict[str, tuple[str, str, str]] | None = None):
        self.records = records or {chunk_id: ("legacy.md", "旧知识", "旧内容") for chunk_id in ids}
        self.executed: list[tuple[str, tuple | None]] = []
        self.commits = 0

    @property
    def ids(self) -> set[str]:
        return set(self.records)

    def cursor(self) -> _SyncCursor:
        return _SyncCursor(self)

    def commit(self) -> None:
        self.commits += 1


class _Vector:
    def tolist(self) -> list[float]:
        return [0.0] * 1024


class _EmbeddingModel:
    def __init__(self):
        self.calls: list[list[str]] = []

    def encode(self, *, inputs: list[str], **_kwargs) -> list[_Vector]:
        self.calls.append(inputs)
        return [_Vector() for _ in inputs]


class _AsyncRows:
    def __init__(self, rows: list[tuple]):
        self.rows = iter(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.rows)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _AsyncKnowledgeConnection:
    def __init__(self):
        self.executed: list[tuple[str, object]] = []
        self.rows = {
            "legacy.md": [("legacy-1", "legacy.md", "旧退款规则", "退款到账只要一天")],
            "refund_request.md": [("runtime-1", "refund_request.md", "退款申请", "退款需要先查订单资格")],
        }

    async def set_autocommit(self, _enabled: bool) -> None:
        return None

    async def execute(self, sql: str, params=()) -> _AsyncRows:
        self.executed.append((sql, params))
        if "source = any(%s)" not in sql:
            raise AssertionError("knowledge_chunks 查询必须按 runtime manifest 过滤")
        sources = set(params[1] if "embedding" in sql else params[0])
        if "select id,content" in sql:
            return _AsyncRows([(row[0], row[3]) for source in sources for row in self.rows[source]])
        return _AsyncRows([row + (0.99,) for source in sources for row in self.rows[source]])


def _chunk_id(chunk: tuple[str, str, str]) -> str:
    ingest_module = _load_ingest_module()
    return ingest_module.KnowledgeChunk(
        source=chunk[0],
        title=chunk[1],
        content=chunk[2],
        section_identity=chunk[1],
        chunk_index=0,
    ).id


def test_safe_ingest_keeps_legacy_chunks() -> None:
    ingest_module = _load_ingest_module()
    chunk = ("refund_request.md", "退款申请", "先核验订单资格")
    conn = _SyncConnection(ids={"legacy-chunk"})

    ingest_module.ingest(conn, _EmbeddingModel(), [chunk])

    assert "legacy-chunk" in conn.ids
    assert _chunk_id(chunk) in conn.ids
    assert not any(sql.lower().startswith("delete") for sql, _ in conn.executed)


def test_first_stable_sync_renames_matching_content_hash_without_reembedding() -> None:
    ingest_module = _load_ingest_module()
    chunk = ("refund_request.md", "退款申请", "先核验订单资格")
    stable_id = _chunk_id(chunk)
    conn = _SyncConnection(
        ids={"old-content-hash"},
        records={"old-content-hash": (chunk[0], chunk[1], chunk[2])},
    )
    model = _EmbeddingModel()

    ingest_module.ingest(conn, model, [chunk])

    assert conn.ids == {stable_id}
    assert model.calls == []
    assert any(sql.lower().startswith("update knowledge_chunks set id") for sql, _ in conn.executed)


def test_prune_explicitly_removes_legacy_chunks() -> None:
    ingest_module = _load_ingest_module()
    chunk = ("refund_request.md", "退款申请", "先核验订单资格")
    current_id = _chunk_id(chunk)
    conn = _SyncConnection(
        ids={"legacy-chunk", current_id},
        records={
            "legacy-chunk": ("legacy.md", "旧知识", "旧内容"),
            current_id: (chunk[0], chunk[1], chunk[2]),
        },
    )

    ingest_module.ingest(conn, _EmbeddingModel(), [chunk], prune=True)

    assert "legacy-chunk" not in conn.ids
    assert current_id in conn.ids
    assert any(sql.lower().startswith("delete") for sql, _ in conn.executed)


def test_changed_content_updates_the_same_stable_id() -> None:
    ingest_module = _load_ingest_module()
    old = ingest_module.KnowledgeChunk("refund_status.md", "退款状态", "旧正文", "退款状态", 0)
    changed = ingest_module.KnowledgeChunk("refund_status.md", "退款状态", "新正文", "退款状态", 0)
    assert old.id == changed.id
    conn = _SyncConnection(records={old.id: (old.source, old.title, old.content)}, ids={old.id})
    model = _EmbeddingModel()

    ingest_module.ingest(conn, model, [changed])

    assert conn.records[old.id] == (changed.source, changed.title, changed.content)
    assert len(model.calls) == 1
    assert any(sql.lower().startswith("update knowledge_chunks set source") for sql, _ in conn.executed)


def test_unchanged_chunk_skips_update_and_embedding() -> None:
    ingest_module = _load_ingest_module()
    current = ingest_module.KnowledgeChunk("refund_status.md", "退款状态", "当前正文", "退款状态", 0)
    conn = _SyncConnection(
        ids={current.id},
        records={current.id: (current.source, current.title, current.content)},
    )
    model = _EmbeddingModel()

    ingest_module.ingest(conn, model, [current])

    assert model.calls == []
    assert not any(sql.lower().startswith("update") for sql, _ in conn.executed)


def test_new_section_inserts_only_the_new_chunk() -> None:
    ingest_module = _load_ingest_module()
    first = ingest_module.KnowledgeChunk("refund_status.md", "当前状态", "正文一", "当前状态", 0)
    second = ingest_module.KnowledgeChunk("refund_status.md", "失败处理", "正文二", "失败处理", 0)
    conn = _SyncConnection(records={first.id: (first.source, first.title, first.content)}, ids={first.id})
    model = _EmbeddingModel()

    ingest_module.ingest(conn, model, [first, second])

    assert conn.ids == {first.id, second.id}
    assert len(model.calls) == 1
    assert model.calls[0] == [f"{second.source} {second.title}: {second.content}"]


def test_deleted_section_removes_only_stale_chunks_of_current_source() -> None:
    ingest_module = _load_ingest_module()
    current = ingest_module.KnowledgeChunk("refund_status.md", "当前状态", "正文一", "当前状态", 0)
    stale = ingest_module.KnowledgeChunk("refund_status.md", "旧 section", "旧正文", "旧 section", 0)
    other = ingest_module.KnowledgeChunk("payment_status.md", "支付状态", "其他正文", "支付状态", 0)
    conn = _SyncConnection(
        ids={current.id, stale.id, other.id},
        records={
            current.id: (current.source, current.title, current.content),
            stale.id: (stale.source, stale.title, stale.content),
            other.id: (other.source, other.title, other.content),
        },
    )

    ingest_module.ingest(conn, _EmbeddingModel(), [current])

    assert conn.ids == {current.id, other.id}
    deletes = [params for sql, params in conn.executed if sql.lower().startswith("delete")]
    assert deletes == [(stale.id,)]


def test_manifest_source_without_sections_still_syncs_and_cleans_source() -> None:
    ingest_module = _load_ingest_module()
    stale = ingest_module.KnowledgeChunk("empty.md", "旧 section", "旧正文", "旧 section", 0)
    other = ingest_module.KnowledgeChunk("payment_status.md", "支付状态", "其他正文", "支付状态", 0)
    conn = _SyncConnection(
        ids={stale.id, other.id},
        records={
            stale.id: (stale.source, stale.title, stale.content),
            other.id: (other.source, other.title, other.content),
        },
    )

    ingest_module.ingest(
        conn,
        _EmbeddingModel(),
        [],
        runtime_sources=["empty.md"],
    )

    assert conn.ids == {other.id}
    assert (stale.id,) in [params for sql, params in conn.executed if sql.lower().startswith("delete")]


def test_manifest_loader_rejects_missing_empty_missing_file_and_nested_paths(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="缺少运行时知识清单"):
        load_runtime_knowledge_sources(tmp_path)

    manifest = tmp_path / "runtime_manifest.txt"
    manifest.write_text("# no sources\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不能为空"):
        load_runtime_knowledge_sources(tmp_path)

    manifest.write_text("missing.md\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="不存在"):
        load_runtime_knowledge_sources(tmp_path)

    manifest.write_text("nested/refund_request.md\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="根目录"):
        load_runtime_knowledge_sources(tmp_path)


@pytest.mark.asyncio
async def test_vector_retrieval_excludes_legacy_source(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _AsyncKnowledgeConnection()
    monkeypatch.setattr(retrieve, "load_runtime_knowledge_sources", lambda: frozenset({"refund_request.md"}))
    monkeypatch.setattr(retrieve, "_get_model", lambda: _EmbeddingModel())
    monkeypatch.setattr(retrieve, "get_connection", AsyncMock(return_value=conn))
    monkeypatch.setattr(retrieve, "put_connection", AsyncMock())

    results = await retrieve.vector_search("退款多久到账", table="knowledge_chunks", top_k=5)

    assert [result["source"] for result in results] == ["refund_request.md"]
    sql, params = conn.executed[0]
    assert "source = any(%s)" in sql
    assert params[1] == ["refund_request.md"]


@pytest.mark.asyncio
async def test_bm25_retrieval_excludes_legacy_source(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _AsyncKnowledgeConnection()
    retrieve._bm25_cache.clear()
    monkeypatch.setattr(retrieve, "load_runtime_knowledge_sources", lambda: frozenset({"refund_request.md"}))
    monkeypatch.setattr(retrieve, "get_connection", AsyncMock(return_value=conn))
    monkeypatch.setattr(retrieve, "put_connection", AsyncMock())

    index = await retrieve._get_bm25("knowledge_chunks")

    assert [doc["id"] for doc in index.docs] == ["runtime-1"]
    sql, params = conn.executed[0]
    assert "source = any(%s)" in sql
    assert params == (["refund_request.md"],)


@pytest.mark.asyncio
async def test_invalid_manifest_fails_closed_before_database_query(monkeypatch: pytest.MonkeyPatch) -> None:
    get_connection = AsyncMock()
    monkeypatch.setattr(
        retrieve,
        "load_runtime_knowledge_sources",
        lambda: (_ for _ in ()).throw(RuntimeError("invalid")),
    )
    monkeypatch.setattr(retrieve, "get_connection", get_connection)

    with pytest.raises(RuntimeError, match="invalid"):
        await retrieve.vector_search("退款", table="knowledge_chunks")

    get_connection.assert_not_awaited()


def test_runtime_markdown_paths_follow_the_manifest(tmp_path: Path) -> None:
    (tmp_path / "refund_request.md").write_text("# 退款", encoding="utf-8")
    (tmp_path / "runtime_manifest.txt").write_text("refund_request.md\n", encoding="utf-8")

    assert runtime_markdown_paths(tmp_path) == [tmp_path / "refund_request.md"]
