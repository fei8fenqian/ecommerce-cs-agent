"""商品目录 API 只读行为测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.products import product_router


async def request(app: FastAPI, path: str) -> httpx.Response:
    """经 ASGI 直接请求，避免同步 TestClient 桥接影响接口验证。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_list_laptop_products_returns_public_catalog_fields():
    """目录 API 返回前端卡片所需字段，而不暴露商品内部向量数据。"""
    app = FastAPI()
    app.include_router(product_router)
    product = {
        "id": "lp-1",
        "product_name": "示例笔记本",
        "brand": "示例品牌",
        "price": 5999.0,
        "description": "16GB 内存",
        "product_type": "轻薄本",
        "status": "在售",
        "stock": 8,
        "image_url": None,
    }
    catalog_page = {
        "products": [product],
        "total": 1,
        "page": 1,
        "page_size": 24,
        "brands": ["示例品牌"],
        "component_categories": {},
    }
    with patch("api.products.list_product_page", new=AsyncMock(return_value=catalog_page)):
        response = await request(app, "/api/v1/products?category=laptops&query=示例")

    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "laptops"
    assert body["products"] == [product]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_list_phone_products_forwards_pagination_and_filters():
    """类别、筛选和页码由 API 校验后传给受白名单保护的 Store。"""
    app = FastAPI()
    app.include_router(product_router)
    mocked_list = AsyncMock(
        return_value={
            "products": [],
            "total": 0,
            "page": 2,
            "page_size": 12,
            "brands": ["OPPO"],
            "component_categories": {},
        }
    )
    with patch("api.products.list_product_page", new=mocked_list):
        response = await request(app, "/api/v1/products?category=phones&query=OPPO&brand=OPPO&page=2&page_size=12")

    assert response.status_code == 200
    mocked_list.assert_awaited_once_with("phones", "OPPO", "OPPO", "", 2, 12)


@pytest.mark.asyncio
async def test_list_components_accepts_component_category():
    """配件目录可按固定配件分类筛选，仍保持只读。"""
    app = FastAPI()
    app.include_router(product_router)
    mocked_list = AsyncMock(
        return_value={
            "products": [],
            "total": 0,
            "page": 1,
            "page_size": 24,
            "brands": [],
            "component_categories": {"cpu": "CPU"},
        }
    )
    with patch("api.products.list_product_page", new=mocked_list):
        response = await request(app, "/api/v1/products?category=components&component_category=cpu")

    assert response.status_code == 200
    mocked_list.assert_awaited_once_with("components", "", "", "cpu", 1, 24)


@pytest.mark.asyncio
async def test_product_detail_returns_public_specifications():
    """详情接口只返回卡片字段和规格表，不返回检索或仓库内部字段。"""
    app = FastAPI()
    app.include_router(product_router)
    detail = {
        "id": "lp-1",
        "product_name": "示例笔记本",
        "brand": "示例品牌",
        "price": 5999.0,
        "description": "16GB 内存",
        "product_type": "轻薄本",
        "status": "在售",
        "stock": 8,
        "image_url": "https://example.test/laptop.jpg",
        "specifications": [{"name": "内存", "value": "16GB"}],
    }
    with patch("api.products.get_product_detail", new=AsyncMock(return_value=detail)):
        response = await request(app, "/api/v1/products/laptops/lp-1")

    assert response.status_code == 200
    assert response.json()["specifications"] == detail["specifications"]
