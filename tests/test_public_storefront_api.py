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
