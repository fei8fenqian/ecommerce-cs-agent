"""手机产品数据清洗脚本，与 clean_products.py 同模式"""

import json
import re
from pathlib import Path

# ====== 字段映射：标准字段 → ZOL 原始参数名 ======
FIELD_MAP = {
    # 基本信息
    "release_date": ["上市日期"],
    "product_model": ["产品型号"],
    "scene": ["使用场景"],
    "os": ["操作系统"],
    # 外观
    "height": ["长度"],
    "width": ["宽度"],
    "thickness": ["厚度"],
    "weight": ["重量"],
    "material": ["机身材质"],
    "color": ["机身颜色"],
    # 屏幕
    "screen_size": ["屏幕尺寸"],
    "screen_type": ["屏幕类型"],
    "resolution": ["分辨率"],
    "screen_material": ["屏幕材质"],
    "refresh_rate": ["屏幕刷新率"],
    "pixel_density": ["像素密度"],
    "touch_sampling": ["触控采样率"],
    "screen_color": ["屏幕色彩"],
    "brightness": ["屏幕亮度"],
    # 硬件
    "cpu": ["CPU型号"],
    "ram": ["RAM容量"],
    "storage": ["ROM容量"],
    # 摄像头
    "rear_camera_pixels": ["像素"],
    "camera_total": ["摄像头总数"],
    "aperture": ["光圈"],
    "stabilization": ["防抖功能"],
    "zoom": ["变焦倍数"],
    # 电池
    "battery_capacity": ["电池容量"],
    "battery_type": ["电池类型"],
    "wired_charging": ["有线充电"],
    "wireless_charging": ["无线充电"],
    # 连接
    "network_type": ["网络类型"],
    "sim_type": ["SIM卡类型"],
    "wlan": ["WLAN功能"],
    "bluetooth": ["蓝牙"],
    "nfc": ["NFC"],
    # 功能
    "waterproof": ["三防功能"],
    "fingerprint": ["指纹识别"],
    "face_unlock": ["面部识别"],
    "speaker": ["扬声器"],
    "interface": ["机身接口"],
}

# 翻转：原始参数名 → 标准字段
REVERSE_MAP = {}
for std_key, raw_keys in FIELD_MAP.items():
    for raw in raw_keys:
        REVERSE_MAP[raw] = std_key

# ====== 垃圾词 ======
NOISE_WORDS = [
    r"运行流畅",
    r"极速运行",
    r"多任务运行强",
    r"显示流畅",
    r"游戏运行流畅",
    r">",
    r"更多.*?>?",
    r"查看.*?>",
    r"查看外观图?>?",
    r"查看外观>?",
    r"手机性能排行>?",
    r"\d+万张照片\d+首歌曲",
    r"\d+\.?\d*万张照片",
    r"\d+首歌曲",
    r"大电池",
]


