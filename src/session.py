"""src/session.py — 多轮对话 Session 管理 + 指代消解

职责：
1. 对话历史管理 — 每轮 messages 存入 SessionContext，下一轮传给 AgentLoop
2. 指代消解 — 规则替换 "它/这个/那台" 为上一轮识别的实体名

企业场景用规则做指代消解，比 LLM 快且确定。
未来换 Redis：改 _sessions 存储后端，接口不动。
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent.loop import LoopResult

logger = logging.getLogger(__name__)

# 指代词 → entity key 映射
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
    """用上一轮识别的实体替换指代词，保留原句结构。

    "它的价格呢" + {product: "拯救者Y9000P"} → "拯救者Y9000P的价格呢"
    """
    if not entities:
        return query
    for pronoun, key in PRONOUN_MAP.items():
        entity = entities.get(key, "")
        if entity and pronoun in query:
            query = query.replace(pronoun, entity)

    return query


# SessionContext
@dataclass
class SessionContext:
    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_entities: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    last_active: float = 0.0


# SessionManager
class SessionManager:
    """管理所有会话的创建、更新、过期清理。Phase 4 用内存 dict，后续换
    Redis。"""

    def __init__(self, ttl: int = 1800):
        self._sessions: dict[str, SessionContext] = {}
        self._ttl = ttl

    # -- CRUD
    async def get_or_create(self, session_id: str | None = None) -> SessionContext:
        """创建或获取会话上下文"""
        now = time.time()
        if session_id and session_id in self._sessions:
            ctx = self._sessions[session_id]
            ctx.last_active = now
            return ctx

        sid = session_id or str(uuid.uuid4())
        ctx = SessionContext(session_id=sid, created_at=now, last_active=now)
        self._sessions[sid] = ctx
        logger.info("Session created: %s", sid)
        return ctx

    async def add_turn(self, session_id: str, query: str, result: LoopResult) -> None:
        """把本轮对话产生的消息追加进 history。"""
        ctx = self._sessions.get(session_id)
        # 会话不存在
        if ctx is None:
            logger.warning("Session not found: %s, dropping turn", session_id)
            return

        # 用户输入消息
        ctx.messages.append({"role": "user", "content": query})

        # agent响应消息
        for step in result.steps:
            if step.tool_calls:
                ctx.messages.append(
                    {
                        "role": "assistant",
                        "content": step.thought,
                        "tool_calls": step.tool_calls,
                    }
                )
                ctx.messages.append(
                    {
                        "role": "tool",
                        "content": step.observation or "",
                    }
                )

        # 最终回答
        ctx.messages.append({"role": "tool", "content": result.answer})

        # 更新entity
        # 把新实体的键值对合并进 ctx。相同的 key 覆盖，新的 key 新增。
        if result.last_entities:
            ctx.last_entities.update(result.last_entities)

        ctx.last_active = time.time()

    async def resolve(self, query: str, session_id: str | None = None) -> str:
        """对当前 query 做指代消解。"""

        # 没传 session_id（新用户、第一轮）
        if session_id is None:
            return query
        ctx = self._sessions.get(session_id)

        # 传了 session_id，但找不到了（过期/清理）
        if ctx is None:
            return query
        return resolve_pronouns(query=query, entities=ctx.last_entities)

    async def cleanup_expired(self) -> int:
        """清理过期 session，返回清理数量。"""
        now = time.time()
        expired = []
        for sid, ctx in self._sessions.items():
            if now - ctx.last_active > self._ttl:
                expired.append(sid)
        for sid in expired:
            del self._sessions[sid]
        if expired:
            logger.info("Cleaned %d expired sessions", len(expired))
        return len(expired)
