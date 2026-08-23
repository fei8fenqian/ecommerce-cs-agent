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
        "warehouse": "华南仓",
        "image_url": None,
    }
    with patch("api.products.list_products", new=AsyncMock(return_value=[product])):
        response = await request(app, "/api/v1/products?category=laptops&query=示例")

    assert response.status_code == 200
    body = response.json()
    assert body["category"] == "laptops"
    assert body["products"] == [product]


@pytest.mark.asyncio
async def test_list_phone_products_forwards_category_and_limit():
    """类别与限制由 API 校验后传给受白名单保护的 Store。"""
    app = FastAPI()
    app.include_router(product_router)
    mocked_list = AsyncMock(return_value=[])
    with patch("api.products.list_products", new=mocked_list):
        response = await request(app, "/api/v1/products?category=phones&query=OPPO&limit=12")

    assert response.status_code == 200
    mocked_list.assert_awaited_once_with("phones", "OPPO", 12)
