"""商品目录图片索引测试。"""

import hashlib
from pathlib import Path

from infra.product_image_catalog import load_product_image_urls
from store.product_catalog_store import _image_url


def test_load_product_image_urls_uses_source_url_hash_and_skips_invalid_entries(tmp_path: Path):
    """原始缩略图只会映射到对应入库 ID，非法链接不进入浏览器响应。"""
    laptop_directory = tmp_path / "laptops"
    phone_directory = tmp_path / "phones"
    laptop_directory.mkdir()
    phone_directory.mkdir()
    source_url = "/notebook/index100.shtml"
    (laptop_directory / "catalog.jsonl").write_text(
        "\n".join(
            (
                '{"详情链接":"/notebook/index100.shtml","缩略图URL":"https://img.example.com/100.png"}',
                '{"详情链接":"/notebook/index200.shtml","缩略图URL":"http://img.example.com/200.png"}',
            )
        ),
        encoding="utf-8",
    )

    image_urls = load_product_image_urls(tmp_path)

    assert image_urls == {
        hashlib.md5(source_url.encode()).hexdigest(): "https://img.example.com/100.png",
    }


def test_product_image_urls_cover_the_checked_in_laptop_catalog():
    """仓库商品源数据含图片，避免部署后所有笔记本卡片退化为占位视觉。"""
    image_urls = load_product_image_urls(Path("data/products/raw"))

    assert len(image_urls) >= 1_000


def test_catalog_uses_source_image_for_existing_ingested_product():
    """早于图片字段的已入库商品仍能按稳定 ID 显示原始缩略图。"""
    source_url = "/notebook/index2161924.shtml"
    product_id = hashlib.md5(source_url.encode()).hexdigest()

    image_url = _image_url(product_id, {"source_url": source_url})

    assert image_url == "https://2d.zol-img.com.cn/product/275_500x375/723/cem0HK2GTVePE.png"
