"""把经来源审计的运行时 Markdown 文档增量导入 pgvector。

这是一次性 CLI，不复用应用的异步连接池：这样数据库未启动时可以立即失败，
也不会把同步的旧脚本误当成异步连接使用。
"""

import argparse
import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection, OperationalError, connect
from sentence_transformers import SentenceTransformer

from agent.rag.runtime_manifest import runtime_markdown_paths
from config import settings
from infra.db_pool import get_dsn
from infra.model_device import resolve_model_device


def create_knowledge_table(conn: Connection) -> None:
    """幂等创建知识库表和向量索引。"""

    with conn.cursor() as cur:
        cur.execute(
            """create table if not exists knowledge_chunks (
                id VARCHAR(128) PRIMARY KEY,
                source VARCHAR(128),
                title VARCHAR(256),
                content TEXT,
                embedding VECTOR(1024)
            )"""
        )
        cur.execute(
            """create index if not exists idx_knowledge_embedding
               on knowledge_chunks using hnsw (embedding vector_cosine_ops)"""
        )

    conn.commit()
    print("表建好了")


def load_model() -> SentenceTransformer:
    print("正在加载模型...")
    return SentenceTransformer(
        settings.embedding_model,
        device=resolve_model_device(settings.rag_device),
    )


@dataclass(frozen=True)
class KnowledgeChunk:
    """一个可稳定寻址的运行时知识 chunk。"""

    source: str
    title: str
    content: str
    section_identity: str
    chunk_index: int

    @property
    def id(self) -> str:
        material = f"runtime-v1\0{self.source}\0{self.section_identity}\0{self.chunk_index}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _coerce_chunks(chunks: Iterable[KnowledgeChunk | tuple[str, str, str]]) -> list[KnowledgeChunk]:
    """兼容旧的三元组调用方，同时为其生成稳定 section/chunk 身份。"""

    result: list[KnowledgeChunk] = []
    counters: defaultdict[tuple[str, str], int] = defaultdict(int)
    for chunk in chunks:
        if isinstance(chunk, KnowledgeChunk):
            result.append(chunk)
            continue
        source, title, content = chunk
        key = (source, title)
        index = counters[key]
        counters[key] += 1
        result.append(
            KnowledgeChunk(
                source=source,
                title=title,
                content=content,
                section_identity=title,
                chunk_index=index,
            )
        )
    return result


