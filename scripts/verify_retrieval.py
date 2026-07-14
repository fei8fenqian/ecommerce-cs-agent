import psycopg2
from sentence_transformers import SentenceTransformer

conn = psycopg2.connect(
    host="localhost", port=5433, dbname="postgres", user="postgres", password="postgres"
)

cur = conn.cursor()

model = SentenceTransformer("BAAI/bge-large-zh-v1.5")

queries = [
    {
        "text": "适合经常出差的轻薄本",
        "where": "product_type = '轻薄笔记本'",
        "limit": 3,
    },
    {
        "text": "8000 以内的游戏本",
        "where": "price <= 8000 AND product_type = '游戏本'",
        "limit": 3,
    },
    {
        "text": "华为 MateBook 32G 内存",
        "where": "brand = '华为'",
        "limit": 3,
    },
]

for q in queries:
    print(f"查询: {q["text"]}")
    print("-" * 50)

    # encode 的输入必须是 list
    q_vec = model.encode([q["text"]], normalize_embeddings=True)[0].tolist()
    q_vec_str = str(q_vec)

    # psycopg2 的 execute() 第二个参数必须是一个tuple
    cur.execute(
        f"""
                select product_name, brand, price, description
                from laptop_products
                where {q["where"]}
                order by embedding <=> %s::vector
                limit {q["limit"]}
                """,
        (q_vec_str,),
    )

    # cur.fetchall() — 获取查询结果的所有行，返回 list of tuples
    for i, row in enumerate(cur.fetchall(), 1):
        product_name, brand, price, description = row
        print(f"{i}. {brand} {product_name}  ¥{price}")
        print(f"   {description[:150]}...")
        print()

    print()

cur.close()
conn.close()
