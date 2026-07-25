import json
from pathlib import Path

from psycopg2.extensions import connection
from psycopg2.extras import Json
from sentence_transformers import SentenceTransformer

from core.db_pool import close_pool, get_connection, init_pool, put_connection

from ..generate_descriptions import build_component_description


def create_component_table(conn: connection):
    cur = conn.cursor()
    cur.execute("drop table if exists component_products")
    cur.execute("""create table component_products (
        id          varchar(128) primary key,
        name        varchar(512),
        category    varchar(64),    -- cpu / motherboard / vga / ...
        price       numeric,
        url         varchar(512),
        normalized  jsonb,          -- {socket: "Socket AM4", ...}
        params      jsonb,          -- {基本参数_适用类型: "台式机", ...}
        description text,
        embedding   vector(1024),
        metadata    jsonb
    );
    create index on component_products using hnsw (embedding vector_cosine_ops);
    """)
    conn.commit()
    print("表建好了")
    cur.close()


def load_model() -> SentenceTransformer:
    print("正在加载模型...")
    return SentenceTransformer("BAAI/bge-large-zh-v1.5")


def read_products(path) -> list[dict]:
    """读 JSONL，返回产品列表"""
    products = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                products.append(json.loads(line))
    return products


def ingest(products: list[dict], model: SentenceTransformer, conn: connection):
    # 批量编码
    descriptions = []
    for product in products:
        descriptions.append(build_component_description(product))
    embeddings = model.encode(
        inputs=descriptions, normalize_embeddings=True, show_progress_bar=True
    )

    # 逐行插入
    cur = conn.cursor()
    for idx, p in enumerate(products):
        cur.execute(
            """
            insert into component_products
            (id, name, category, price, url, normalized, params, description, embedding, metadata)
            values
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
            (
                p.get("id"),
                p.get("name"),
                p.get("category"),
                p.get("price"),
                p.get("url"),
                Json(p.get("normalized", {})),
                Json(p.get("params", {})),
                descriptions[idx],
                embeddings[idx].tolist(),
                Json(p),
            ),
        )
    conn.commit()
    cur.close()


if __name__ == "__main__":
    init_pool()
    conn = get_connection()
    try:
        create_component_table(conn)
        model = load_model()
        root = Path(__file__).parent.parent.parent
        read_path = root / "data" / "products" / "clean" / "components"
        all_products = []
        for path in read_path.glob("*.jsonl"):
            all_products.extend(read_products(path))
        ingest(all_products, model, conn)

    except Exception as e:
        print(f"注入失败 {str(e)}")
    finally:
        put_connection(conn)
        close_pool()
