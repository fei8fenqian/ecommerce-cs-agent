import json
import re
from pathlib import Path

# ====== 映射表 ======
FIELD_MAP = {
    "cpu": ["CPU型号"],
    "cpu_series": ["CPU系列"],
    "cpu_cores": ["核心/线程数"],
    "ram": ["内存容量"],
    "ram_type": ["内存类型"],
    "storage": ["硬盘容量"],
    "storage_desc": ["硬盘描述"],
    "screen_size": ["屏幕尺寸"],
    "resolution": ["屏幕分辨率"],
    "refresh_rate": ["屏幕刷新率"],
    "gpu_type": ["显卡类型"],
    "gpu_chip": ["显卡芯片"],
    "weight": ["笔记本重量"],
    "battery": ["电池类型"],
    "cpu_base_freq": ["CPU主频"],
    "cpu_turbo_freq": ["最高睿频"],
    "brightness": ["亮度"],
    "color_gamut": ["sRGB色域", "DCI-P3色域", "色域", "NTSC色域", "Adobe RGB色域"],
    "screen_ratio": ["显示比例"],
    "gpu_vram": ["显存容量"],
    "thickness": ["厚度"],
    "battery_life": ["续航时间"],
    "os": ["操作系统"],
    "product_type": ["产品定位", "产品类型"],
    "usb_ports": ["数据接口", "其它接口"],
    "video_ports": ["视频接口"],
    "release_date": ["上市时间"],
}

# 翻转
REVERSE_MAP = {}
for std_key, raw_keys in FIELD_MAP.items():
    for raw in raw_keys:
        REVERSE_MAP[raw] = std_key

# 垃圾词列表，匹配后删除
NOISE_WORDS = [
    r"运行流畅",
    r"极速运行",
    r"多任务运行强",
    r"显示流畅",
    r"旗舰机",
    r"高端机",
    r"中端主流机",
    r"强",
    r"流畅",
    r"高清",
    r"普清",
    r"热门游戏本>?",
    r"长续航笔记本>?",
    r"耗电高",
    r"更多.*?>",
    r"游戏、便捷",
    r"略显吃力",
    r"多任务运行弱",
    r"运行弱",
]

# 色域类型标注：raw_key → 显示前缀
GAMUT_PREFIX = {
    "sRGB色域": "sRGB",
    "DCI-P3色域": "DCI-P3",
    "NTSC色域": "NTSC",
    "Adobe RGB色域": "AdobeRGB",
    "色域": "",
}


def clean_value(std_key, raw_val, raw_key=""):
    """按字段类型清洗值"""
    if not raw_val:
        return ""

    # ===== 通用：去掉垃圾词 =====
    for noise in NOISE_WORDS:
        raw_val = re.sub(noise, "", raw_val)
    # 去掉所有残留的 >（都在数据中是噪音）
    raw_val = raw_val.replace(">", "")
    raw_val = re.sub(r"\s+", " ", raw_val).strip()

    # ===== 单位保留型：提取数字，拼回单位 =====
    if std_key == "ram":
        # "32GB（16GB×2）极速运行" → "32GB"
        m = re.search(r"(\d+)GB", raw_val.replace(" ", ""))
        return f"{m.group(1)}GB" if m else raw_val

    if std_key == "storage":
        m = re.search(r"(\d+)\s*(GB|TB)", raw_val)
        return f"{m.group(1)}{m.group(2)}" if m else raw_val

    if std_key == "weight":
        # "970g" → "0.97Kg", "1.49Kg" → "1.49Kg"
        raw_clean = raw_val.lower().replace(" ", "")
        m = re.search(r"[\d.]+", raw_val)
        if not m:
            return raw_val
        num = float(m.group())
        if raw_clean.endswith("g") and not raw_clean.endswith("kg"):
            return f"{num / 1000:.2f}Kg"
        return f"{num}Kg"

    if std_key == "screen_size":
        m = re.search(r"[\d.]+", raw_val)
        return f"{m.group()}英寸" if m else raw_val

    if std_key == "resolution":
        m = re.search(r"\d+[xX×]\d+", raw_val)
        return m.group() if m else raw_val

    if std_key == "brightness":
        # "400nits" → "400nit"
        m = re.search(r"\d+", raw_val)
        return f"{m.group()}nit" if m else raw_val

    if std_key == "refresh_rate":
        m = re.search(r"\d+", raw_val)
        return f"{m.group()}Hz" if m else raw_val

    if std_key == "cpu_base_freq":
        m = re.search(r"[\d.]+", raw_val)
        return f"{m.group()}GHz" if m else raw_val

    if std_key == "cpu_turbo_freq":
        m = re.search(r"[\d.]+", raw_val)
        return f"{m.group()}GHz" if m else raw_val

    if std_key == "thickness":
        return raw_val.replace("mm", "mm").strip()

    if std_key == "gpu_vram":
        m = re.search(r"(\d+)GB", raw_val.replace(" ", ""))
        return f"{m.group(1)}GB" if m else raw_val

    # ===== 文字清洗型 =====
    if std_key == "product_type":
        types = re.findall(
            r"(轻薄笔记本|轻薄本|游戏本|商务办公本|商务本|"
            "家用|商用|全能学生本|影音娱乐本|二合一笔记本)",
            raw_val,
        )
        return types[0] if types else raw_val

    if std_key == "os":
        raw_val = raw_val.replace("预装", "")
        raw_val = re.sub(r"[（(].*", "", raw_val)
        raw_val = raw_val.replace("64bit", "").replace("64位", "")
        return raw_val.strip()

    # CPU / GPU 芯片
    if std_key in ("cpu", "cpu_series", "gpu_chip", "gpu_type", "ram_type", "storage_desc"):
        raw_val = re.sub(r">.*$", "", raw_val)
        raw_val = re.sub(r"更多.*$", "", raw_val)
        return raw_val.strip()

    # 接口类
    if std_key == "usb_ports":
        # 剔除 RJ45、电源等非 USB 接口
        segments = re.split(r"[；;]", raw_val)
        usb_segments = [
            s.strip()
            for s in segments
            if s.strip() and not re.search(r"RJ45|网络接口|电源接口|电源", s)
        ]
        return "；".join(usb_segments).strip()

    if std_key == "video_ports":
        return raw_val.strip()

    # 电池
    if std_key == "battery":
        return raw_val.strip()

    # 色域：带上类型前缀
    if std_key == "color_gamut":
        m = re.search(r"\d+%", raw_val)
        if not m:
            return raw_val
        prefix = GAMUT_PREFIX.get(raw_key, "")
        return f"{prefix}:{m.group()}" if prefix else m.group()

    # 日期
    if std_key == "release_date":
        return raw_val.strip()

    return raw_val.strip()


