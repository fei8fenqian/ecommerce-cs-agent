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
    """单个会话的完整上下文。

    Redis 持久化策略：
    - 元数据（除 messages 外）→ session:{id} hash
    - messages                → session:{id}:messages list（每条 JSON 序列化）
    """

    session_id: str  # UUID4 唯一标识
    title: str = ""  # 会话标题，取自首条 query 前 50 字符
    messages: list[dict[str, Any]] = field(default_factory=list)  # OpenAI 格式对话历史
    last_entities: dict[str, str] = field(default_factory=dict)  # {"product": "拯救者Y9000P", "order": "xxx"}
    created_at: float = 0.0  # 创建时间戳
    last_active: float = 0.0  # 最后活跃时间戳


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
    """Redis 后端会话管理，async 非阻塞，TTL 自动过期。

    Redis 数据结构：
      session:{id}          → hash   (title, created_at, last_active, last_entities)
      session:{id}:messages → list   (每条消息 JSON 序列化后 push 到尾部)
    两个 key 都设 TTL（session_ttl），过期自动清理，无需手动 GC。
    """

    def __init__(self, redis_url: str | None = None, ttl: int | None = None):
        """
        Args:
            redis_url: Redis 连接串，默认取 settings.redis_url
                      格式 redis://host:port/db
            ttl: 会话过期时间（秒），默认取 settings.session_ttl
                 过期后 Redis 自动删除，无需手动清理
        """
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
        """获取已有会话或创建新会话。

        逻辑：
        1. 传了 session_id → 查 Redis session:{id} hash
           - 存在 → 反序列化返回（恢复历史对话）
           - 不存在 → 当新会话处理
        2. 没传 session_id → 生成 UUID4 新 ID，写 Redis

        Returns:
            SessionContext: 包含完整 messages 列表的会话对象
        """
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
        """把一轮 Agent ReAct 对话写入 Redis。

        写入顺序（和 OpenAI API 多轮格式一致）：
        1. user message       — 用户原始 query
        2. assistant message  — LLM 的 thought + tool_calls 数组（如果有）
        3. tool result messages — 每个工具调用一条，role="tool"
        4. assistant message  — 最终给用户的文本回答

        写完后：
        - 更新 last_entities（供下一轮的指代消解使用）
        - 调 _trim_history 截断超过 max_tokens 的历史
        - 重新设 TTL（续期）
        """
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
        """轻量版 add_turn — 只存 user + assistant 两条消息。

        用于 Plan-and-Execute 场景：中间步骤复杂（多个子任务、依赖传参），
        展开存储会极大膨胀 messages 体积，只存最终问答对即可。
        """
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
        """对 query 做指代消解后返回。

        从对话上下文中取出上一轮识别到的实体（产品名/订单号），
        替换 query 中的指代词（"它""这单"等）。没有历史则原样返回。
        """
        if session_id is None:
            return query
        ctx = await self._load(session_id)
        if ctx is None:
            return query
        return resolve_pronouns(query=query, entities=ctx.last_entities)

    async def list_sessions(self) -> list[dict[str, Any]]:
        """列出 Redis 中所有活跃会话（供中台列表页使用）。

        用 SCAN 遍历 session:* 前缀的 key，跳过 :messages 子 key，
        读取每个会话的元数据 hash，按 last_active 倒序排列。

        Returns:
            [{"session_id", "title", "created_at", "last_active", "message_count"}, ...]
        """
        result: list[dict[str, Any]] = []
        async for key in self._redis.scan_iter(match="session:*"):  # type: ignore[attr-defined]
            # 只取主 hash key（session:{id}），跳过 messages list 子 key
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
        """生成 Redis key。

        - 主 key: session:{id}             → hash (元数据)
        - 子 key: session:{id}:messages    → list (对话历史)
        """
        key = f"session:{session_id}"
        return f"{key}:{suffix}" if suffix else key

    async def _load(self, session_id: str) -> SessionContext | None:
        """从 Redis 加载单个会话。不存在返回 None。"""
        data = await self._redis.hgetall(self._key(session_id))
        if not data:
            return None
        return await self._from_hash(session_id, data)

    async def _from_hash(self, session_id: str, data: dict[str | bytes, str | bytes]) -> SessionContext:
        """将 Redis 原始 bytes/str 数据反序列化为 SessionContext。

        Redis hash 字段都是 bytes/str，时间戳存的是 float 的字符串表示，
        last_entities 是 JSON 字符串，messages 从独立的 list key 读取。
        """
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
        """将 SessionContext 全量写入 Redis。

        使用 pipeline 批量执行，减少网络往返：
        1. HSET session:{id}      — 元数据 hash
        2. EXPIRE session:{id}    — 续 TTL
        3. DEL session:{id}:messages — 清空旧消息列表
        4. RPUSH messages 逐条写入 — 顺序追加
        5. EXPIRE session:{id}:messages — 续 TTL
        """
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
