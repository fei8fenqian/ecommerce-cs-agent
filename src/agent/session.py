"""src/session.py — 多轮对话 Session 管理 + 指代消解

职责：
1. 对话历史管理 — 每轮 messages 存入 SessionContext，下一轮传给 AgentLoop
2. 指代消解 — 规则替换 "它/这个/那台" 为上一轮识别的实体名

规则做指代消解，比 LLM 快且确定。
存储后端：Redis（带 TTL 自动过期），async 非阻塞。
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as redis
import tiktoken

from config import settings

from .loop import LoopResult

logger = logging.getLogger(__name__)

_ENCODER = tiktoken.get_encoding("cl100k_base")

PRONOUN_MAP: dict[str, str] = {
    "它": "product",
    "他": "product",
    "这个": "product",
    "这台": "product",
    "那台": "product",
    "这款": "product",
    "该商品": "product",
    "该产品": "product",
    "这单": "order",
    "那个订单": "order",
    "该订单": "order",
}


def resolve_pronouns(query: str, entities: dict[str, str]) -> str:
    """用上一轮识别的实体替换指代词。"""
    if not entities:
        return query
    for pronoun, key in PRONOUN_MAP.items():
        entity = entities.get(key, "")
        if entity and pronoun in query:
            query = query.replace(pronoun, entity)
    return query


@dataclass
class SessionContext:
    session_id: str
    title: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_entities: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    last_active: float = 0.0


def _trim_history(
    messages: list[dict[str, Any]],
    max_tokens: int = 8000,
    keep_head_turns: int = 1,
) -> list[dict[str, Any]]:
    """按 token 截断对话历史，不拆散完整轮次。"""
    if not messages:
        return messages

    turns: list[list[dict[str, Any]]] = []
    current_turn: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "user" and current_turn:
            turns.append(current_turn)
            current_turn = []
        current_turn.append(msg)
    if current_turn:
        turns.append(current_turn)

    if len(turns) <= keep_head_turns + 1:
        return messages

    def _count_tokens(msgs: list[dict[str, Any]]) -> int:
        total = 0
        for m in msgs:
            total += len(_ENCODER.encode(json.dumps(m, ensure_ascii=False)))
        return total

    head = turns[:keep_head_turns]
    tail_candidates = turns[keep_head_turns:]
    head_tokens = sum(_count_tokens(t) for t in head)

    kept_tail: list[list[dict[str, Any]]] = []
    tail_tokens = 0
    for turn in reversed(tail_candidates):
        t = _count_tokens(turn)
        if head_tokens + tail_tokens + t > max_tokens:
            break
        kept_tail.insert(0, turn)
        tail_tokens += t

    if not kept_tail:
        kept_tail = [tail_candidates[-1]]

    result = [m for t in head for m in t] + [m for t in kept_tail for m in t]
    trimmed = len(messages) - len(result)
    if trimmed > 0:
        logger.info("trimmed %d messages, kept %d turns", trimmed, len(head) + len(kept_tail))
    return result


def _decode(val: str | bytes | None) -> str:
    """安全解码 Redis 返回的 bytes/str。"""
    if val is None:
        return ""
    return val.decode() if isinstance(val, bytes) else val


class SessionManager:
    """Redis 后端会话管理，async 非阻塞，TTL 自动过期。"""

    def __init__(self, redis_url: str | None = None, ttl: int | None = None):
        self._redis = redis.from_url(redis_url or settings.redis_url)
        self._ttl = ttl or settings.session_ttl

    async def health_check(self) -> bool:
        """启动时检查 Redis 连通性。失败不阻塞启动，只打日志。"""
        try:
            await self._redis.ping()
            logger.info("Redis 连接成功: %s", self._redis.connection_pool.connection_kwargs.get("host", "?"))
            return True
        except Exception as e:
            logger.warning("Redis 连接失败: %s，会话功能将不可用", e)
            return False

    async def close(self) -> None:
        """关闭 Redis 连接池。"""
        await self._redis.aclose()

    # -- Public API --

    async def get_or_create(self, session_id: str | None = None) -> SessionContext:
        now = time.time()
        if session_id:
            data = await self._redis.hgetall(self._key(session_id))
            if data:
                return await self._from_hash(session_id, data)

        sid = session_id or str(uuid.uuid4())
        ctx = SessionContext(session_id=sid, created_at=now, last_active=now)
        await self._save(sid, ctx)
        logger.info("Session created: %s", sid)
        return ctx

    async def add_turn(self, session_id: str, query: str, result: LoopResult) -> None:
        ctx = await self._load(session_id)
        if ctx is None:
            return

        if not ctx.title:
            ctx.title = query[:50]

        ctx.messages.append({"role": "user", "content": query})

        for step in result.steps:
            if step.tool_calls:
                ctx.messages.append(
                    {
                        "role": "assistant",
                        "content": step.thought,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                                },
                            }
                            for tc in step.tool_calls
                        ],
                    }
                )
                for tc in step.tool_calls:
                    ctx.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": step.observation or "",
                        }
                    )

        ctx.messages.append({"role": "assistant", "content": result.answer})

        if result.last_entities:
            ctx.last_entities.update(result.last_entities)

        ctx.messages = _trim_history(ctx.messages, max_tokens=settings.history_max_tokens)
        ctx.last_active = time.time()
        await self._save(session_id, ctx)

    async def add_turn_simple(self, session_id: str, query: str, answer: str) -> None:
        ctx = await self._load(session_id)
        if ctx is None:
            return

        if not ctx.title:
            ctx.title = query[:50]

        ctx.messages.append({"role": "user", "content": query})
        ctx.messages.append({"role": "assistant", "content": answer})
        ctx.messages = _trim_history(ctx.messages, max_tokens=settings.history_max_tokens)
        ctx.last_active = time.time()
        await self._save(session_id, ctx)

    async def resolve(self, query: str, session_id: str | None = None) -> str:
        if session_id is None:
            return query
        ctx = await self._load(session_id)
        if ctx is None:
            return query
        return resolve_pronouns(query=query, entities=ctx.last_entities)

    async def list_sessions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for key in self._redis.scan_iter(match="session:*"):  # type: ignore[attr-defined]
            # 只取主 key，跳过 messages 子 key
            if b":messages" in key:
                continue
            sid = key.decode().split(":", 1)[1]
            data = await self._redis.hgetall(key)
            msg_count = await self._redis.llen(self._key(sid, "messages"))
            result.append(
                {
                    "session_id": sid,
                    "title": _decode(data.get(b"title")),
                    "created_at": float(data.get(b"created_at", 0)),
                    "last_active": float(data.get(b"last_active", 0)),
                    "message_count": msg_count,
                }
            )
        result.sort(key=lambda s: s["last_active"], reverse=True)
        return result

    # -- Internal --

    @staticmethod
    def _key(session_id: str, suffix: str = "") -> str:
        key = f"session:{session_id}"
        return f"{key}:{suffix}" if suffix else key

    async def _load(self, session_id: str) -> SessionContext | None:
        data = await self._redis.hgetall(self._key(session_id))
        if not data:
            return None
        return await self._from_hash(session_id, data)

    async def _from_hash(self, session_id: str, data: dict[str | bytes, str | bytes]) -> SessionContext:
        msgs_raw = await self._redis.lrange(self._key(session_id, "messages"), 0, -1)
        messages = [json.loads(m) for m in msgs_raw] if msgs_raw else []
        entities_raw = data.get(b"last_entities", b"{}")
        return SessionContext(
            session_id=session_id,
            title=_decode(data.get(b"title")),
            messages=messages,
            last_entities=json.loads(entities_raw) if entities_raw else {},
            created_at=float(data.get(b"created_at", 0)),
            last_active=float(data.get(b"last_active", 0)),
        )

    async def _save(self, session_id: str, ctx: SessionContext) -> None:
        key = self._key(session_id)
        pipe = self._redis.pipeline()
        pipe.hset(
            key,
            mapping={
                "title": ctx.title,
                "created_at": str(ctx.created_at),
                "last_active": str(ctx.last_active),
                "last_entities": json.dumps(ctx.last_entities, ensure_ascii=False),
            },
        )
        pipe.expire(key, self._ttl)
        msg_key = self._key(session_id, "messages")
        pipe.delete(msg_key)
        for msg in ctx.messages:
            pipe.rpush(msg_key, json.dumps(msg, ensure_ascii=False))
        pipe.expire(msg_key, self._ttl)
        await pipe.execute()
