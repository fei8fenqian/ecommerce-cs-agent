"""tests/conftest.py — 测试基础设施。

测试数据库的结构由 Alembic 创建；本文件只负责校验 schema 和准备最小测试数据。
直接用 psycopg 连接，避免和测试文件里的连接池 fixture 冲突。
"""

from pathlib import Path

import psycopg
import pytest_asyncio

from config import settings

EXPECTED_SCHEMA_REVISION = "6fe44ca01f9a"
REQUIRED_TABLES = {
    "component_products",
    "knowledge_chunks",
    "laptop_products",
    "phone_products",
    "orders",
    "order_items",
    "tickets",
    "users",
}


def _build_dsn() -> str:
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


async def _validate_schema(conn: psycopg.AsyncConnection) -> None:
    cur = await conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    actual_tables = {row[0] for row in await cur.fetchall()}
    missing_tables = REQUIRED_TABLES - actual_tables
    if missing_tables:
        raise RuntimeError(
            "测试数据库缺少 Alembic 创建的表: "
            + ", ".join(sorted(missing_tables))
            + "; 请先执行 PG_DBNAME=<test_db> alembic upgrade head。"
        )

    cur = await conn.execute("SELECT version_num FROM alembic_version")
    version = await cur.fetchone()
    if version is None or version[0] != EXPECTED_SCHEMA_REVISION:
        actual_version = version[0] if version else "<empty>"
        raise RuntimeError(f"测试数据库迁移版本为 {actual_version!r}，预期为 {EXPECTED_SCHEMA_REVISION!r}。")


