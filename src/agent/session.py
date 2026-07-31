"""src/session.py — 多轮对话 Session 管理 + 指代消解

职责：
1. 对话历史管理 — 每轮 messages 存入 SessionContext，下一轮传给 AgentLoop
2. 指代消解 — 规则替换 "它/这个/那台" 为上一轮识别的实体名

企业场景用规则做指代消解，比 LLM 快且确定。
未来换 Redis：改 _sessions 存储后端，接口不动。
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import tiktoken

from config import settings
from .loop import LoopResult

logger = logging.getLogger(__name__)

# DeepSeek tokenizer 和 GPT-4 的 cl100k_base 接近，用作截断估算
_ENCODER = tiktoken.get_encoding("cl100k_base")

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
def _trim_history(
    messages: list[dict[str, Any]],
    max_tokens: int = 8000,
    keep_head_turns: int = 1,
) -> list[dict[str, Any]]:
    """按 token 截断对话历史，不拆散完整轮次。

    1. 用 tiktoken 算每条消息的 token 数
    2. 按 role=="user" 把 messages 切分成轮次列表
    3. 从最后一轮往前累加 token，超出 max_tokens 停
    4. 保留 开头 keep_head_turns 轮 + 最近能装下的轮次
    5. 没超出则不裁
    """
    if not messages:
        return messages

    # ---- 切分轮次：每个 user 消息是一轮起点 ----
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
        return messages  # 轮次还不多，不裁

    # ---- 计算每轮 token 数 ----
    def _count_tokens(msgs: list[dict[str, Any]]) -> int:
        total = 0
        for m in msgs:
            total += len(_ENCODER.encode(json.dumps(m, ensure_ascii=False)))
        return total

    head = turns[:keep_head_turns]
    tail_candidates = turns[keep_head_turns:]
    head_tokens = sum(_count_tokens(t) for t in head)

    # ---- 倒序累计，装得下的保留 ----
    kept_tail: list[list[dict[str, Any]]] = []
    tail_tokens = 0
    for turn in reversed(tail_candidates):
        t = _count_tokens(turn)
        if head_tokens + tail_tokens + t > max_tokens:
            break
        kept_tail.insert(0, turn)
        tail_tokens += t

    if not kept_tail:
        # 连一轮都装不下 → 至少保留最后一轮（兜底）
        kept_tail = [tail_candidates[-1]]

    result = [m for t in head for m in t] + [m for t in kept_tail for m in t]
    trimmed = len(messages) - len(result)
    if trimmed > 0:
        logger.info("trimmed %d messages (%d tokens), kept %d turns", trimmed, head_tokens + tail_tokens, len(head) + len(kept_tail))
    return result


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

        # 最终回答
        ctx.messages.append({"role": "assistant", "content": result.answer})

        # 更新entity
        # 把新实体的键值对合并进 ctx。相同的 key 覆盖，新的 key 新增。
        if result.last_entities:
            ctx.last_entities.update(result.last_entities)

        ctx.messages = _trim_history(ctx.messages, max_tokens=settings.history_max_tokens)
        ctx.last_active = time.time()

    async def add_turn_simple(self, session_id: str, query: str, answer: str) -> None:
        """plan_execute 等不回 LoopResult 的场景，只追加 user+assistant 两条。"""
        ctx = self._sessions.get(session_id)
        if ctx is None:
            return
        ctx.messages.append({"role": "user", "content": query})
        ctx.messages.append({"role": "assistant", "content": answer})
        ctx.messages = _trim_history(ctx.messages, max_tokens=settings.history_max_tokens)
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
