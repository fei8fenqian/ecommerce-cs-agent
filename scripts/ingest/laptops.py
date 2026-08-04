"""笔记本产品数据入库：建表 → 生成描述 → 增量编码 → 插入 pgvector"""

import hashlib
import json
from pathlib import Path

from psycopg2.extensions import connection
from psycopg2.extras import Json
from sentence_transformers import SentenceTransformer

from core.db_pool import close_pool, get_connection, init_pool, put_connection

from ..generate_descriptions import build_laptop_description


def create_product_table(conn: connection):
    """建表 + HNSW 索引（幂等）"""
    cur = conn.cursor()
    cur.execute("""
        create table if not exists laptop_products (
            id           varchar(128) primary key,
            product_name varchar(512),
            brand        varchar(64),
            price        numeric,
            product_type varchar(32),
            description  text,
            embedding    vector(1024),
            metadata     jsonb,
            content_hash varchar(32)
        )
    """)
    # 兼容旧表：没 content_hash 列时补上
    cur.execute("""
        alter table laptop_products
        add column if not exists content_hash varchar(32)
    """)
    cur.execute("""
        create index if not exists idx_laptop_embedding
        on laptop_products using hnsw (embedding vector_cosine_ops)
    """)
    conn.commit()
    cur.close()
    print("laptop_products 表就绪")


def load_model() -> SentenceTransformer:
    print("正在加载模型...")
    return SentenceTransformer("BAAI/bge-large-zh-v1.5")


def read_products(path: str | Path) -> list[dict]:
    products = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                products.append(json.loads(line))
    return products


def ingest(conn: connection, model: SentenceTransformer, products: list[dict]):
    cur = conn.cursor()

    # ---- 1. 读 DB 已有 id + hash ----
    db_hashes: dict[str, str] = {}
    cur.execute("select id, content_hash from laptop_products")
    for row in cur.fetchall():
        db_hashes[row[0]] = row[1] or ""

    # ---- 2. 算新数据的 id + hash ----
    new_hashes: dict[str, str] = {}
    desc_map: dict[str, str] = {}  # {rid: description}
    product_map: dict[str, dict] = {}  # {rid: product_dict}
    for p in products:
        rid = hashlib.md5(p["source_url"].encode()).hexdigest()
        desc = build_laptop_description(p)
        desc_map[rid] = desc
        new_hashes[rid] = hashlib.md5(desc.encode()).hexdigest()
        product_map[rid] = p

    new_ids = set(new_hashes)
    db_ids = set(db_hashes)

    insert_ids = new_ids - db_ids
    delete_ids = db_ids - new_ids
    # 同 id 但 hash 变了 → 需要重新 embedding
    update_ids = {rid for rid in new_ids & db_ids if new_hashes[rid] != db_hashes[rid]}
    skip_count = len(db_ids & new_ids) - len(update_ids)

    # ---- 3. 只编码新增 + 变更的 ----
    changed_ids = insert_ids | update_ids
    if changed_ids:
        changed_descs = [desc_map[rid] for rid in changed_ids]
        print(f"新增 {len(insert_ids)} + 更新 {len(update_ids)} 条，正在编码向量...")
        embeddings = model.encode(inputs=changed_descs, normalize_embeddings=True, show_progress_bar=True)
        emb_map = dict(zip(changed_ids, embeddings))

        for rid in changed_ids:
            p = product_map[rid]
            cur.execute(
                "insert into laptop_products "
                "(id, product_name, brand, price, product_type, description, embedding, metadata, content_hash) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (id) do update set "
                "product_name=excluded.product_name, brand=excluded.brand, price=excluded.price, "
                "product_type=excluded.product_type, description=excluded.description, "
                "embedding=excluded.embedding, metadata=excluded.metadata, content_hash=excluded.content_hash",
                (
                    rid,
                    p.get("product_name"),
                    p.get("brand"),
                    p.get("price"),
                    p.get("product_type"),
                    desc_map[rid],
                    emb_map[rid].tolist(),
                    Json(p),
                    new_hashes[rid],
                ),
            )

    # ---- 4. 删除下架的 ----
    for rid in delete_ids:
        cur.execute("delete from laptop_products where id = %s", (rid,))

    conn.commit()
    cur.close()
    print(
        f"新增 {len(insert_ids)} 条，更新 {len(update_ids)} 条，删除 {len(delete_ids)} 条，跳过 {skip_count} 条（未变）"
    )


if __name__ == "__main__":
    init_pool()
    conn = get_connection()
    try:
        create_product_table(conn)
        model = load_model()
        root = Path(__file__).parent.parent.parent
        file_path = root / "data" / "products" / "laptops.jsonl"
        products = read_products(file_path)
        ingest(conn, model, products)
    finally:
        put_connection(conn)
        close_pool()
    print("笔记本数据注入完成")