def ingest(
    conn: Connection,
    model: SentenceTransformer,
    chunks: Iterable[KnowledgeChunk | tuple[str, str, str]],
    *,
    prune: bool = False,
    runtime_sources: Iterable[str] | None = None,
) -> None:
    normalized_chunks = _coerce_chunks(chunks)
    # ``runtime_sources`` 来自 manifest，而不是只从 chunks 推断；这样即使某个
    # manifest 文档暂时没有有效 section，也能删除该 source 的过期索引。
    active_sources = (
        set(runtime_sources) if runtime_sources is not None else {chunk.source for chunk in normalized_chunks}
    )
    chunk_sources = {chunk.source for chunk in normalized_chunks}
    if not chunk_sources.issubset(active_sources):
        unexpected = sorted(chunk_sources - active_sources)
        raise ValueError(f"chunk source 不在 runtime manifest 中: {unexpected}")

    # ---- 1. 读 DB 已有记录（legacy source 不参与默认清理） ----
    with conn.cursor() as cur:
        cur.execute("select id, source, title, content from knowledge_chunks")
        db_rows = [tuple(row) for row in cur.fetchall()]

    db_by_id = {str(row[0]): row for row in db_rows}
    rows_by_source: defaultdict[str, list[tuple]] = defaultdict(list)
    exact_rows: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
    for row in db_rows:
        row_id, source, title, content = str(row[0]), str(row[1]), str(row[2]), str(row[3])
        rows_by_source[source].append(row)
        exact_rows[(source, title, content)].append(row_id)

    retained_ids: set[str] = set()
    insert_chunks: list[KnowledgeChunk] = []
    update_chunks: list[KnowledgeChunk] = []
    rename_pairs: list[tuple[str, str]] = []
    used_legacy_ids: set[str] = set()
    skip_count = 0

    # ---- 2. 按稳定身份同步：同身份 UPDATE，不同身份 INSERT；同 source 的旧记录稍后清理 ----
    for chunk in normalized_chunks:
        stable_id = chunk.id
        existing = db_by_id.get(stable_id)
        if existing is not None:
            retained_ids.add(stable_id)
            if (
                str(existing[1]) == chunk.source
                and str(existing[2]) == chunk.title
                and str(existing[3]) == chunk.content
            ):
                skip_count += 1
            else:
                update_chunks.append(chunk)
            continue

        # 首次切换到 stable ID 时，复用完全相同的旧 content-hash 记录，避免重复 embedding。
        legacy_candidates = exact_rows.get((chunk.source, chunk.title, chunk.content), [])
        legacy_id = next((row_id for row_id in legacy_candidates if row_id not in used_legacy_ids), None)
        if legacy_id is not None:
            retained_ids.add(stable_id)
            used_legacy_ids.add(legacy_id)
            rename_pairs.append((legacy_id, stable_id))
        else:
            insert_chunks.append(chunk)
            retained_ids.add(stable_id)

    # 当前 manifest source 的 stale chunk（包括已删除 section 和旧内容版本）默认就应下线；
    # 其他 source 的 legacy 数据完全不动。--prune 另外清理整个 manifest 之外的 source。
    renamed_legacy_ids = {old_id for old_id, _ in rename_pairs}
    stale_ids = [
        str(row[0])
        for source in active_sources
        for row in rows_by_source.get(source, [])
        if str(row[0]) not in renamed_legacy_ids and str(row[0]) not in retained_ids
    ]
    if prune:
        stale_ids.extend(
            str(row[0]) for row in db_rows if str(row[1]) not in active_sources and str(row[0]) not in stale_ids
        )

    changed_chunks = [*insert_chunks, *update_chunks]
    if changed_chunks:
        inputs = [f"{chunk.source} {chunk.title}: {chunk.content}" for chunk in changed_chunks]
        print(f"新增/修改 {len(changed_chunks)} 条，正在编码向量...")
        embeddings = model.encode(inputs=inputs, normalize_embeddings=True, show_progress_bar=True)
        emb_map = dict(zip(changed_chunks, embeddings))
    else:
        emb_map = {}

    with conn.cursor() as cur:
        for old_id, stable_id in rename_pairs:
            cur.execute("update knowledge_chunks set id = %s where id = %s", (stable_id, old_id))
        for chunk in insert_chunks:
            vector = str(emb_map[chunk].tolist())
            cur.execute(
                "insert into knowledge_chunks (id, source, title, content, embedding) "
                "values (%s, %s, %s, %s, %s::vector)",
                (chunk.id, chunk.source, chunk.title, chunk.content, vector),
            )
        for chunk in update_chunks:
            vector = str(emb_map[chunk].tolist())
            cur.execute(
                "update knowledge_chunks set source = %s, title = %s, content = %s, embedding = %s::vector "
                "where id = %s",
                (chunk.source, chunk.title, chunk.content, vector, chunk.id),
            )
        for row_id in stale_ids:
            cur.execute("delete from knowledge_chunks where id = %s", (row_id,))

    conn.commit()
    untouched_legacy_count = sum(
        1 for row in db_rows if str(row[1]) not in active_sources and str(row[0]) not in stale_ids
    )
    print(
        f"新增 {len(insert_chunks)} 条，修改 {len(update_chunks)} 条，重命名 {len(rename_pairs)} 条，"
        f"删除 stale {len(stale_ids)} 条，跳过 {skip_count} 条（未变），"
        f"保留其他 source {untouched_legacy_count} 条"
    )