def clean_value(std_key: str, raw_val: str, raw_key: str = "") -> str:
    """按字段类型清洗值"""
    if not raw_val:
        return ""

    # 通用清洗
    for noise in NOISE_WORDS:
        raw_val = re.sub(noise, "", raw_val)
    raw_val = raw_val.replace(">", "")
    raw_val = re.sub(r"\s+", " ", raw_val).strip()

    # ===== 数值提取型 =====
    if std_key == "ram":
        m = re.search(r"(\d+)GB", raw_val.replace(" ", ""))
        return f"{m.group(1)}GB" if m else raw_val

    if std_key == "storage":
        m = re.search(r"(\d+)\s*(GB|TB)", raw_val)
        return f"{m.group(1)}{m.group(2)}" if m else raw_val

    if std_key == "weight":
        raw_clean = raw_val.lower().replace(" ", "")
        m = re.search(r"[\d.]+", raw_val)
        if not m:
            return raw_val
        num = float(m.group())
        if raw_clean.endswith("g") and not raw_clean.endswith("kg"):
            return f"{num:.0f}g"
        return f"{num:.0f}g"

    if std_key in ("screen_size",):
        m = re.search(r"[\d.]+", raw_val)
        return f"{m.group()}英寸" if m else raw_val

    if std_key in ("resolution",):
        m = re.search(r"\d+[xX×]\d+", raw_val)
        return m.group() if m else raw_val

    if std_key == "refresh_rate":
        m = re.search(r"\d+", raw_val)
        return f"{m.group()}Hz" if m else raw_val

    if std_key == "brightness":
        m = re.search(r"\d+", raw_val)
        return f"{m.group()}nit" if m else raw_val

    if std_key in ("height", "width", "thickness"):
        return raw_val.replace(" ", "")

    # 电池容量
    if std_key == "battery_capacity":
        m = re.search(r"(\d+)mAh", raw_val.replace(" ", ""))
        return f"{m.group(1)}mAh" if m else raw_val

    # 充电功率
    if std_key in ("wired_charging", "wireless_charging"):
        m = re.search(r"(\d+)\s*[wW]", raw_val)
        return f"{m.group(1)}W" if m else raw_val

    # 摄像头像素 — 取后置主摄
    if std_key == "rear_camera_pixels":
        # "后置摄像头1：5000万像素后置摄像头2：4000万像素..."
        m = re.search(r"后置摄像头1[：:]\s*(\d+)万像素", raw_val)
        return f"{m.group(1)}万像素" if m else raw_val

    # ===== 文字清洗型 =====
    if std_key == "os":
        raw_val = re.sub(r"更多.*$", "", raw_val)
        return raw_val.strip()

    if std_key == "cpu":
        raw_val = re.sub(r"更多.*$", "", raw_val)
        raw_val = re.sub(r">.*$", "", raw_val)
        return raw_val.strip()

    if std_key == "nfc":
        return "支持" if "支持" in raw_val else raw_val.strip()

    if std_key in ("fingerprint", "face_unlock", "waterproof"):
        raw_val = re.sub(r">.*$", "", raw_val)
        return raw_val.strip()

    if std_key == "waterproof":
        # 提取 IP 等级
        m = re.search(r"IP\d+[X\d]*", raw_val.upper())
        return m.group() if m else raw_val

    if std_key == "network_type":
        # 标准化：5G, 4G, 3G
        types = re.findall(r"[543]G", raw_val)
        return "，".join(sorted(set(types), reverse=True)) if types else raw_val

    if std_key == "bluetooth":
        m = re.search(r"蓝牙\d+\.?\d*", raw_val)
        return m.group() if m else raw_val

    # 默认
    return raw_val.strip()


def normalize(product: dict, brand: str) -> dict:
    """将原始产品字典转为标准化字典"""
    clean = {
        "brand": brand,
        "product_name": product.get("产品名称", ""),
        "price": _parse_price(product.get("参考价格", "")),
        "source_url": product.get("详情链接", ""),
    }

    params = product.get("params", {})
    if isinstance(params, str):
        return clean

    for _, param_group in params.items():
        if not isinstance(param_group, dict):
            continue
        for param_key, param_val in param_group.items():
            if param_key in REVERSE_MAP:
                std_key = REVERSE_MAP[param_key]
                cleaned = clean_value(std_key, param_val, raw_key=param_key)
                clean[std_key] = cleaned

    return clean


def _parse_price(val: str) -> int | None:
    """价格字符串 → 整数，￥4699 → 4699"""
    if not val:
        return None
    try:
        return int(re.sub(r"[^\d]", "", str(val)))
    except ValueError:
        return None


def _is_recent(product: dict) -> bool:
    """过滤 2024 年前上市的产品"""
    rd = product.get("release_date", "")
    if not rd:
        return True
    years = re.findall(r"20\d{2}", rd)
    if not years:
        return True
    return int(years[0]) >= 2024


# ====== 主流程 ======
if __name__ == "__main__":
    root = Path(__file__).parent.parent.parent
    normalized_dir = root / "data" / "products" / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = root / "data" / "products" / "raw" / "phones"
    total_all = 0

    for path in sorted(raw_dir.glob("*_phones.jsonl")):
        brand = path.stem.replace("_phones", "")
        out_path = normalized_dir / f"{brand}_phones_normalized.jsonl"

        stats = {"total": 0, "kept": 0, "filtered": 0}
        with open(path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
            for line in fin:
                stats["total"] += 1
                product = json.loads(line)
                clean = normalize(product, brand)

                if not _is_recent(clean):
                    stats["filtered"] += 1
                    continue

                fout.write(json.dumps(clean, ensure_ascii=False) + "\n")
                stats["kept"] += 1

        print(f"{brand}: {stats['total']} → {stats['kept']}（过滤 {stats['filtered']}）")
        total_all += stats["kept"]

    # 合并
    merged_path = root / "data" / "products" / "phones.jsonl"
    with open(merged_path, "w", encoding="utf-8") as fout:
        written = 0
        for path in sorted(normalized_dir.glob("*_phones_normalized.jsonl")):
            with open(path, encoding="utf-8") as fin:
                for line in fin:
                    fout.write(line)
                    written += 1
    print(f"\n合并完成 → {merged_path} ({written} 条)")
    print("手机清洗完成！")
