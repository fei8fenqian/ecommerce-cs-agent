"""会话 API 的显示字段测试。"""

from unittest.mock import patch

import httpx
import pytest
import tiktoken
from fastapi import FastAPI

from agent.customer_presentation import ChoicePresentation

# SessionManager 初始化 tokenizer 时默认可能尝试下载编码表；本文件只验证
# customer-safe session projection，不应依赖外网。
with patch.object(tiktoken, "get_encoding", return_value=object()):
    from agent.llm.session import SessionContext, _model_safe_messages
    from api.session import _customer_message, session_router


class _SessionManager:
    async def get(self, session_id: str, owner_user_id: int) -> SessionContext:
        return SessionContext(
            session_id=session_id,
            title="测试会话",
            messages=[
                {"role": "user", "content": "你好"},
                {
                    "role": "assistant",
                    "content": "请选择订单",
                    "_decision_facts": {"refund_status": "PROCESSING"},
                    "_decision_contexts": [{"subject_id": "SO-PRIVATE", "facts": {"refund_amount": 1}}],
                    "response_control": {"mode": "FACT"},
                    "_presentation": ChoicePresentation(title="请选择订单", options=[]).model_dump(mode="json"),
                },
                {"role": "system", "content": "internal"},
                {"role": "tool", "content": "private tool result"},
            ],
            message_sequence_numbers=[4, 5, 6, 7],
            created_at=1.0,
            last_active=2.0,
        )


@pytest.mark.asyncio
async def test_session_detail_returns_customer_allow_list_and_presentation():
    """客户历史只返回允许字段，并能恢复已保存的 customer-safe card。"""
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
    payload = response.json()
    assert len(payload["messages"]) == 2
    assert "_decision_facts" not in response.text
    assert "_decision_contexts" not in response.text
    assert "response_control" not in response.text
    assert "internal" not in response.text
    assert "private tool result" not in response.text
    assert payload["messages"][1]["presentation"]["kind"] == "choice"


def test_invalid_or_internal_presentation_is_dropped_from_customer_history():
    message = {
        "role": "assistant",
        "content": "旧会话消息",
        "_presentation": {"kind": "status", "version": 1, "status": "raw"},
        "response_control": {"mode": "FACT"},
    }
    safe = _customer_message(message, 8)
    assert safe is not None
    assert safe.presentation is None
    assert safe.model_dump() == {
        "role": "assistant",
        "content": "旧会话消息",
        "sequence_no": 8,
        "presentation": None,
    }


def test_presentation_and_internal_metadata_never_enter_model_history():
    messages = [
        {
            "role": "assistant",
            "content": "请选择订单",
            "_presentation": {"kind": "choice", "version": 1},
            "_decision_facts": {"refund_status": "PROCESSING"},
            "_decision_contexts": [{"subject_id": "SO-PRIVATE", "facts": {"refund_amount": 1}}],
        }
    ]

    assert _model_safe_messages(messages) == [{"role": "assistant", "content": "请选择订单"}]
