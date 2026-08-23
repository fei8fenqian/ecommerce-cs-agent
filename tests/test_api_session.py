"""会话 API 的显示字段测试。"""

import httpx
import pytest
from fastapi import FastAPI

from agent.llm.session import SessionContext
from api.session import session_router


class _SessionManager:
    async def get(self, session_id: str, owner_user_id: int) -> SessionContext:
        return SessionContext(
            session_id=session_id,
            title="测试会话",
            messages=[
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "你好，有什么可以帮你？"},
            ],
            message_sequence_numbers=[4, 5],
            created_at=1.0,
            last_active=2.0,
        )


@pytest.mark.asyncio
async def test_session_detail_exposes_message_sequence_for_editing():
    """页面只可编辑服务端明确标识的用户消息。"""
    app = FastAPI()

    @app.middleware("http")
    async def fake_auth(request, call_next):
        request.state.user = {"id": 1}
        return await call_next(request)

    app.include_router(session_router)
    app.state.session = _SessionManager()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/sessions/test-session")

    assert response.status_code == 200
    assert [message["sequence_no"] for message in response.json()["messages"]] == [4, 5]
