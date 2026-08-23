"""从随应用发布的商品源数据中建立商品 ID 到公开缩略图的只读索引。"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RAW_PRODUCT_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "products" / "raw"


def load_product_image_urls(raw_product_directory: Path) -> dict[str, str]:
    """读取原始商品 JSONL，返回已入库商品 ID 对应的公开缩略图链接。

    Args:
        raw_product_directory: 包含 laptops、phones 原始 JSONL 文件的目录。

    Returns:
        以入库时使用的 ``md5(source_url)`` 为键的图片链接。缺图、格式异常或
        非 HTTPS 链接的记录会被忽略，目录页面会回退到自身的占位视觉。
    """
    image_urls: dict[str, str] = {}
    for category in ("laptops", "phones"):
        for path in sorted((raw_product_directory / category).glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item: dict[str, Any] = json.loads(line)
                source_url = item.get("详情链接")
                image_url = item.get("缩略图URL")
                if not isinstance(source_url, str) or not isinstance(image_url, str):
                    continue
                if not image_url.startswith("https://"):
                    continue
                product_id = hashlib.md5(source_url.encode()).hexdigest()
                image_urls[product_id] = image_url
    return image_urls


@lru_cache(maxsize=1)
def product_image_urls() -> dict[str, str]:
    """懒加载图片索引，避免非商品业务路径承担源数据解析开销。"""
    return load_product_image_urls(RAW_PRODUCT_DIRECTORY)
