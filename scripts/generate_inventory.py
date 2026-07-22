import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
from config import settings

# 仓库城市 + 权重（一线城市权重高，同城配送最常见）
WAREHOUSES = [
    ("北京仓", 25),
    ("上海仓", 25),
    ("深圳仓", 20),
    ("成都仓", 15),
    ("武汉仓", 10),
    ("西安仓", 5),
]

# 库存分布（模拟真实的库存水位）
STOCK_TIERS = [
    ("in_stock", 60, (10, 200)),
    ("low_stock", 18, (1, 9)),
    ("out_of_stock", 8, (0, 0)),
    ("high_stock", 14, (200, 500)),
]


def _connect():
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        dbname=settings.pg_dbname,
    )


def _pick_warehouse() -> str:
    """按权重随机选仓库"""
    names = [w[0] for w in WAREHOUSES]
    weights = [w[1] for w in WAREHOUSES]
    return random.choices(names, weights, k=1)[0]


def _pick_stock() -> int:
    """按分布随机生成库存量"""
    tiers = [t[0] for t in STOCK_TIERS]
    weights = [t[1] for t in STOCK_TIERS]
    tier = random.choices(tiers, weights, k=1)[0]
    for t in STOCK_TIERS:
        if t[0] == tier:
            low, high = t[2]
            return random.randint(low, high)
    return 0


def _ensure_columns(conn: psycopg2.extensions.connection, table: str):
    """给表添加 stock / warehouse 列（幂等：已存在则跳过）"""
    cur = conn.cursor()
    # 查已有列
    cur.execute(
        "select column_name from information_schema.columns where table_name = %s",
        (table,),
    )
    existing = {row[0] for row in cur.fetchall()}

    for col_name, col_def in [
        ("stock", "INTEGER DEFAULT 0"),
        ("warehouse", "VARCHAR(50) DEFAULT ''"),
    ]:
        if col_name in existing:
            print(f"{table}.{col_name} 已存在，跳过")
            continue
        cur.execute(f"alter table {table} add column {col_name} {col_def}")
        print(f"  ✅ {table}.{col_name} 列已添加")

    conn.commit()
    cur.close()


def generate(seed: int = 42, dry_run: bool = False):
    random.seed(seed)
    conn = _connect()
    stats: dict[str, dict] = {}

    for table in ["laptop_products", "phone_products"]:
        print(f"\n{'=' * 50}")
        print(f"处理表: {table}")

        cur = conn.cursor()

        # 1. 确保列存在
        _ensure_columns(conn, table)

        # 2. 查出所有产品 id
        cur.execute(f"select id, product_name, brand from {table}")
        products = cur.fetchall()
        total = len(products)

        # 四种库存等级的计数器
        in_stock = low = out = high = 0
        # 仓库分布 每个仓库里放了多少个 SKU（产品种类）
        wh_counts: dict[str, int] = {}

        # 3. 逐行更新库存
        for pid, _, _ in products:
            stock = _pick_stock()
            warehouse = _pick_warehouse()

            # 统计
            if stock == 0:
                out += 1
            elif stock < 10:
                low += 1
            elif stock < 200:
                in_stock += 1
            else:
                high += 1
            wh_counts[warehouse] = wh_counts.get(warehouse, 0) + 1

            # 写库
            cur.execute(
                f"update {table} set stock = %s, warehouse = %s where id = %s",
                (stock, warehouse, pid),
            )

        stats[table] = {
            "total": total,
            "in_stock": in_stock,
            "low_stock": low,
            "out_of_stock": out,
            "high_stock": high,
            "warehouses": wh_counts,
        }

        cur.close()

    if dry_run:
        print("\n⚠  DRY RUN — 以上 SQL 不会真正提交")
        conn.rollback()

    else:
        conn.commit()
        print("\n✅ 库存数据已写入 PostgreSQL")

    conn.close()

    # 4. 打印统计
    for table, s in stats.items():
        print(f"\n📦 {table}:")
        print(f"   总计: {s['total']} SKU")
        print(
            f"   正常({10}-{199}台): {s['in_stock']} ({s['in_stock']/s['total']*100:.1f}%)"
        )
        print(
            f"   紧张(1-9台):     {s['low_stock']} ({s['low_stock']/s['total']*100:.1f}%)"
        )
        print(
            f"   缺货(0台):       {s['out_of_stock']} ({s['out_of_stock']/s['total']*100:.1f}%)"
        )
        print(
            f"   充足(200+台):    {s['high_stock']} ({s['high_stock']/s['total']*100:.1f}%)"
        )
        print(f"   仓库分布: {s['warehouses']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成产品库存数据")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    parser.add_argument("--dry-run", action="store_true", help="只打印 SQL，不提交")
    args = parser.parse_args()

    generate(seed=args.seed, dry_run=args.dry_run)
