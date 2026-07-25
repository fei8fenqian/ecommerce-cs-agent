"""清洗 + 标准化爬取的配件数据，输出统一格式的 JSONL

输入: data/products/raw/components/<category>/*.jsonl
输出: data/products/clean/components/<category>.jsonl (一个品类一个文件)

标准化字段:
  - id, name, price, url, category
  - normalized: 品类特定的兼容性关键字段（扁平化）
  - params: 原始参数表（保留）
  - text: 用于向量检索的拼接文本
"""

import json
import re
from pathlib import Path

# =============================================================================
# 产品名称清洗：h3 里可能被 ZOL 塞了参数摘要 span
# "AMD Ryzen 5 5600 适用类型:笔记本 CPU系列:..." → "AMD Ryzen 5 5600"
# =============================================================================

# 匹配末尾参数摘要: " 主芯片组:..." / " PFC类型:..." / " 80PLUS认证:..."
_PARAM_SUFFIX_RE = re.compile(r"\s{1,2}\S+?:.+$", re.DOTALL)


def clean_name(raw: str) -> str:
    """去掉名称末尾被 ZOL 注入的参数摘要"""
    raw = raw.replace("\n", "")
    return _PARAM_SUFFIX_RE.sub("", raw).strip()


ROOT = Path(__file__).parent.parent.parent
RAW_DIR = ROOT / "data" / "products" / "raw" / "components"
CLEAN_DIR = ROOT / "data" / "products" / "clean" / "components"

# =============================================================================
# 价格清洗
# =============================================================================


def clean_price(raw: str | None) -> int | None:
    """'149' → 149, '1099-6399' → 1099, '暂无报价' → None"""
    if not raw or "暂无" in str(raw):
        return None
    m = re.search(r"(\d+)", str(raw).replace(",", ""))
    return int(m.group(1)) if m else None


# =============================================================================
# 参数字典扁平化（去掉分组层级）
# =============================================================================


def flatten_params(params: dict) -> dict:
    """{基本参数: {插槽类型: ..., ...}, ...} → {基本参数_插槽类型: ..., ...}"""
    flat: dict = {}
    for group, fields in params.items():
        if not isinstance(fields, dict):
            continue  # 跳过 error、空值等非正常结构
        for k, v in fields.items():
            flat[f"{group}_{k}"] = v
    return flat


# =============================================================================
# 各品类的兼容性关键字段提取
# =============================================================================

# key_map: {目标字段名: [可能的原始字段名列表]}
# 目标字段名统一用英文（方便后续代码做兼容性匹配）

CPU_KEYS = {
    "socket": ["插槽类型"],
    "memory_type": ["内存类型"],
    "tdp": ["热设计功耗(TDP)"],
    "cores": ["核心数量"],
    "threads": ["线程数量"],
    "usage": ["适用类型"],
}

MOTHERBOARD_KEYS = {
    "socket": ["CPU插槽"],
    "memory_type": ["内存类型"],
    "form_factor": ["主板板型"],
    "dimensions": ["外形尺寸"],
    "chipset": ["主芯片组"],
}

VGA_KEYS = {
    "interface": ["接口类型"],
    "power_draw": ["最大功耗"],
    "vram": ["显存容量"],
    "vram_type": ["显存类型"],
    "gpu_type": ["显卡类型"],
    "dimensions": ["产品尺寸"],
}

MEMORY_KEYS = {
    "memory_type": ["内存类型"],
    "frequency": ["内存主频"],
    "capacity": ["容量描述"],
    "usage": ["适用类型"],
}

SSD_KEYS = {
    "interface": ["接口类型"],
    "capacity": ["存储容量"],
    "dimensions": ["外形尺寸"],
}

HDD_KEYS = {
    "interface": ["接口类型"],
    "capacity": ["硬盘容量"],
    "form_factor": ["硬盘尺寸"],
    "dimensions": ["产品尺寸"],
    "rpm": ["转速"],
}

POWER_KEYS = {
    "wattage": ["额定功率"],
    "dimensions": ["电源尺寸"],
    "certification": ["80PLUS认证"],
    "form_factor": ["电源类型"],
}

CASE_KEYS = {
    "motherboard_support": ["适用主板"],
    "gpu_length_max": ["显卡限长"],
    "cooler_height_max": ["CPU散热器限高"],
    "dimensions": ["产品尺寸"],
    "case_type": ["机箱类型"],
}

