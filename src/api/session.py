from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, TypeAdapter, ValidationError

from agent.customer_presentation import CustomerPresentation
from agent.llm.session import SessionManager


class SessionItem(BaseModel):
    """会话元数据"""

    session_id: str
    title: str
    created_at: float
    last_active: float
    message_count: int


class SessionListResponse(BaseModel):
    """当前用户的所有会话列表"""

    sessions: list[SessionItem]
    total: int


class SessionMessageResponse(BaseModel):
    """客户历史消息 allow-list；内部 session payload 永不直接出 API。"""

    role: Literal["user", "assistant"]
    content: str
    sequence_no: int
    presentation: CustomerPresentation | None = None


class SessionDetailResponse(BaseModel):
    """会话详情"""

    session_id: str
    title: str
    created_at: float
    last_active: float
    messages: list[SessionMessageResponse]


session_router = APIRouter(prefix="/api/v1", tags=["会话记录"])

_PRESENTATION_ADAPTER = TypeAdapter(CustomerPresentation)


def _customer_presentation(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        parsed = _PRESENTATION_ADAPTER.validate_python(value)
    except ValidationError:
        return None
    return parsed.model_dump(mode="json", exclude_none=True)


def _customer_message(message: object, sequence_no: int) -> SessionMessageResponse | None:
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    # tool/system messages and assistant tool-call payloads are internal traces,
    # not customer chat history.
    if role not in {"user", "assistant"} or message.get("tool_calls"):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    return SessionMessageResponse(
        role=role,
        content=content,
        sequence_no=sequence_no,
        presentation=_customer_presentation(message.get("_presentation")),
    )


@session_router.get("/sessions", response_model=SessionListResponse)
async def get_sessions(request: Request):
    session: SessionManager = request.app.state.session
    user_id = request.state.user["id"]
    results: list[dict[str, Any]] = await session.list_sessions(user_id)
    session_list: list[SessionItem] = []
    for res in results:
        session_list.append(
            SessionItem(
                session_id=res.get("session_id", ""),
                title=res.get("title", ""),
                created_at=res.get("created_at", 0.0),
                last_active=res.get("last_active", 0.0),
                message_count=res.get("message_count", 0),
            )
        )
    return SessionListResponse(sessions=session_list, total=len(session_list))


@session_router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str, request: Request):
    session: SessionManager = request.app.state.session
    user_id = request.state.user["id"]
    session_ctx = await session.get(session_id, user_id)
    if session_ctx is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    safe_messages = [
        safe_message
        for index, message in enumerate(session_ctx.messages)
        if (
            safe_message := _customer_message(
                message,
                session_ctx.message_sequence_numbers[index]
                if index < len(session_ctx.message_sequence_numbers)
                else index,
            )
        )
        is not None
    ]
    return SessionDetailResponse(
        session_id=session_ctx.session_id,
        title=session_ctx.title,
        created_at=session_ctx.created_at,
        last_active=session_ctx.last_active,
        messages=safe_messages,
    )


@session_router.delete("/sessions/{session_id}")
async def del_session(session_id: str, request: Request):
    session: SessionManager = request.app.state.session
    user_id = request.state.user["id"]
    success = await session.delete(session_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="删除失败")
    return {"ok": success}
