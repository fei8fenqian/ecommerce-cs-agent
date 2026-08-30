"""只读商品目录接口，供客户页面浏览并把问题带入现有 AI 对话。"""

import re
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from agent.rag.retrieve import hybrid_search
from config import settings
from exceptions import DependencyUnavailableError
from store.product_catalog_store import (
    ProductCategory,
    build_public_product_context,
    get_product_detail,
    list_product_page,
)

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
    image_url: str | None


class ProductListResponse(BaseModel):
    """一个类别下的只读商品目录。"""

    category: ProductCategory
    products: list[ProductItem]
    total: int
    page: int
    page_size: int
    brands: list[str]
    component_categories: dict[str, str]


class ProductSpecification(BaseModel):
    """详情页中的一项公开规格。"""

    name: str
    value: str


class ProductDetailResponse(ProductItem):
    """商品详情页所需的公开信息和规格表。"""

    specifications: list[ProductSpecification]


class PublicAssistantRequest(BaseModel):
    """未登录访客可发送的公开商品咨询。"""

    query: str = Field(min_length=1, max_length=800)
    product_category: Literal["laptops", "phones", "components"] | None = None
    product_id: str | None = Field(default=None, min_length=1, max_length=128)


class PublicAssistantResponse(BaseModel):
    """不保存会话、不读取个人业务数据的商城 AI 回复。"""

    answer: str


def _public_retrieval_table(query: str) -> str:
    """为匿名咨询选择公开目录表，避免由模型决定数据边界。"""
    normalized = query.lower()
    if any(term in normalized for term in ("手机", "iphone", "安卓", "荣耀", "小米", "华为")):
        return "phone_products"
    if any(term in normalized for term in ("显卡", "cpu", "主板", "内存", "固态", "电源", "配件")):
        return "component_products"
    if any(term in normalized for term in ("保修", "退货", "换货", "售后", "政策")):
        return "knowledge_chunks"
    return "laptop_products"


def _public_context(docs: list[dict]) -> str:
    """移除可能混入索引文本的库存和仓库细节，仅保留客户可见的知识。"""
    if not docs:
        return "未找到匹配的公开资料。"
    parts: list[str] = []
    for doc in docs[:4]:
        content = re.sub(r"(?:库存|仓库|[\u4e00-\u9fa5]+仓)[^。；\n]*[。；]?", "", str(doc.get("content") or ""))
        parts.append(f"[{doc.get('title') or '商品资料'}] {content[:500]}")
    return "\n---\n".join(parts)


@product_router.get("", response_model=ProductListResponse)
async def products(
    category: Literal["laptops", "phones", "components"] = "laptops",
    query: str = Query(default="", max_length=100),
    brand: str = Query(default="", max_length=64),
    component_category: str = Query(default="", max_length=64),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=12, le=48),
) -> ProductListResponse:
    """按类别、品牌和页码浏览已入库商品，不产生任何交易写入。"""
    result = await list_product_page(category, query, brand, component_category, page, page_size)
    return ProductListResponse(category=category, **result)


@product_router.get("/{category}/{product_id}", response_model=ProductDetailResponse)
async def product_detail(
    category: Literal["laptops", "phones", "components"],
    product_id: str,
) -> ProductDetailResponse:
    """读取一件已入库商品的公开详情，不暴露仓库或检索内部字段。"""
    result = await get_product_detail(category, product_id)
    if result is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="商品不可用或无法核验")
    return ProductDetailResponse(**result)


@product_router.post("/assistant", response_model=PublicAssistantResponse)
async def public_assistant(body: PublicAssistantRequest, request: Request) -> PublicAssistantResponse:
    """回答访客的公开商品问题；不创建会话，也不调用订单、支付或售后工具。"""
    if (body.product_category is None) != (body.product_id is None):
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="商品咨询上下文不完整")
    selected_product_context = ""
    table = _public_retrieval_table(body.query)
    if body.product_category and body.product_id:
        product = await get_product_detail(body.product_category, body.product_id)
        if product is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="商品不可用或无法核验")
        selected_product_context = build_public_product_context(product)
        table = {
            "laptops": "laptop_products",
            "phones": "phone_products",
            "components": "component_products",
        }[body.product_category]
    docs = await hybrid_search(body.query, table=table, use_rerank=False)
    messages = [
        {
            "role": "system",
            "content": (
                "你是 Geex Digital 的商品导购。只基于参考资料回答商品参数、适用场景、公开价格和政策。"
                "不要提及仓库、精确库存、内部系统或工具；不要创建订单、工单或承诺付款、退款、发货。"
                "信息不足时直接说明，并建议用户查看商品详情。回答简洁自然。"
            ),
        },
        {
            "role": "user",
            "content": (f"{selected_product_context}\n\n参考资料：\n{_public_context(docs)}\n\n问题：{body.query}"),
        },
    ]
    try:
        response = await request.app.state.llm_client.chat(
            messages,
            temperature=settings.temperature,
            max_tokens=min(settings.max_tokens, 700),
        )
    except Exception as exc:
        raise DependencyUnavailableError("智能导购暂时不可用") from exc
    answer = (response.content or "").strip()
    if not answer:
        answer = "暂时没有生成合适的回答，请换个商品名称或关键词试试。"
    return PublicAssistantResponse(answer=answer)
