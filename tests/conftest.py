"""tests/conftest.py — 测试基础设施

Session 级别自动建表 + 最小测试数据，解决 CI 环境中数据库为空的问题。
直接用 psycopg 连接（绕过连接池），避免和 test 文件里的 pool fixture 冲突。
"""

import psycopg
import pytest_asyncio

from config import settings


def _build_dsn() -> str:
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_db():
    """确保 CI 环境有所需的测试表 + 数据。本地已有数据时跳过 INSERT。"""
    dsn = _build_dsn()
    conn = await psycopg.AsyncConnection.connect(dsn)
    await conn.set_autocommit(True)

    # pgvector 扩展
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── laptop_products ──────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS laptop_products (
            id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            price NUMERIC DEFAULT 0,
            stock INTEGER DEFAULT 0,
            warehouse TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM laptop_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO laptop_products (product_name, brand, price, stock, warehouse, description)
            VALUES
            ('联想拯救者Y9000P', '联想', 9999.00, 50, '北京仓', '高性能游戏本 RTX4060 16GB'),
            ('联想拯救者R9000P', '联想', 8999.00, 30, '上海仓', 'AMD游戏本 RTX4060'),
            ('联想小新Pro16', '联想', 5999.00, 100, '深圳仓', '轻薄办公本 16英寸'),
            ('华为MateBook X Pro', '华为', 8999.00, 20, '北京仓', '高端轻薄本 触屏'),
            ('华为MateBook 14', '华为', 5999.00, 45, '上海仓', '中端轻薄本')
        """)

    # ── phone_products ───────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS phone_products (
            id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            price NUMERIC DEFAULT 0,
            stock INTEGER DEFAULT 0,
            warehouse TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM phone_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO phone_products (product_name, brand, price, stock, warehouse, description)
            VALUES
            ('iPhone 15 Pro Max', 'Apple', 9999.00, 30, '北京仓', 'A17 Pro芯片 钛金属'),
            ('华为Mate 60 Pro', '华为', 6999.00, 25, '上海仓', '麒麟芯片 卫星通信')
        """)

    # ── component_products ───────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS component_products (
            id SERIAL PRIMARY KEY,
            product_name TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            price NUMERIC DEFAULT 0,
            stock INTEGER DEFAULT 0,
            warehouse TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT ''
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM component_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO component_products (product_name, brand, price, stock, warehouse, description)
            VALUES
            ('AMD 锐龙7 7800X3D', 'AMD', 2599.00, 20, '北京仓', '8核16线程 游戏CPU'),
            ('Intel 酷睿i5-14600KF', 'Intel', 1899.00, 35, '上海仓', '14核20线程'),
            ('NVIDIA RTX 4060', 'NVIDIA', 2399.00, 15, '深圳仓', '8GB GDDR6')
        """)

    # ── knowledge_chunks ─────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id SERIAL PRIMARY KEY,
            content TEXT NOT NULL DEFAULT ''
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM knowledge_chunks")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO knowledge_chunks (content)
            VALUES
            ('退货政策：7天无理由退货，15天内质量问题可换货。'),
            ('笔记本保修政策：整机保修2年，电池保修1年。'),
            ('支付方式：支持微信支付、支付宝、银行卡。')
        """)

    # ── orders ───────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            tracking_company TEXT NOT NULL DEFAULT '',
            tracking_number TEXT NOT NULL DEFAULT '',
            total_amount NUMERIC DEFAULT 0,
            paid_amount NUMERIC DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT '',
            order_date TIMESTAMP DEFAULT NOW(),
            delivered_at TIMESTAMP,
            phone TEXT NOT NULL DEFAULT ''
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM orders")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO orders
                (order_id, status, tracking_company, tracking_number,
                 total_amount, paid_amount, payment_method, order_date, phone)
            VALUES
            ('ORD-TEST-001', 'shipped', '顺丰速运', 'SF1234567890',
             9999.00, 9999.00, '微信支付', '2026-08-01', '13800138001'),
            ('ORD-TEST-002', 'pending', '', '',
             5999.00, 5999.00, '支付宝', '2026-08-05', '13800138001'),
            ('ORD-TEST-003', 'delivered', '京东物流', 'JD9876543210',
             8999.00, 8999.00, '银行卡', '2026-07-25', '13900139002')
        """)

    # ── order_items ──────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id TEXT NOT NULL,
            product_name TEXT NOT NULL DEFAULT '',
            brand TEXT NOT NULL DEFAULT '',
            price NUMERIC DEFAULT 0,
            quantity INTEGER DEFAULT 1
        )
    """)
    cur = await conn.execute("SELECT COUNT(*) FROM order_items")
    if (await cur.fetchone())[0] == 0:
        await conn.execute("""
            INSERT INTO order_items (order_id, product_name, brand, price, quantity)
            VALUES
            ('ORD-TEST-001', '联想拯救者Y9000P', '联想', 9999.00, 1),
            ('ORD-TEST-002', '联想小新Pro16', '联想', 5999.00, 1),
            ('ORD-TEST-003', '华为MateBook X Pro', '华为', 8999.00, 1)
        """)

    # ── tickets ──────────────────────────────────────────────────────
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id SERIAL PRIMARY KEY,
            ticket_id VARCHAR(20) UNIQUE NOT NULL,
            customer_name VARCHAR(50) DEFAULT '',
            phone VARCHAR(20) DEFAULT '',
            issue TEXT NOT NULL,
            urgency VARCHAR(10) DEFAULT 'medium',
            status VARCHAR(10) DEFAULT '待处理',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await conn.close()
    yield
    # CI 容器跑完就销毁，不需要 teardown
