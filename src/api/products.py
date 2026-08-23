"""只读商品目录接口，供客户页面浏览并把问题带入现有 AI 对话。"""

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from store.product_catalog_store import ProductCategory, list_products

product_router = APIRouter(prefix="/api/v1/products", tags=["商品目录"])


class ProductItem(BaseModel):
    """商品目录卡片使用的最小公开字段。"""

    id: str
    product_name: str
    brand: str
    price: float | None
    description: str
    product_type: str
    status: str
    stock: int
    warehouse: str
    image_url: str | None


class ProductListResponse(BaseModel):
    """一个类别下的只读商品目录。"""

    category: ProductCategory
    products: list[ProductItem]


@product_router.get("", response_model=ProductListResponse)
async def products(
    category: Literal["laptops", "phones"] = "laptops",
    query: str = Query(default="", max_length=100),
    limit: int = Query(default=24, ge=1, le=48),
) -> ProductListResponse:
    """按类别浏览已入库的商品，不产生下单、预留或库存写入。"""
    result = await list_products(category, query, limit)
    return ProductListResponse(category=category, products=[ProductItem(**item) for item in result])
