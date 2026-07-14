import psycopg2
from sentence_transformers import SentenceTransformer
import json
from .generate_descriptions import build_description
import hashlib
from psycopg2.extras import Json
from pathlib import Path


def connect_product_db() -> psycopg2.extensions.connection:
    """连 pgvector，建表，返回连接"""
    # 跟 PG 服务器握手、验证身份、建立一条 TCP 连接
    # 返回一个 Connection 对象
    conn = psycopg2.connect(
        host="localhost",
        port=5433,
        user="postgres",
        password="postgres",
        dbname="postgres",
    )
    # cursor 是一个执行句柄。你通过它发 SQL、读结果。
    cur = conn.cursor()
    # cur.execute(sql) 把字符串里的 SQL 发到 PG 执行
    # 安装pgvector插件
    cur.execute("create extension if not exists vector")
    # 删除 laptop_products 表，方便重跑脚本
    cur.execute("drop table if exists laptop_products")
    # CREATE TABLE 建表
    # id 是 source_url 的 MD5 hash
    cur.execute("""
        create table laptop_products (
            id           VARCHAR(128) PRIMARY KEY,
            product_name VARCHAR(512),
            brand        VARCHAR(64),
            price        NUMERIC,
            product_type VARCHAR(32),
            description  TEXT,
            embedding    VECTOR(1024),
            metadata     JSONB
        )
    """)
    # 对 embedding 列建一个 HNSW 索引
    cur.execute("""
        create index on laptop_products
        using hnsw (embedding vector_cosine_ops)
    """)
    # 把 SQL 写入数据库，所有 execute 都在一个临时缓冲区，commit() 才真正落盘
    conn.commit()
    print("表建好了")
    cur.close()
    return conn


def load_model() -> SentenceTransformer:
    """加载向量化模型"""
    print("正在加载模型...")
    return SentenceTransformer("BAAI/bge-large-zh-v1.5")


def read_products(path: str) -> list[dict]:
    """读 JSONL，返回产品列表"""
    products = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                product = json.loads(line)
                products.append(product)
    return products


def ingest(
    conn: psycopg2.extensions.connection,
    model: SentenceTransformer,
    products: list[dict],
):
    """生成描述 → 编码 → 插入"""
    descriptions = []
    for p in products:
        p_desc = build_description(p)
        descriptions.append(p_desc)

    # 批量编码
    print("正在编码向量...")
    embeddings = model.encode(
        inputs=descriptions, normalize_embeddings=True, show_progress_bar=True
    )

    # 逐行插入
    cur = conn.cursor()
    sql = """
        insert into laptop_products
            (id, product_name, brand, price, product_type, description,
            embedding, metadata)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
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
                p.get("product_type"),
                descriptions[i],
                embeddings[i].tolist(),
                Json(p),
            ),
        )

    conn.commit()
    cur.close()
    print(f"插入完成，共 {len(products)} 条")


if __name__ == "__main__":
    conn = connect_product_db()
    model = load_model()

    root = Path(__file__).parent.parent
    file_path = root / "data" / "products" / "laptops.jsonl"
    products = read_products(file_path)
    ingest(conn, model, products)
    conn.close()
    print("数据注入完成")