"""从随应用发布的商品源数据中建立商品 ID 到公开缩略图的只读索引。"""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

RAW_PRODUCT_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "products" / "raw"
PHONE_IMAGE_OVERRIDE_FILE = RAW_PRODUCT_DIRECTORY / "phone_image_overrides.jsonl"
COMPONENT_IMAGE_OVERRIDE_FILE = RAW_PRODUCT_DIRECTORY / "component_image_overrides.jsonl"


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

    # 配件入库时沿用爬虫给出的产品 ID，而不是详情链接的哈希。
    # 把这层差异收敛在图片索引中，目录查询不必知道历史入库规则。
    for path in sorted((raw_product_directory / "components").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            raw_product_id = item.get("产品ID")
            image_url = item.get("缩略图URL")
            if not isinstance(raw_product_id, str) or not isinstance(image_url, str):
                continue
            if image_url.startswith("https://"):
                image_urls[raw_product_id] = image_url

    # 手机图片补采集文件独立保存，不改动历史爬取记录；同一 ID 时优先使用补采集结果。
    override_path = raw_product_directory / PHONE_IMAGE_OVERRIDE_FILE.name
    if override_path.exists():
        for line in override_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            override_product_id = item.get("product_id")
            image_url = item.get("image_url")
            if isinstance(override_product_id, str) and isinstance(image_url, str) and image_url.startswith("https://"):
                image_urls[override_product_id] = image_url

    component_override_path = raw_product_directory / COMPONENT_IMAGE_OVERRIDE_FILE.name
    if component_override_path.exists():
        for line in component_override_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            override_product_id = item.get("product_id")
            image_url = item.get("image_url")
            if isinstance(override_product_id, str) and isinstance(image_url, str) and image_url.startswith("https://"):
                image_urls[override_product_id] = image_url
    return image_urls


@lru_cache(maxsize=1)
def product_image_urls() -> dict[str, str]:
    """懒加载图片索引，避免非商品业务路径承担源数据解析开销。"""
    return load_product_image_urls(RAW_PRODUCT_DIRECTORY)
