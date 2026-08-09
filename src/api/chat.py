import json
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.engines.loop import LoopResult
from agent.rag.retrieve import hybrid_search
from agent.sentiment import build_escalation_prompt, detect_sentiment
from config import settings
from exceptions import LLMError

_chat_logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000, description="用户消息")
    session_id: str | None = Field(None, description="不传则自动创建新会话")


class ChatResponse(BaseModel):
    answer: str
    session_id: str
    total_steps: int
    total_tokens: int


chat_router = APIRouter(prefix="/api/v1", tags=["聊天"])


def _build_context(docs: list[dict]) -> str:
    """把检索结果拼成上下文字符串"""
    if not docs:
        return "（未找到相关内容）"
    lines = []
    for doc in docs[: settings.rerank_top_k]:
        title = doc.get("title", "?")
        content = doc.get("content", "")[:300]
        lines.append(f"[来源: {title} {content}]")
    return "\n-----\n".join(lines)


@chat_router.post("/chat", response_model=ChatResponse)
async def chat(chat_req: ChatRequest, request: Request):
    try:
        agent = request.app.state.agent
        session = request.app.state.session
        intent_router = request.app.state.intent_router

        # 获取历史会话或创建新会话
        ctx = await session.get_or_create(chat_req.session_id)

        # 判断指代词对应的实体
        resolved_query = await session.resolve(chat_req.query, ctx.session_id)

        # 用户情感判断
        sentiment = detect_sentiment(resolved_query, history=ctx.messages)
        sentiment_ctx = build_escalation_prompt(sentiment)

        # 意图路由
        intent = await intent_router.route(resolved_query)

        if intent.target == "plan_execute":
            plan_agent = request.app.state.plan_execute_agent
            plan_state = await plan_agent.run(
                resolved_query,
                history=ctx.messages,
                scenario=intent.scenario,
            )
            # plan_execute 不走 AgentLoop，手动记录到 session
            await session.add_turn_simple(ctx.session_id, chat_req.query, plan_state.get("answer", ""))
            return ChatResponse(
                answer=plan_state.get("answer", ""),
                session_id=ctx.session_id,
                total_steps=len(plan_state.get("plan", [])),
                total_tokens=plan_state.get("total_tokens", 0),
            )
        elif intent.target == "rag":
            docs = await hybrid_search(resolved_query, table=intent.table)
            context = _build_context(docs)
            loop_result = await agent.run(
                resolved_query,
                context=context,
                history=ctx.messages,
                system_prompt_extra=sentiment_ctx,
            )
        else:
            loop_result = await agent.run(resolved_query, history=ctx.messages, system_prompt_extra=sentiment_ctx)

        # 当前对话放入上下文ctx
        await session.add_turn(ctx.session_id, chat_req.query, loop_result)
        return ChatResponse(
            answer=loop_result.answer,
            session_id=ctx.session_id,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
        )
    except LLMError as e:
        _chat_logger.error(
            "LLM 调用失败: query=%s retry=%d status=%s reason=%s",
            chat_req.query,
            e.retry_count,
            e.status_code,
            e.last_response,
        )
        return ChatResponse(
            answer="服务暂时不可用",
            session_id=ctx.session_id,
            total_steps=0,
            total_tokens=0,
        )
    except Exception:
        _chat_logger.exception("chat 端点异常: query=%s", chat_req.query)
        raise


@chat_router.post("/chat/stream")
async def chat_stream(chat_req: ChatRequest, request: Request):
    agent = request.app.state.agent
    session = request.app.state.session
    intent_router = request.app.state.intent_router

    # 历史会话
    session_ctx = await session.get_or_create(chat_req.session_id)
    history = session_ctx.messages
    session_id = session_ctx.session_id

    # 指代消解
    resolve_query = await session.resolve(chat_req.query, session_id)

    # 用户情绪
    sentiment = detect_sentiment(resolve_query, history=history)
    extra_prompt = build_escalation_prompt(sentiment)

    # 意图识别
    intent = await intent_router.route(resolve_query)
    context = ""
    if intent.target == "rag":
        docs = hybrid_search(resolve_query, table=intent.table)
        context = _build_context(docs)

    stream_res = {"answer": "", "total_steps": 0, "total_tokens": 0}
    last_entities: dict[str, str] = {}
    start_t = time.perf_counter()

    async def generate():
        # 先推一个 start 事件给前端，带 session_id
        yield f"data: {json.dumps({'event': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

        nonlocal last_entities

        if intent.target == "plan_execute":
            plan_agent = request.app.state.plan_execute_agent
            async for chunk in plan_agent.run_stream(
                resolve_query,
                history=history,
                scenario=intent.scenario,
            ):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                if chunk.get("event") == "done":
                    data = chunk.get("data", {})
                    stream_res["answer"] = data.get("answer", "")
                    stream_res["total_steps"] = len(data.get("plan", []))
                    stream_res["total_tokens"] = data.get("total_tokens", 0)

            await session.add_turn(
                session_id,
                resolve_query,
                LoopResult(
                    answer=stream_res["answer"],
                    total_steps=stream_res["total_steps"],
                    total_latency_ms=(time.perf_counter() - start_t) * 1000,
                    last_entities=last_entities,
                ),
            )
            return

        # 消费 agent 的消息流，逐个处理事件
        async for event in agent.run_stream(
            resolve_query,
            context=context,
            history=history,
            system_prompt_extra=extra_prompt,
        ):
            # event 可能的值：
            #   {"event": "thinking", ...}
            #   {"event": "tool_call", "name": "search_product", "args": {...}}
            #   {"event": "tool_result", ...}
            #   {"event": "token", "content": "..."}
            #   {"event": "error", "message": "..."}
            #   {"event": "done", "answer": "...", ...}
            if event["event"] == "tool_call":
                args = event.get("args", {})
                if "product_name" in args:
                    last_entities["product"] = str(args["product_name"])
                if "order_id" in args:
                    last_entities["order"] = str(args["order_id"])

            if event["event"] == "done":
                stream_res["answer"] = event.get("answer", "")
                stream_res["total_steps"] = event.get("total_steps", 0)

            # 把事件序列化成 SSE 格式推给前端
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        await session.add_turn(
            session_id,
            resolve_query,
            LoopResult(
                answer=stream_res["answer"],
                total_steps=stream_res["total_steps"],
                total_latency_ms=(time.perf_counter() - start_t) * 1000,
                last_entities=last_entities,
            ),
        )

    return StreamingResponse(content=generate(), media_type="text/event-stream")
