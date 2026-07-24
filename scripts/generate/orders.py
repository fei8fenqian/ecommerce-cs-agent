"""
scripts/generate_orders.py — 企业级 mock 订单生成器

设计原则：
  1. 固定随机种子 → 每次生成一模一样的数据，可复现
  2. 从真实产品库采样 → 订单的 product_name/price 和 DB 一致
  3. 品牌加权 → 热门品牌多，冷门品牌少，符合市场规律
  4. 状态分布 → 每种物流状态都有覆盖，含异常 case
  5. 时间分布 → 越近的日期订单越多（幂律）
  6. 客户复购 → 300 人客户池，头部客户多次下单（幂律）
  7. 多商品订单 → ~15% 订单含 2-3 件商品（企业采购/家庭购买）
  8. 真实地址 → 含小区名+楼栋+门牌

用法：
  python scripts/generate_orders.py            # 默认 5000 条
  python scripts/generate_orders.py -n 10000   # 10000 条
  python scripts/generate_orders.py --seed 123 # 换种子
"""

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2

ROOT = Path(__file__).parent.parent.parent
DATA = ROOT / "data" / "products" / "raw"

sys.path.insert(0, str(ROOT / "src"))

from config import settings  # noqa: E402

# =============================================================================
# PostgreSQL 连接 + 建表
# =============================================================================


def _pg_connect():
    return psycopg2.connect(
        host=settings.pg_host,
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password.get_secret_value(),
        dbname=settings.pg_dbname,
    )