def normalize(product, brand):
    clean = {
        "brand": brand,
        "product_name": product.get("产品名称", ""),
        "price": _parse_price(product.get("参考价格", "")),
        "source_url": product.get("详情链接", ""),
    }

    params = product.get("params", {})
    if isinstance(params, str):
        return clean

    merged_fields = {"usb_ports"}

    for _, param in params.items():
        if not isinstance(param, dict):
            continue
        for param_key, param_val in param.items():
            if param_key in REVERSE_MAP:
                std_key = REVERSE_MAP[param_key]
                cleaned = clean_value(std_key, param_val, raw_key=param_key)

                if std_key in merged_fields and std_key in clean:
                    if cleaned and cleaned not in clean[std_key]:
                        clean[std_key] = clean[std_key] + "；" + cleaned
                else:
                    clean[std_key] = cleaned

    return clean


def _parse_price(val):
    """价格字符串转整数"""
    if not val:
        return None
    try:
        return int(re.sub(r"[^\d]", "", str(val)))
    except ValueError:
        return None


def _is_recent(product):
    """判断产品是否 2024 年及以后上市"""
    rd = product.get("release_date", "")
    if not rd:
        return True  # 没有日期的不删
    years = re.findall(r"20\d{2}", rd)
    if not years:
        return True
    return int(years[0]) >= 2024


# ====== 主流程 ======
if __name__ == "__main__":
    root = Path(__file__).parent.parent.parent
    normalized_dir = root / "data" / "products" / "normalized"
    normalized_dir.mkdir(parents=True, exist_ok=True)

    raw_dir = root / "data" / "products" / "raw" / "laptops"
    for path in raw_dir.glob("*.jsonl"):
        brand = path.stem.replace("_laptops", "")
        out_path = normalized_dir / f"{brand}_normalized.jsonl"

        with open(path, encoding="utf-8") as fin, open(out_path, "w", encoding="utf-8") as fout:
            stats = {"total": 0, "kept": 0, "filtered": 0}
            for line in fin:
                stats["total"] += 1
                product = json.loads(line)
                clean = normalize(product, brand)

                # 过滤 2024 年前的产品
                if not _is_recent(clean):
                    stats["filtered"] += 1
                    continue

                fout.write(json.dumps(clean, ensure_ascii=False) + "\n")
                stats["kept"] += 1

            print(f"  {stats['total']} → {stats['kept']}（过滤 {stats['filtered']}）")

        print(f"{brand} → {out_path.name}")

    # 合并所有品牌到一个文件
    merged_path = normalized_dir.parent / "laptops.jsonl"
    with open(merged_path, "w", encoding="utf-8") as fout:
        total = 0
        for path in sorted(normalized_dir.glob("*_normalized.jsonl")):
            with open(path, encoding="utf-8") as fin:
                for line in fin:
                    fout.write(line)
                    total += 1
    print(f"\n合并完成 → {merged_path} ({total} 条)")

    print("清洗完成！")
