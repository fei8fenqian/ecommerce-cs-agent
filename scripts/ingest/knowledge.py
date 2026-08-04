import hashlib
import re
from pathlib import Path

import psycopg2
from sentence_transformers import SentenceTransformer

from core.db_pool import close_pool, get_connection, init_pool, put_connection


def create_knowledge_table(conn: psycopg2.extensions.connection):
    cur = conn.cursor()

    cur.execute("""create table if not exists knowledge_chunks (
                id VARCHAR(128) PRIMARY KEY,
                source VARCHAR(128),
                title VARCHAR(256),
                content TEXT,
                embedding VECTOR(1024)
                )""")
    cur.execute(
        """create index if not exists idx_knowledge_embedding
           on knowledge_chunks using hnsw (embedding vector_cosine_ops)"""
    )

    conn.commit()
    cur.close()
    print("表建好了")


def load_model() -> SentenceTransformer:
    print("正在加载模型...")
    return SentenceTransformer("BAAI/bge-large-zh-v1.5")


def ingest(conn: psycopg2.extensions.connection, model: SentenceTransformer, chunks: list):
    cur = conn.cursor()

    # ---- 1. 读 DB 已有 id ----
    db_ids: set[str] = set()
    cur.execute("select id from knowledge_chunks")
    for row in cur.fetchall():
        db_ids.add(row[0])

    # ---- 2. 算新 chunks 的 id，建映射 ----
    new_ids: set[str] = set()
    chunk_map: dict[str, tuple] = {}  # {rid: (source, title, content)}
    for chunk in chunks:
        rid = hashlib.md5(f"{chunk[0]} {chunk[1]} {chunk[2]}".encode()).hexdigest()
        new_ids.add(rid)
        chunk_map[rid] = chunk

    insert_ids = new_ids - db_ids
    delete_ids = db_ids - new_ids
    skip_count = len(db_ids & new_ids)

    # ---- 3. 只编码新增的 ----
    if insert_ids:
        insert_chunks = [chunk_map[rid] for rid in insert_ids]
        inputs = [f"{s} {t}: {c}" for s, t, c in insert_chunks]
        print(f"新增 {len(insert_ids)} 条，正在编码向量...")
        embeddings = model.encode(inputs=inputs, normalize_embeddings=True, show_progress_bar=True)

        emb_map = dict(zip(insert_ids, embeddings))
        for rid in insert_ids:
            s, t, c = chunk_map[rid]
            cur.execute(
                "insert into knowledge_chunks (id, source, title, content, embedding) values (%s, %s, %s, %s, %s)",
                (rid, s, t, c, emb_map[rid].tolist()),
            )

    # ---- 4. 删除过期的 ----
    for rid in delete_ids:
        cur.execute("delete from knowledge_chunks where id = %s", (rid,))

    conn.commit()
    cur.close()
    print(f"新增 {len(insert_ids)} 条，删除 {len(delete_ids)} 条，跳过 {skip_count} 条（未变）")


MAX_CHARS = 400
OVERLAP = 50


def split_markdown(source: str, text: str) -> list[tuple[str, str, str]]:
    """把一篇 markdown 按 ## 标题切成小段，返回 [(来源文档, 标题, 正文), ...]"""

    # 在每个 "## " 开头的行前面切开文本
    # (?=...)   : 前瞻断言 → 匹配一个"位置"而非字符，分隔符不会被吃掉
    # ^         : 行首（配合 MULTILINE 则匹配每一行的行首）
    # re.MULTILINE : 让 ^ 匹配每一行的行首（默认只能匹配全文开头）
    sections = re.split(r"(?=^##)", text, flags=re.MULTILINE)

    chunks = []
    for sec in sections:
        if not sec.strip():
            continue

        # 把标题行和内容体分开
        lines = sec.strip().split("\n", 1)
        title = lines[0].lstrip("# ").strip()
        content = lines[1].strip() if len(lines) > 1 else ""

        if not content:
            continue

        chunks.extend(_split_long(source, title, content))
    return chunks


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


if __name__ == "__main__":
    init_pool()
    conn = get_connection()
    create_knowledge_table(conn)
    model = load_model()
    print("模型加载完成")

    all_chunks = []
    root = Path(__file__).parent.parent.parent
    knowledge_path = root / "data" / "knowledge"
    for file_path in knowledge_path.glob("*.md"):
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            source = f"{file_path.stem}.md"
            chunks = split_markdown(source, text)
            all_chunks.extend(chunks)

    ingest(conn, model, all_chunks)
    put_connection(conn)
    close_pool()
