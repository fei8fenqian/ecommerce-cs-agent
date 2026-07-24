"""手机产品数据入库：建表 → 生成描述 → 编码向量 → 插入 pgvector"""

import hashlib
import json
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from sentence_transformers import SentenceTransformer

from ..db import connect_db
from ..generate_descriptions import build_phone_description


def create_phone_table(conn: psycopg2.extensions.connection):
    """建 phone_products 表 + HNSW 索引"""
    cur = conn.cursor()
    cur.execute("drop table if exists phone_products")
    cur.execute("""
        create table phone_products (
            id           varchar(128) primary key,
            product_name varchar(512),
            brand        varchar(64),
            price        numeric,
            description  text,
            embedding    vector(1024),
            metadata     jsonb
        )
    """)
    cur.execute("""
        create index on phone_products
        using hnsw (embedding vector_cosine_ops)
    """)
    conn.commit()
    cur.close()
    print("phone_products 表建好了")


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


def ingest(conn, model, products):
    """生成描述 → 编码 → 插入"""
    descriptions = [build_phone_description(p) for p in products]

    print("正在编码向量...")
    embeddings = model.encode(descriptions, normalize_embeddings=True, show_progress_bar=True)

    cur = conn.cursor()
    sql = """
        insert into phone_products
            (id, product_name, brand, price, description, embedding, metadata)
        values (%s, %s, %s, %s, %s, %s, %s)
    """
    for i, p in enumerate(products):
        rid = hashlib.md5(p["source_url"].encode()).hexdigest()
        cur.execute(
            sql,
            (
                rid,
                p.get("product_name"),
                p.get("brand"),
                p.get("price"),
                descriptions[i],
                embeddings[i].tolist(),
                Json(p),
            ),
        )

    conn.commit()
    cur.close()
    print(f"插入完成，共 {len(products)} 条")


if __name__ == "__main__":
    conn = connect_db()
    create_phone_table(conn)
    model = load_model()

    root = Path(__file__).parent.parent.parent
    file_path = root / "data" / "products" / "phones.jsonl"
    products = read_products(file_path)
    ingest(conn, model, products)
    conn.close()
    print("手机数据注入完成")
