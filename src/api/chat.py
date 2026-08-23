import asyncio
import json
import logging
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.engines.loop import LoopResult
from agent.llm.sentiment import build_escalation_prompt, detect_sentiment
from agent.rag.retrieve import hybrid_search
from agent.tools_registry import ToolContext
from config import settings
from exceptions import DependencyUnavailableError, LLMError
from log_config import get_request_id

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

_STREAM_ERROR_MESSAGE = "智能服务暂时不可用，请稍后重试"


def _stream_error_event(request: Request) -> dict[str, str]:
    request_id = getattr(request.state, "request_id", None) or get_request_id()
    return {
        "event": "error",
        "code": "DEPENDENCY_UNAVAILABLE",
        "message": _STREAM_ERROR_MESSAGE,
        "request_id": request_id,
    }


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


def _entities_from_retrieval(table: str, docs: list[dict]) -> dict[str, str]:
    """把商品检索首选结果保存为下一轮可解析的会话事实。"""
    if table not in {"laptop_products", "phone_products"} or not docs:
        return {}
    title = str(docs[0].get("title") or "").strip()
    return {"product": title} if title else {}


def _should_rerank(query: str, table: str) -> bool:
    """仅在精排确实能改善答案时承担额外 CPU 延迟。

    商品单品咨询、参数查询和预算推荐首先要求快速出首字；向量检索与 BM25 融合
    已足够作为候选。明确比较多个商品时，才为笔记本/手机启用交叉编码精排。
    政策和组件类问题则保留精排，以降低把不相关依据带入回答的概率。
    """
    if table in {"laptop_products", "phone_products"}:
        comparison_markers = ("对比", "区别", "哪个好", "哪款", " versus ", " vs ", "和")
        normalized_query = f" {query.lower()} "
        return any(re.search(re.escape(marker), normalized_query) for marker in comparison_markers)
    return True


@chat_router.post("/chat", response_model=ChatResponse)
async def chat(chat_req: ChatRequest, request: Request):
    try:
        agent = request.app.state.agent
        session = request.app.state.session
        intent_router = request.app.state.intent_router
        user_id = request.state.user["id"]
        tool_context = ToolContext(user_id=user_id, role=request.state.user["role"])

        # 获取历史会话或创建新会话
        ctx = await session.get_or_create(chat_req.session_id, user_id)
        if ctx is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        # 判断指代词对应的实体
        resolved_query = await session.resolve(chat_req.query, ctx.session_id, user_id)

        # 用户情感判断
        sentiment = detect_sentiment(resolved_query, history=ctx.history)
        sentiment_ctx = build_escalation_prompt(sentiment)

        # 意图路由
        intent = await intent_router.route(resolved_query)

        if intent.target == "plan_execute":
            plan_agent = request.app.state.plan_execute_agent
            plan_state = await plan_agent.run(
                resolved_query,
                history=ctx.history,
                scenario=intent.scenario,
                tool_context=tool_context,
            )
            # plan_execute 不走 AgentLoop，手动记录到 session
            await session.add_turn_simple(ctx.session_id, user_id, chat_req.query, plan_state.get("answer", ""))
            return ChatResponse(
                answer=plan_state.get("answer", ""),
                session_id=ctx.session_id,
                total_steps=len(plan_state.get("plan", [])),
                total_tokens=plan_state.get("total_tokens", 0),
            )
        elif intent.target == "rag":
            docs = await hybrid_search(
                resolved_query,
                table=intent.table,
                use_rerank=_should_rerank(resolved_query, intent.table),
            )
            context = _build_context(docs)
            retrieved_entities = _entities_from_retrieval(intent.table, docs)
            loop_result = await agent.run(
                resolved_query,
                context=context,
                history=ctx.history,
                system_prompt_extra=sentiment_ctx,
                tool_context=tool_context,
            )
            loop_result.last_entities = {**retrieved_entities, **loop_result.last_entities}
        else:
            loop_result = await agent.run(
                resolved_query,
                history=ctx.history,
                system_prompt_extra=sentiment_ctx,
                tool_context=tool_context,
            )

        # 当前对话放入上下文ctx
        await session.add_turn(ctx.session_id, user_id, chat_req.query, loop_result)
        return ChatResponse(
            answer=loop_result.answer,
            session_id=ctx.session_id,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
        )
    except DependencyUnavailableError:
        raise
    except LLMError as e:
        _chat_logger.error(
            "LLM 调用失败: retry=%d status=%s reason=%s",
            e.retry_count,
            e.status_code,
            e.last_response,
        )
        raise DependencyUnavailableError("智能服务暂时不可用") from e
    except Exception:
        _chat_logger.error("chat 端点异常")
        raise


