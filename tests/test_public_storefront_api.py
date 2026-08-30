"""公开商品与匿名导购接口的最小回归测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from api.products import product_router


@pytest.mark.asyncio
async def test_public_assistant_uses_only_public_retrieval_and_returns_answer():
    """访客导购不依赖用户身份，也不会创建订单或会话。"""
    app = FastAPI()
    app.state.llm_client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="这款适合游戏使用。")))
    app.include_router(product_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("api.products.hybrid_search", new=AsyncMock(return_value=[])) as search:
            response = await client.post("/api/v1/products/assistant", json={"query": "推荐一台游戏本"})

    assert response.status_code == 200
    assert response.json() == {"answer": "这款适合游戏使用。"}
    search.assert_awaited_once()


@pytest.mark.asyncio
async def test_detail_page_assistant_reads_the_selected_product_not_only_its_title():
    """详情页带来的商品 ID 必须变成模型可引用的参数上下文。"""
    app = FastAPI()
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content="这套内存支持 XMP 3.0。")))
    app.state.llm_client = llm
    app.include_router(product_router)
    product = {
        "id": "memory-1",
        "product_name": "Pallas II DDR5 6000 32G",
        "brand": "宏碁",
        "price": 669.0,
        "description": "DDR5 内存套装",
        "product_type": "内存",
        "specifications": [{"name": "内存规格", "value": "DDR5 6000"}, {"name": "XMP", "value": "支持 XMP 3.0"}],
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("api.products.get_product_detail", new=AsyncMock(return_value=product)),
            patch("api.products.hybrid_search", new=AsyncMock(return_value=[])),
        ):
            response = await client.post(
                "/api/v1/products/assistant",
                json={
                    "query": "介绍这款内存",
                    "product_category": "components",
                    "product_id": "memory-1",
                },
            )

    assert response.status_code == 200
    prompt = llm.chat.await_args.args[0][-1]["content"]
    assert "Pallas II DDR5 6000 32G" in prompt
    assert "支持 XMP 3.0" in prompt