MAX_CHARS = 400
OVERLAP = 50


def split_markdown_with_identity(source: str, text: str) -> list[KnowledgeChunk]:
    """按 section 解析稳定 chunk；正文变化不会改变同一 section 的身份。"""

    sections = re.split(r"(?=^##)", text, flags=re.MULTILINE)
    occurrences: defaultdict[str, int] = defaultdict(int)
    chunks: list[KnowledgeChunk] = []
    for sec in sections:
        if not sec.strip():
            continue
        lines = sec.strip().split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        content = lines[1].strip() if len(lines) > 1 else ""
        if not content:
            continue
        occurrence = occurrences[title]
        occurrences[title] += 1
        section_identity = title if occurrence == 0 else f"{title}#{occurrence + 1}"
        parts = _split_long(source, title, content)
        chunks.extend(
            KnowledgeChunk(
                source=source,
                title=part_title,
                content=part_content,
                section_identity=section_identity,
                chunk_index=index,
            )
            for index, (_part_source, part_title, part_content) in enumerate(parts)
        )
    return chunks


def split_markdown(source: str, text: str) -> list[tuple[str, str, str]]:
    """兼容旧调用方的 Markdown 解析接口。"""

    return [(chunk.source, chunk.title, chunk.content) for chunk in split_markdown_with_identity(source, text)]


def _split_long(source, title, content):
    """段落超过 MAX_CHARS 时按句号拆，每个子段带上标题"""
    if len(content) <= MAX_CHARS:
        return [(source, title, content)]

    # 按句子分割
    sentences = content.split("。")
    res = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        s = s + "。"

        if len(buf) + len(s) <= MAX_CHARS:
            buf += s
        else:
            if buf:
                res.append((source, title, buf))
            # 如果单句就超限，硬截断
            # 找自然断点 + overlap
            while len(s) > MAX_CHARS:
                cut_at = MAX_CHARS
                for sep in ["。", "，", "；", "：", "、"]:
                    pos = s.rfind(sep, MAX_CHARS - OVERLAP, MAX_CHARS)
                    if pos > 0:
                        cut_at = pos + 1
                        break
                res.append((source, title, s[:cut_at]))
                s = s[max(0, cut_at - OVERLAP) :]
            buf = s
    if buf:
        res.append((source, title, buf))
    return res


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导入经 Source Audit 的运行时知识")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="物理删除不在当前 runtime manifest 生成集合内的旧知识 chunk",
    )
    args = parser.parse_args(argv)
    mode = "PRUNE" if args.prune else "SAFE UPSERT"
    print(f"Knowledge ingest mode: {mode}")

    all_chunks: list[KnowledgeChunk] = []
    root = Path(__file__).parent.parent.parent
    knowledge_path = root / "data" / "knowledge"
    manifest_paths = runtime_markdown_paths(knowledge_path)
    runtime_sources = [file_path.name for file_path in manifest_paths]
    for file_path in manifest_paths:
        text = file_path.read_text(encoding="utf-8")
        source = file_path.name
        all_chunks.extend(split_markdown_with_identity(source, text))

    print(f"读取 {len(all_chunks)} 个知识片段")
    # 这是一次性 CLI，不占用应用连接池；同步 psycopg 连接在数据库不可达时能快速失败。
    try:
        with connect(f"{get_dsn()} connect_timeout=15") as conn:
            create_knowledge_table(conn)
            model = load_model()
            print("模型加载完成")
            ingest(conn, model, all_chunks, prune=args.prune, runtime_sources=runtime_sources)
    except OperationalError as exc:
        raise RuntimeError(
            "无法连接 PostgreSQL。请先启动当前项目的 pgvector 容器，并确认 "
            "PG_HOST/PG_PORT/PG_DBNAME/PG_PASSWORD 与 .env 一致。"
        ) from exc


if __name__ == "__main__":
    main()
