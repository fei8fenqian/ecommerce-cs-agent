import psycopg2
from sentence_transformers import SentenceTransformer

conn = psycopg2.connect(
    host="localhost", port=5433, dbname="postgres", user="postgres", password="postgres"
)

cur = conn.cursor()

model = SentenceTransformer("BAAI/bge-large-zh-v1.5")

queries = [
    {
        "text": "花呗分期怎么免息",
        "limit": 3,
    },
    {
        "text": "以旧换新能同时回收几台旧手机",
        "limit": 3,
    },
    {
        "text": "笔记本怎么选CPU处理器",
        "limit": 3,
    },
    {
        "text": "收到货不满意怎么退货退款",
        "limit": 3,
    },
    {
        "text": "学生买电脑有什么优惠",
        "limit": 3,
    },
]

for q in queries:
    print(f"查询: {q['text']}")
    print("-" * 50)

    q_vec = model.encode([q["text"]], normalize_embeddings=True)[0].tolist()
    q_vec_str = str(q_vec)

    cur.execute(
        f"""
            select source, title, content
            from knowledge_chunks
            order by embedding <=> %s::vector
            limit {q['limit']}
        """,
        (q_vec_str,),
    )

    for i, row in enumerate(cur.fetchall(), 1):
        source, title, content = row
        print(f"{i}. [{source}] {title}")
        print(f"   {content[:200]}...")
        print()

    print()

cur.close()
conn.close()