def _ensure_orders_tables(conn):
    """创建 orders / order_items 表（幂等）"""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_id VARCHAR(20) UNIQUE NOT NULL,
            customer_id VARCHAR(10),
            customer_name VARCHAR(20),
            order_date DATE,
            status VARCHAR(10),
            total_amount NUMERIC(12, 2),
            paid_amount NUMERIC(12, 2),
            discount NUMERIC(12, 2),
            payment_method VARCHAR(20),
            payment_time TIMESTAMP,
            tracking_company VARCHAR(20),
            tracking_number VARCHAR(30),
            shipping_address TEXT,
            phone VARCHAR(11),
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id VARCHAR(20) REFERENCES orders(order_id) ON DELETE CASCADE,
            product_name TEXT,
            category VARCHAR(10),
            brand VARCHAR(20),
            price NUMERIC(12, 2),
            quantity INT
        )
    """)
    conn.commit()
    cur.close()


def _ensure_stock_for_orders(conn, orders: list[dict]):
    """确保所有下单的产品库存 >= 10。"""
    ordered_names: set[str] = set()
    for o in orders:
        for it in o["items"]:
            ordered_names.add(it["product_name"])

    cur = conn.cursor()
    fixed = 0
    for table in ["laptop_products", "phone_products"]:
        cur.execute(
            f"SELECT product_name FROM {table} WHERE product_name = ANY(%s) AND stock < 10",
            (list(ordered_names),),
        )
        low_stock = [row[0] for row in cur.fetchall()]
        for name in low_stock:
            cur.execute(
                f"UPDATE {table} SET stock = %s WHERE product_name = %s",
                (random.randint(10, 80), name),
            )
            fixed += 1
    conn.commit()
    cur.close()
    if fixed:
        print(f"\n🔧 库存一致性修复: {fixed} 款下单产品 stock 补足到 >= 10")


# =============================================================================
# 品牌权重 — 模拟真实市场份额
# =============================================================================
LAPTOP_BRAND_WEIGHTS: dict[str, int] = {
    "联想": 18,
    "华硕": 15,
    "华为": 15,
    "苹果": 14,
    "惠普": 10,
    "戴尔": 10,
    "机械革命": 5,
    "微星": 4,
    "荣耀": 3,
    "Acer宏碁": 3,
    "小米": 2,
    "神舟": 1,
}

PHONE_BRAND_WEIGHTS: dict[str, int] = {
    "苹果": 20,
    "华为": 18,
    "小米": 16,
    "OPPO": 12,
    "vivo": 10,
    "荣耀": 8,
    "红米": 7,
    "三星": 5,
    "真我": 3,
    "一加": 1,
}

# =============================================================================
# 物流公司 + 快递单号前缀
# =============================================================================
TRACKING_COMPANIES: list[tuple[str, str]] = [
    ("顺丰速运", "SF"),
    ("京东物流", "JD"),
    ("中通快递", "ZT"),
    ("圆通速递", "YT"),
    ("EMS", "EMS"),
    ("韵达快递", "YD"),
]

# =============================================================================
# 支付方式（带权重）
# =============================================================================
PAYMENT_METHODS: list[tuple[str, int]] = [
    ("微信支付", 45),
    ("支付宝", 35),
    ("银行卡", 12),
    ("花呗分期", 8),
]

# =============================================================================
# 真实地址组件（组合生成 "上海市浦东新区张江碧波路690号3号楼501室" 这种地址）
# =============================================================================
ADDRESS_PARTS: list[dict[str, Any]] = [
    {
        "city": "北京",
        "districts": ["朝阳区", "海淀区", "丰台区", "昌平区"],
        "landmarks": [
            "望京",
            "中关村",
            "五道口",
            "西二旗",
            "国贸",
            "回龙观",
            "双井",
            "知春路",
            "上地",
            "芍药居",
        ],
        "communities": [
            "融科橄榄城",
            "华清嘉园",
            "远洋一方",
            "首开国风美唐",
            "北苑家园",
            "天通苑",
            "翠微南里",
        ],
    },
    {
        "city": "上海",
        "districts": ["浦东新区", "徐汇区", "杨浦区", "闵行区", "静安区"],
        "landmarks": [
            "张江",
            "漕河泾",
            "五角场",
            "虹桥",
            "陆家嘴",
            "徐家汇",
            "古北",
            "大宁",
            "莘庄",
            "三林",
        ],
        "communities": [
            "中远两湾城",
            "上海康城",
            "三林世博家园",
            "大华锦绣华城",
            "万科城市花园",
            "静安新城",
            "金地自在城",
        ],
    },
    {
        "city": "广州",
        "districts": ["天河区", "番禺区", "海珠区", "白云区"],
        "landmarks": ["珠江新城", "体育西", "客村", "琶洲", "番禺广场", "京溪", "赤岗", "车陂"],
        "communities": ["骏景花园", "祈福新村", "华南碧桂园", "岭南新世界", "光大花园", "逸景翠园"],
    },
    {
        "city": "深圳",
        "districts": ["南山区", "福田区", "宝安区", "龙岗区", "罗湖区"],
        "landmarks": ["科技园", "车公庙", "西丽", "坂田", "宝安中心", "龙华", "布吉", "南山中心"],
        "communities": [
            "桃源村",
            "万科城",
            "鸿荣源壹城中心",
            "深业上城",
            "华润城润府",
            "侨香村",
            "益田村",
        ],
    },
    {
        "city": "杭州",
        "districts": ["西湖区", "滨江区", "余杭区", "拱墅区"],
        "landmarks": ["文三路", "西溪", "未来科技城", "滨江区政府", "三墩", "九堡", "申花", "祥符"],
        "communities": [
            "绿城翡翠城",
            "万科良渚文化村",
            "滨江金色黎明",
            "融创河滨之城",
            "德信东望",
            "龙湖春江郦城",
        ],
    },
    {
        "city": "成都",
        "districts": ["武侯区", "高新区", "锦江区", "成华区"],
        "landmarks": [
            "天府软件园",
            "金融城",
            "春熙路",
            "建设路",
            "大源",
            "中和",
            "万年场",
            "桐梓林",
        ],
        "communities": [
            "南城都汇",
            "华润二十四城",
            "中海国际社区",
            "蓝光金悦城",
            "万科魅力之城",
            "保利大国璟",
        ],
    },
    {
        "city": "武汉",
        "districts": ["洪山区", "武昌区", "江汉区", "东湖高新区"],
        "landmarks": ["光谷", "街道口", "徐东", "楚河汉街", "中南路", "关山", "积玉桥", "南湖"],
        "communities": [
            "百瑞景中央生活区",
            "保利时代",
            "万科金色家园",
            "复地东湖国际",
            "金地格林东郡",
        ],
    },
    {
        "city": "南京",
        "districts": ["鼓楼区", "建邺区", "玄武区", "江宁区"],
        "landmarks": ["新街口", "河西", "仙林", "百家湖", "鼓楼", "奥体", "九龙湖", "麒麟"],
        "communities": [
            "万科金域蓝湾",
            "仁恒江湾城",
            "中海塞纳丽舍",
            "银城东苑",
            "朗诗绿色街区",
            "保利梧桐语",
        ],
    },
    {
        "city": "重庆",
        "districts": ["渝北区", "江北区", "南岸区", "沙坪坝区"],
        "landmarks": [
            "观音桥",
            "汽博中心",
            "南坪",
            "大学城",
            "冉家坝",
            "弹子石",
            "三峡广场",
            "照母山",
        ],
        "communities": [
            "龙湖春森彼岸",
            "融创凡尔赛",
            "金科廊桥水乡",
            "恒大照母山",
            "万科渝园",
            "保利观澜",
        ],
    },
    {
        "city": "长沙",
        "districts": ["岳麓区", "开福区", "天心区", "雨花区"],
        "landmarks": [
            "麓谷",
            "梅溪湖",
            "洋湖",
            "北辰三角洲",
            "德思勤",
            "万家丽",
            "金星路",
            "月亮岛",
        ],
        "communities": ["万科金域国际", "保利麓谷林语", "北辰定江洋", "中建梅溪湖中心", "恒大江湾"],
    },
]

LAST_NAMES = [
    "张",
    "李",
    "王",
    "赵",
    "陈",
    "刘",
    "黄",
    "周",
    "吴",
    "郑",
    "孙",
    "朱",
    "马",
    "胡",
    "林",
    "何",
    "高",
    "罗",
    "郭",
    "杨",
]


# =============================================================================
# 地址生成
# =============================================================================
def _build_address_pool(n: int = 80) -> list[str]:
    """生成带小区名+楼栋+门牌的地址池"""
    addresses: list[str] = []
    for _ in range(n):
        part = random.choice(ADDRESS_PARTS)
        city = part["city"]
        district = random.choice(part["districts"])
        landmark = random.choice(part["landmarks"])
        community = random.choice(part["communities"])
        road_num = random.randint(1, 2000)
        building = random.randint(1, 30)
        unit = random.randint(1, 4)
        room = random.randint(101, 2804)
        addr = (
            f"{city}市{district}{landmark}{community}{road_num}号{building}号楼{unit}单元{room}室"
        )
        addresses.append(addr)
    return addresses


# =============================================================================
# 客户池生成（300 人，幂律复购）
# =============================================================================
def _build_customer_pool(n: int = 300) -> list[dict[str, str]]:
    """生成客户池，每人有唯一 ID + 姓名 + 所在城市"""
    customers: list[dict[str, str]] = []
    cities = list({p["city"] for p in ADDRESS_PARTS})
    for i in range(1, n + 1):
        last = random.choice(LAST_NAMES)
        # 年轻化称呼
        suffix = random.choice(["先生", "女士", "同学", "老师"])
        customers.append(
            {
                "customer_id": f"UID{i:06d}",
                "name": f"{last}{suffix}",
                "city": random.choice(cities),
            }
        )
    return customers


# =============================================================================
# 产品加载
# =============================================================================
def _load_products() -> list[dict[str, Any]]:
    """从 data/products/raw/ 加载所有产品"""
    products: list[dict[str, Any]] = []

    for pattern, category, brand_weights in [
        ("laptops/*_laptops.jsonl", "笔记本", LAPTOP_BRAND_WEIGHTS),
        ("phones/*_phones.jsonl", "手机", PHONE_BRAND_WEIGHTS),
    ]:
        for fpath in sorted(DATA.glob(pattern)):
            brand = fpath.stem.replace("_laptops", "").replace("_phones", "")
            if brand not in brand_weights:
                continue
            for line in fpath.read_text(encoding="utf-8").strip().split("\n"):
                try:
                    item = json.loads(line)
                    name = item.get("产品名称", "")
                    price_str = item.get("参考价格", "0")
                    price = float(price_str) if price_str else 0.0
                    if name and price > 0:
                        products.append(
                            {
                                "product_name": name,
                                "price": price,
                                "brand": brand,
                                "category": category,
                            }
                        )
                except (json.JSONDecodeError, ValueError):
                    continue

    return products


def _weighted_product_pool(
    products: list[dict], brand_weights: dict[str, int], category: str
) -> list[dict]:
    """按品牌权重展开产品池"""
    pool = [p for p in products if p["category"] == category]
    weighted: list[dict] = []
    for p in pool:
        w = brand_weights.get(p["brand"], 1)
        weighted.extend([p] * w)
    return weighted


# =============================================================================
# 主逻辑
# =============================================================================
def generate(n: int = 5000, seed: int = 42) -> list[dict]:
    random.seed(seed)
    products = _load_products()

    if not products:
        raise RuntimeError("未找到产品数据，请确认 data/products/raw/ 下有 JSONL 文件")

    brand_set = {p["brand"] for p in products}
    print(f"加载 {len(products)} 个产品（{len(brand_set)} 个品牌）")

    # -- 产品加权池 ---------------------------------------------------------
    laptop_pool = _weighted_product_pool(products, LAPTOP_BRAND_WEIGHTS, "笔记本")
    phone_pool = _weighted_product_pool(products, PHONE_BRAND_WEIGHTS, "手机")

    # -- 客户池（幂律复购）--------------------------------------------------
    customers = _build_customer_pool(300)
    # 模拟复购：头部客户权重高（幂律），最多可下单 20+ 次
    customer_weights = [int(300 / (i + 1) ** 0.6) for i in range(len(customers))]
    customer_pool: list[dict] = []
    for c, w in zip(customers, customer_weights):
        customer_pool.extend([c] * w)

    # -- 地址池 -------------------------------------------------------------
    addresses = _build_address_pool(80)
    # 70% 订单用客户常住城市，30% 跨城
    customer_city_addresses: dict[str, list[str]] = {}
    for addr in addresses:
        city = addr[:2]  # "北京" / "上海" ...
        customer_city_addresses.setdefault(city, []).append(addr)

    # -- 状态分布 -----------------------------------------------------------
    status_weights: list[tuple[str, int]] = [
        ("待付款", 5),
        ("待发货", 15),
        ("运输中", 25),
        ("已签收", 35),
        ("已完成", 15),
        ("已取消", 5),
    ]
    statuses, status_w = zip(*status_weights)

    # -- 日期分布：最近 30 天，越近越多（幂律）-------------------------------
    today = datetime(2026, 7, 20)
    date_pool: list[str] = []
    for days_ago in range(30):
        d = today - timedelta(days=days_ago)
        date_pool.extend([d.strftime("%Y-%m-%d")] * (30 - days_ago))

    # -- 组装订单 -----------------------------------------------------------
    orders: list[dict] = []
    used_tracking_nums: set[str] = set()
    # 品类比例：55% 笔记本 45% 手机，但用随机加权而非交替，打散更自然
    category_choice = ["笔记本"] * 55 + ["手机"] * 45

    for i in range(n):
        # 随机选品类
        cat = random.choice(category_choice)
        pool = laptop_pool if cat == "笔记本" else phone_pool

        # -- 构建订单商品列表：85% 单件，12% 2 件，3% 3 件 -----------------
        item_count_weights = [(1, 85), (2, 12), (3, 3)]
        counts, count_w = zip(*item_count_weights)
        n_items = random.choices(counts, weights=count_w, k=1)[0]

        items: list[dict] = []
        seen_names: set[str] = set()
        for _ in range(n_items):
            p = random.choice(pool)
            # 避免同一订单里出现完全相同的商品
            retries = 0
            while p["product_name"] in seen_names and retries < 20:
                p = random.choice(pool)
                retries += 1
            seen_names.add(p["product_name"])
            qty = random.choices([1, 2, 3], weights=[80, 15, 5], k=1)[0]
            items.append(
                {
                    "product_name": p["product_name"],
                    "category": p["category"],
                    "brand": p["brand"],
                    "price": round(p["price"], 2),
                    "quantity": qty,
                }
            )

        total_amount = round(sum(it["price"] * it["quantity"] for it in items), 2)

        # -- 订单日期（先定日期，后续支付/物流都基于这个日期）--------------
        order_date = random.choice(date_pool)

        # -- 状态 -----------------------------------------------------------
        status = random.choices(statuses, weights=status_w, k=1)[0]
        is_paid = status not in ("待付款", "已取消")

        # -- 支付信息（基于 order_date）--------------------------------------
        discount = 0.0
        if is_paid and random.random() < 0.10:
            if random.random() < 0.2:
                discount = round(random.uniform(50, 300), 2)  # 大促折扣
            else:
                discount = round(random.uniform(5, 30), 2)  # 小额优惠券
            discount = min(discount, total_amount * 0.5)  # 最多半价

        paid_amount = round(total_amount - discount, 2) if is_paid else 0.0
        payment_method = (
            random.choices(
                [pm for pm, _ in PAYMENT_METHODS], weights=[w for _, w in PAYMENT_METHODS], k=1
            )[0]
            if is_paid
            else None
        )
        payment_time = ""
        if is_paid:
            order_date_dt = datetime.strptime(order_date, "%Y-%m-%d")
            payment_dt = order_date_dt + timedelta(
                minutes=random.randint(0, 120),
                seconds=random.randint(0, 59),
            )
            payment_time = payment_dt.strftime("%Y-%m-%d %H:%M:%S")

        # -- 客户 + 地址（70% 同城，30% 跨城）-------------------------------
        customer = random.choice(customer_pool)
        if random.random() < 0.7:
            city_prefix = customer["city"][:2]
            city_addrs = customer_city_addresses.get(city_prefix, addresses)
            shipping_addr = random.choice(city_addrs) if city_addrs else random.choice(addresses)
        else:
            shipping_addr = random.choice(addresses)

        # -- 物流 -----------------------------------------------------------
        needs_tracking = status in ("运输中", "已签收", "已完成")
        tracking_company: str | None = None
        tracking_number: str | None = None
        if needs_tracking:
            company_name, prefix = random.choice(TRACKING_COMPANIES)
            tracking_company = company_name
            tracking_number = f"{prefix}{random.randint(100000000000, 999999999999)}"
            while tracking_number in used_tracking_nums:
                tracking_number = f"{prefix}{random.randint(100000000000, 999999999999)}"
            used_tracking_nums.add(tracking_number)

        order = {
            "order_id": f"ORD{2026070100001 + i:013d}",
            "items": items,
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "discount": round(discount, 2),
            "payment_method": payment_method,
            "payment_time": payment_time,
            "customer_name": customer["name"],
            "customer_id": customer["customer_id"],
            "order_date": order_date,
            "status": status,
            "tracking_company": tracking_company,
            "tracking_number": tracking_number,
            "shipping_address": shipping_addr,
            "phone": f"1{random.randint(30, 99)}{random.randint(10000000, 99999999)}",
        }
        orders.append(order)

    # =========================================================================
    # 注入异常 case（脏数据——生产环境真实存在）
    # =========================================================================
    transit_indices = [i for i, o in enumerate(orders) if o["status"] == "运输中"]

    # 1. 运输中但缺 tracking_number（物流系统回调漏了）
    for idx in random.sample(transit_indices, min(5, len(transit_indices))):
        orders[idx]["tracking_number"] = None
        orders[idx]["tracking_company"] = None

    # 2. 手机号缺失（微信登录没绑手机）
    for idx in random.sample(range(len(orders)), min(5, len(orders))):
        orders[idx]["phone"] = ""

    # 3. 极致价格（一分钱订单——秒杀/数据异常）
    for idx in random.sample(range(len(orders)), min(3, len(orders))):
        orders[idx]["total_amount"] = 0.01
        orders[idx]["paid_amount"] = 0.01
        orders[idx]["discount"] = 0.0
        for it in orders[idx]["items"]:
            it["price"] = 0.01
            it["quantity"] = 1

    # 4. 待付款但已经过了 48 小时（可能自动取消）
    for idx in random.sample(
        [i for i, o in enumerate(orders) if o["status"] == "待付款"], min(3, len(orders))
    ):
        # 把订单日期改到 3 天前
        old_date = datetime.strptime(orders[idx]["order_date"], "%Y-%m-%d")
        orders[idx]["order_date"] = (old_date - timedelta(days=3)).strftime("%Y-%m-%d")

    # 5. 已取消但有支付记录（退款中）
    for idx in random.sample(
        [i for i, o in enumerate(orders) if o["status"] == "已取消"], min(2, len(orders))
    ):
        orders[idx]["paid_amount"] = orders[idx]["total_amount"]
        orders[idx]["payment_method"] = "微信支付"
        orders[idx]["payment_time"] = (
            datetime.strptime(orders[idx]["order_date"], "%Y-%m-%d")
            + timedelta(minutes=random.randint(5, 30))
        ).strftime("%Y-%m-%d %H:%M:%S")

    # =========================================================================
    # 按日期排序后输出
    # =========================================================================
    orders.sort(key=lambda o: (o["order_date"], o["order_id"]))

    # -- 写入 PostgreSQL -------------------------------------------------------
    conn = _pg_connect()
    _ensure_orders_tables(conn)
    cur = conn.cursor()

    # 先清空旧数据（幂等重跑）
    cur.execute("DELETE FROM order_items")
    cur.execute("DELETE FROM orders")

    for o in orders:
        ptime = o["payment_time"] if o["payment_time"] else None
        cur.execute(
            """INSERT INTO orders
               (order_id, customer_id, customer_name, order_date, status,
                total_amount, paid_amount, discount, payment_method, payment_time,
                tracking_company, tracking_number, shipping_address, phone)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                o["order_id"],
                o["customer_id"],
                o["customer_name"],
                o["order_date"],
                o["status"],
                o["total_amount"],
                o["paid_amount"],
                o["discount"],
                o["payment_method"],
                ptime,
                o["tracking_company"],
                o["tracking_number"],
                o["shipping_address"],
                o["phone"],
            ),
        )
        for it in o["items"]:
            cur.execute(
                """INSERT INTO order_items
                   (order_id, product_name, category, brand, price, quantity)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (
                    o["order_id"],
                    it["product_name"],
                    it["category"],
                    it["brand"],
                    it["price"],
                    it["quantity"],
                ),
            )

    # 库存一致性修复：确保下单产品有库存
    _ensure_stock_for_orders(conn, orders)

    conn.commit()
    cur.close()
    conn.close()
    print(f"\n✅ {len(orders)} 条订单已写入 PostgreSQL")

    # -- 统计 ----------------------------------------------------------------
    brand_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    multi_item_count = 0
    customer_order_counts: dict[str, int] = {}
    payment_counts: dict[str, int] = {}

    for o in orders:
        for it in o["items"]:
            brand_counts[it["brand"]] = brand_counts.get(it["brand"], 0) + 1
        status_counts[o["status"]] = status_counts.get(o["status"], 0) + 1
        if len(o["items"]) > 1:
            multi_item_count += 1
        cid = o["customer_id"]
        customer_order_counts[cid] = customer_order_counts.get(cid, 0) + 1
        if o["payment_method"]:
            payment_counts[o["payment_method"]] = payment_counts.get(o["payment_method"], 0) + 1

    print("\n品类-品牌分布（Top 10）：")
    for brand, cnt in sorted(brand_counts.items(), key=lambda x: -x[1])[:10]:
        bar = "█" * (cnt // 20)
        print(f"  {brand:<8s} {cnt:>5d}  {bar}")

    print("\n状态分布：")
    for s, cnt in sorted(status_counts.items(), key=lambda x: -x[1]):
        pct = cnt / len(orders) * 100
        print(f"  {s}: {cnt:>5d} ({pct:.1f}%)")

    multi_pct = multi_item_count / len(orders) * 100
    print(f"\n多商品订单: {multi_item_count}/{len(orders)} ({multi_pct:.1f}%)")

    print("\n支付方式分布：")
    for pm, cnt in sorted(payment_counts.items(), key=lambda x: -x[1]):
        print(f"  {pm}: {cnt}")

    print("\n客户复购（Top 10）：")
    for cid, cnt in sorted(customer_order_counts.items(), key=lambda x: -x[1])[:10]:
        cust = next((c for c in customers if c["customer_id"] == cid), None)
        name = cust["name"] if cust else cid
        print(f"  {name} ({cid}): {cnt} 单")

    anomaly_count = sum(
        1
        for o in orders
        if o["status"] == "已取消"
        or (o["status"] == "运输中" and not o["tracking_number"])
        or o["phone"] == ""
        or o["total_amount"] == 0.01
    )
    print(f"\n异常数据: {anomaly_count} 条")
    print(f"总计: {len(orders)} 条 → PostgreSQL")

    return orders


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="企业级 mock 订单生成器")
    parser.add_argument("-n", type=int, default=5000, help="订单数量（默认 5000）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（默认 42）")
    args = parser.parse_args()
    generate(n=args.n, seed=args.seed)
