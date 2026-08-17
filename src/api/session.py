from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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


class SessionDetailResponse(BaseModel):
    """会话详情"""

    session_id: str
    title: str
    created_at: float
    last_active: float
    messages: list[dict]


session_router = APIRouter(prefix="/api/v1", tags=["会话记录"])


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
    return SessionDetailResponse(
        session_id=session_ctx.session_id,
        title=session_ctx.title,
        created_at=session_ctx.created_at,
        last_active=session_ctx.last_active,
        messages=session_ctx.messages,
    )


@session_router.delete("/sessions/{session_id}")
async def del_session(session_id: str, request: Request):
    session: SessionManager = request.app.state.session
    user_id = request.state.user["id"]
    success = await session.delete(session_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="删除失败")
    return {"ok": success}