@chat_router.post("/chat/stream")
async def chat_stream(chat_req: ChatRequest, request: Request):
    last_entities: dict[str, str] = {}
    try:
        agent = request.app.state.agent
        session = request.app.state.session
        intent_router = request.app.state.intent_router
        user_id = request.state.user["id"]
        tool_context = ToolContext(user_id=user_id, role=request.state.user["role"])

        # 这些步骤发生在 StreamingResponse 创建前，失败时可以正常返回 HTTP 503。
        session_ctx = await session.get_or_create(chat_req.session_id, user_id)
        if session_ctx is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        history = session_ctx.history
        session_id = session_ctx.session_id
        resolve_query = await session.resolve(chat_req.query, session_id, user_id)
        sentiment = detect_sentiment(resolve_query, history=history)
        extra_prompt = build_escalation_prompt(sentiment)
        intent = await intent_router.route(resolve_query)
        context = ""
        if intent.target == "rag":
            docs = await hybrid_search(
                resolve_query,
                table=intent.table,
                use_rerank=_should_rerank(resolve_query, intent.table),
            )
            context = _build_context(docs)
            last_entities = _entities_from_retrieval(intent.table, docs)
    except DependencyUnavailableError:
        raise
    except LLMError as exc:
        raise DependencyUnavailableError("智能服务暂时不可用") from exc

    stream_res = {"answer": "", "total_steps": 0, "total_tokens": 0}
    start_t = time.perf_counter()

    async def generate():
        stream_completed = False
        nonlocal last_entities
        try:
            # 先推一个 start 事件给前端，带 session_id
            yield f"data: {json.dumps({'event': 'start', 'session_id': session_id}, ensure_ascii=False)}\n\n"

            if intent.target == "plan_execute":
                plan_agent = request.app.state.plan_execute_agent
                async for chunk in plan_agent.run_stream(
                    resolve_query,
                    history=history,
                    scenario=intent.scenario,
                    tool_context=tool_context,
                ):
                    if chunk.get("event") == "error":
                        yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
                        return

                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    if chunk.get("event") == "done":
                        data = chunk.get("data", {})
                        stream_res["answer"] = data.get("answer", "")
                        stream_res["total_steps"] = len(data.get("plan", []))
                        stream_res["total_tokens"] = data.get("total_tokens", 0)
                        stream_completed = True

                if stream_completed:
                    await session.add_turn(
                        session_id,
                        user_id,
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
                tool_context=tool_context,
            ):
                if event.get("event") == "error":
                    yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
                    return

                if event.get("event") == "tool_call":
                    args = event.get("args", {})
                    if "product_name" in args:
                        last_entities["product"] = str(args["product_name"])
                    if "order_id" in args:
                        last_entities["order"] = str(args["order_id"])

                if event.get("event") == "done":
                    stream_res["answer"] = event.get("answer", "")
                    stream_res["total_steps"] = event.get("total_steps", 0)
                    stream_completed = True

                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            if stream_completed:
                await session.add_turn(
                    session_id,
                    user_id,
                    resolve_query,
                    LoopResult(
                        answer=stream_res["answer"],
                        total_steps=stream_res["total_steps"],
                        total_latency_ms=(time.perf_counter() - start_t) * 1000,
                        last_entities=last_entities,
                    ),
                )
        except asyncio.CancelledError:
            raise
        except (DependencyUnavailableError, LLMError):
            yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"
        except Exception:
            _chat_logger.error("chat stream generation failed")
            yield f"data: {json.dumps(_stream_error_event(request), ensure_ascii=False)}\n\n"

    return StreamingResponse(content=generate(), media_type="text/event-stream")
