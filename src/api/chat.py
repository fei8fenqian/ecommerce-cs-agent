from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from agent.sentiment import build_escalation_prompt, detect_sentiment


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    session_id: str | None = Field(None, description="不传则自动创建新会话")


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    total_steps: int
    total_tokens: int


router = APIRouter(prefix="/api/v1", tags=["聊天"])


# 聊天管线
@router.post("/chat", response_model=ChatResponse)
async def chat(chat_req: ChatRequest, request: Request):
    agent = request.app.state.agent
    session = request.app.state.session
    # 获取或创建会话
    ctx = await session.get_or_create(chat_req.session_id)
    # 判断指代词对应的实体
    resolved_query = await session.resolve(chat_req.query, ctx.session_id)
    # 用户情感判断
    sentiment = detect_sentiment(resolved_query, history=ctx.messages)
    sentiment_ctx = build_escalation_prompt(sentiment)
    # run agent
    loop_result = await agent.run(
        resolved_query, history=ctx.messages, system_prompt_extra=sentiment_ctx
    )
    # 当前对话放入上下文ctx
    await session.add_turn(ctx.session_id, chat_req.query, loop_result)
    return ChatResponse(
        answer=loop_result.answer,
        session_id=ctx.session_id,
        total_steps=loop_result.total_steps,
        total_tokens=loop_result.total_tokens,
    )