async def _seed_test_data(conn: psycopg.AsyncConnection) -> None:
    """只插入测试数据，不创建或修改表结构。"""

    cur = await conn.execute("SELECT COUNT(*) FROM laptop_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO laptop_products
                (id, product_name, brand, price, product_type, description,
                 metadata, status, stock, warehouse, content_hash)
            VALUES
                ('laptop-test-001', '联想拯救者Y9000P', '联想', 9999.00, '游戏本',
                 '高性能游戏本 RTX4060 16GB', '{}'::jsonb, '在售', 50, '北京仓', 'test-hash-001'),
                ('laptop-test-002', '联想拯救者R9000P', '联想', 8999.00, '游戏本',
                 'AMD游戏本 RTX4060', '{}'::jsonb, '在售', 30, '上海仓', 'test-hash-002'),
                ('laptop-test-003', '联想小新Pro16', '联想', 5999.00, '轻薄本',
                 '轻薄办公本 16英寸', '{}'::jsonb, '在售', 100, '深圳仓', 'test-hash-003'),
                ('laptop-test-004', '华为MateBook X Pro', '华为', 8999.00, '轻薄本',
                 '高端轻薄本 触屏', '{}'::jsonb, '在售', 20, '北京仓', 'test-hash-004'),
                ('laptop-test-005', '华为MateBook 14', '华为', 5999.00, '轻薄本',
                 '中端轻薄本', '{}'::jsonb, '在售', 45, '上海仓', 'test-hash-005')
            """
        )

    cur = await conn.execute("SELECT COUNT(*) FROM phone_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO phone_products
                (id, product_name, brand, price, description, metadata,
                 status, stock, warehouse, content_hash)
            VALUES
                ('phone-test-001', 'iPhone 15 Pro Max', 'Apple', 9999.00,
                 'A17 Pro芯片 钛金属', '{}'::jsonb, '在售', 30, '北京仓', 'test-hash-101'),
                ('phone-test-002', '华为Mate 60 Pro', '华为', 6999.00,
                 '麒麟芯片 卫星通信', '{}'::jsonb, '在售', 25, '上海仓', 'test-hash-102')
            """
        )

    cur = await conn.execute("SELECT COUNT(*) FROM component_products")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO component_products
                (id, product_name, category, price, url, normalized, params, description,
                 metadata, content_hash, stock, warehouse)
            VALUES
                ('component-test-001', 'AMD 锐龙7 7800X3D', 'cpu', 2599.00, '',
                 '{}'::jsonb, '{}'::jsonb, '8核16线程 游戏CPU', '{}'::jsonb, 'test-hash-201', 20, '北京仓'),
                ('component-test-002', 'Intel 酷睿i5-14600KF', 'cpu', 1899.00, '',
                 '{}'::jsonb, '{}'::jsonb, '14核20线程', '{}'::jsonb, 'test-hash-202', 35, '上海仓'),
                ('component-test-003', 'NVIDIA RTX 4060', 'vga', 2399.00, '',
                 '{}'::jsonb, '{}'::jsonb, '8GB GDDR6', '{}'::jsonb, 'test-hash-203', 15, '深圳仓')
            """
        )

    cur = await conn.execute("SELECT COUNT(*) FROM knowledge_chunks")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO knowledge_chunks (id, source, title, content)
            VALUES
                ('knowledge-test-001', 'test', '退货政策', '退货政策：7天无理由退货，15天内质量问题可换货。'),
                ('knowledge-test-002', 'test', '笔记本保修', '笔记本保修政策：整机保修2年，电池保修1年。'),
                ('knowledge-test-003', 'test', '支付方式', '支付方式：支持微信支付、支付宝、银行卡。')
            """
        )

    cur = await conn.execute("SELECT COUNT(*) FROM orders")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO orders
                (order_id, customer_id, customer_name, order_date, status,
                 total_amount, paid_amount, payment_method, payment_time,
                 tracking_company, tracking_number, shipping_address, phone, delivered_at)
            VALUES
                ('ORD-TEST-001', 'UIDTEST01', '测试用户一', '2026-08-01', 'shipped',
                 9999.00, 9999.00, '微信支付', '2026-08-01 10:00:00',
                 '顺丰速运', 'SF1234567890', '测试地址一', '13800138001', NULL),
                ('ORD-TEST-002', 'UIDTEST01', '测试用户一', '2026-08-05', 'pending',
                 5999.00, 5999.00, '支付宝', '2026-08-05 11:00:00',
                 '', '', '测试地址一', '13800138001', NULL),
                ('ORD-TEST-003', 'UIDTEST02', '测试用户二', '2026-07-25', 'delivered',
                 8999.00, 8999.00, '银行卡', '2026-07-25 12:00:00',
                 '京东物流', 'JD9876543210', '测试地址二', '13900139002', '2026-08-03')
            """
        )

    cur = await conn.execute("SELECT COUNT(*) FROM order_items")
    if (await cur.fetchone())[0] == 0:
        await conn.execute(
            """
            INSERT INTO order_items (order_id, product_name, category, brand, price, quantity)
            VALUES
                ('ORD-TEST-001', '联想拯救者Y9000P', 'laptop', '联想', 9999.00, 1),
                ('ORD-TEST-002', '联想小新Pro16', 'laptop', '联想', 5999.00, 1),
                ('ORD-TEST-003', '华为MateBook X Pro', 'laptop', '华为', 8999.00, 1)
            """
        )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_db():
    """校验迁移后的测试 schema，并插入最小测试数据。"""
    dsn = _build_dsn()
    try:
        conn = await psycopg.AsyncConnection.connect(dsn)
    except psycopg.OperationalError:
        yield
        return

    try:
        if not settings.pg_dbname.endswith("_test"):
            raise RuntimeError(f"测试必须使用名称以 '_test' 结尾的独立数据库；当前数据库为 {settings.pg_dbname!r}。")
        await conn.set_autocommit(True)
        await _validate_schema(conn)
        await _seed_test_data(conn)
    finally:
        await conn.close()
    yield


# =============================================================================
# 测试用 JWT 密钥对（session 级别，所有测试共享）
# =============================================================================
@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_test_keys():
    """生成测试用 RSA 密钥对（真实密钥，非 mock）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    # 如果项目根已有密钥（用户自己生成的），不覆盖
    if Path("private_key.pem").exists() and Path("public_key.pem").exists():
        yield
        return

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    Path("private_key.pem").write_bytes(private_pem)
    Path("public_key.pem").write_bytes(public_pem)

    yield

    # 清理：只删除测试生成的密钥
    Path("private_key.pem").unlink(missing_ok=True)
    Path("public_key.pem").unlink(missing_ok=True)
