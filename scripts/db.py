"""
数据库连接模块。

之后 src/ 下的检索引擎、rerank、ingest 等所有需要连 PG
的模块，统一从这里导入 connect_db()，不用每个文件写一遍连接串。
"""

import psycopg2


def connect_db() -> psycopg2.extensions.connection:
    """连接 pgvector，自动创建 vector 扩展，返回连接对象。

    各调用方自己负责建表 / 删表 / 查询。
    """
    conn = psycopg2.connect(
        host="localhost",
        port=5433,
        user="postgres",
        password="postgres",
        dbname="postgres",
    )
    cur = conn.cursor()
    cur.execute("create extension if not exists vector")
    conn.commit()
    cur.close()
    return conn