COOLER_KEYS = {
    "socket_support": ["适用范围"],
    "dimensions": ["产品尺寸"],
    "type": ["散热器类型"],
    "method": ["散热方式"],
}

# 品类 → key_map + 原始参数里这些字段可能在哪个分组下
CATEGORY_CONFIG = {
    "cpu": {"keys": CPU_KEYS, "param_groups": ["基本参数", "性能参数", "内存参数", "封装规格"]},
    "motherboard": {
        "keys": MOTHERBOARD_KEYS,
        "param_groups": ["主板芯片", "处理器规格", "内存规格", "板型"],
    },
    "vga": {"keys": VGA_KEYS, "param_groups": ["显卡核心", "显存规格", "显卡接口", "其它参数"]},
    "memory": {"keys": MEMORY_KEYS, "param_groups": ["基本参数"]},
    "solid_state_drive": {"keys": SSD_KEYS, "param_groups": ["基本参数", "其它参数"]},
    "hard_drives": {"keys": HDD_KEYS, "param_groups": ["基本参数", "其他参数"]},
    "power": {"keys": POWER_KEYS, "param_groups": ["基本参数"]},
    "case": {"keys": CASE_KEYS, "param_groups": ["基本参数", "外观参数"]},
    "cooling_product": {"keys": COOLER_KEYS, "param_groups": ["基本参数"]},
}


def extract_normalized(params: dict, key_map: dict) -> dict:
    """从原始 params 中提取标准化字段"""
    normalized = {}
    for target_key, source_names in key_map.items():
        for group_name, fields in params.items():
            for source_name in source_names:
                if source_name in fields:
                    normalized[target_key] = fields[source_name]
                    break
            if target_key in normalized:
                break
    return normalized


# =============================================================================
# 拼接检索用文本
# =============================================================================


def build_search_text(name: str, category: str, normalized: dict, params: dict) -> str:
    """拼一段中文文本给 embedding 做向量检索"""
    parts = [name]
    for k, v in normalized.items():
        if v:
            parts.append(f"{k}:{v}")
    for group, fields in params.items():
        if not isinstance(fields, dict):
            continue
        field_text = " ".join(f"{fk}:{fv}" for fk, fv in fields.items() if fv)
        parts.append(field_text)
    return " ".join(parts)


# =============================================================================
# 主流程
# =============================================================================


def normalize_category(category: str, config: dict) -> list[dict]:
    """读取某个品类的所有原始文件，返回标准化后的产品列表"""
    raw_dir = RAW_DIR / category
    if not raw_dir.exists():
        print(f"  ⚠ 目录不存在: {raw_dir}")
        return []

    key_map = config["keys"]
    results = []

    for jsonl_file in sorted(raw_dir.glob("*.jsonl")):
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)

                name = clean_name(raw.get("产品名称", ""))
                price_raw = raw.get("参考价格")
                url = raw.get("详情链接", "")
                raw_id = raw.get("产品ID", "")
                params = raw.get("params", {})

                if not name:
                    continue

                price = clean_price(price_raw)
                normalized = extract_normalized(params, key_map)
                flat_params = flatten_params(params)
                text = build_search_text(name, category, normalized, params)

                results.append(
                    {
                        "id": raw_id,
                        "name": name,
                        "category": category,
                        "price": price,
                        "url": f"https://detail.zol.com.cn{url}" if url else "",
                        "normalized": normalized,
                        "params": flat_params,
                        "text": text,
                    }
                )

    # 去重（按 id）
    seen = set()
    deduped = []
    for r in results:
        if r["id"] not in seen:
            seen.add(r["id"])
            deduped.append(r)

    return deduped


if __name__ == "__main__":
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)

    for category, config in CATEGORY_CONFIG.items():
        print(f"处理: {category}")
        products = normalize_category(category, config)
        if not products:
            print("  ⚠ 无数据，跳过")
            continue

        # 输出
        out_file = CLEAN_DIR / f"{category}.jsonl"
        with open(out_file, "w", encoding="utf-8") as f:
            for p in products:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")

        # 统计
        with_price = sum(1 for p in products if p["price"])
        norm_fields = products[0]["normalized"].keys()
        norm_hits = sum(1 for p in products if any(v for v in p["normalized"].values()))
        print(
            f"  → {len(products)} 条, {with_price} 条有价格, "
            f"字段: {list(norm_fields)}, 覆盖率: {norm_hits}/{len(products)}"
        )

    print("\n完成!")
