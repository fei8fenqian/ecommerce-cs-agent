"""多轮对话 Session 管理和指代消解。

会话数据持久化到 PostgreSQL，Redis 不再用于保存会话内容。
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import tiktoken

from config import settings
from store.session_store import (
    SessionMessage,
    SessionRecord,
    create_session,
)
from store.session_store import (
    append_messages as append_session_messages,
)
from store.session_store import (
    delete_session as delete_session_record,
)
from store.session_store import (
    get_messages as get_session_messages,
)
from store.session_store import (
    get_session as get_session_record,
)
from store.session_store import (
    list_sessions as list_session_records,
)

from ..engines.loop import LoopResult
from .resolve import resolve_pronouns

logger = logging.getLogger(__name__)

_ENCODER = tiktoken.get_encoding("cl100k_base")


@dataclass
class SessionContext:
    """单个会话的上下文。

    ``messages`` 保存从数据库读取的完整消息历史。
    ``history`` 是专门提供给 LLM 的、经过 token 裁剪的历史。
    """

    session_id: str
    title: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    last_entities: dict[str, str] = field(default_factory=dict)
    created_at: float = 0.0
    last_active: float = 0.0

    @property
    def history(self) -> list[dict[str, Any]]:
        """返回经过 token 裁剪、适合发送给 LLM 的消息历史。"""
        return _trim_history(
            self.messages,
            max_tokens=settings.history_max_tokens,
        )


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
        for msg in msgs:
            total += len(_ENCODER.encode(json.dumps(msg, ensure_ascii=False)))
        return total

    head = turns[:keep_head_turns]
    tail_candidates = turns[keep_head_turns:]
    head_tokens = sum(_count_tokens(turn) for turn in head)

    kept_tail: list[list[dict[str, Any]]] = []
    tail_tokens = 0
    for turn in reversed(tail_candidates):
        turn_tokens = _count_tokens(turn)
        if head_tokens + tail_tokens + turn_tokens > max_tokens:
            break
        kept_tail.insert(0, turn)
        tail_tokens += turn_tokens

    if not kept_tail:
        kept_tail = [tail_candidates[-1]]

    result = [msg for turn in head for msg in turn]
    result.extend(msg for turn in kept_tail for msg in turn)

    trimmed = len(messages) - len(result)
    if trimmed > 0:
        logger.info(
            "trimmed %d messages, kept %d turns",
            trimmed,
            len(head) + len(kept_tail),
        )
    return result


class SessionManager:
    """基于 PostgreSQL 的会话管理器。"""

    @staticmethod
    def _record_to_context(
        record: SessionRecord,
        stored_messages: list[SessionMessage],
    ) -> SessionContext:
        """把数据库记录转换成业务层的 SessionContext。"""
        return SessionContext(
            session_id=record["id"],
            title=record["title"],
            messages=[stored_message["payload"] for stored_message in stored_messages],
            last_entities=dict(record["last_entities"]),
            created_at=record["created_at"].timestamp(),
            last_active=record["last_active_at"].timestamp(),
        )

    async def _load_context(
        self,
        session_id: str,
        owner_user_id: int,
    ) -> SessionContext | None:
        """读取会话元数据和消息，并组装成 SessionContext。"""
        record = await get_session_record(session_id, owner_user_id)
        if record is None:
            return None

        # 当前 store 层最多返回 200 条消息；后续可改为支持 limit=None。
        stored_messages = await get_session_messages(
            session_id,
            owner_user_id,
            limit=200,
        )
        if stored_messages is None:
            return None

        return self._record_to_context(record, stored_messages)

    # -- Public API ---------------------------------------------------------

    async def get_or_create(
        self,
        session_id: str | None,
        owner_user_id: int,
    ) -> SessionContext | None:
        """获取已有会话或创建新会话。

        没有传 session_id 时创建新会话；指定了不存在或不属于当前用户的
        session_id 时返回 None。
        """
        if session_id is None:
            record = await create_session(owner_user_id)
            logger.info("Session created")
            return self._record_to_context(record, [])

        return await self._load_context(session_id, owner_user_id)

    async def get(
        self,
        session_id: str,
        owner_user_id: int,
    ) -> SessionContext | None:
        """读取当前用户的会话及其消息。"""
        return await self._load_context(session_id, owner_user_id)

    async def list_sessions(self, owner_user_id: int) -> list[dict[str, Any]]:
        """列出当前用户的会话，保持现有 API 的返回格式。"""
        records = await list_session_records(owner_user_id)
        return [
            {
                "session_id": record["id"],
                "title": record["title"],
                "created_at": record["created_at"].timestamp(),
                "last_active": record["last_active_at"].timestamp(),
                "message_count": record["message_count"],
            }
            for record in records
        ]

    async def delete(self, session_id: str, owner_user_id: int) -> bool:
        """删除当前用户的会话及其消息。"""
        return await delete_session_record(session_id, owner_user_id)

    async def resolve(
        self,
        query: str,
        session_id: str | None,
        owner_user_id: int,
    ) -> str:
        """根据当前用户会话中的实体进行指代消解。"""
        if session_id is None:
            return query

        record = await get_session_record(session_id, owner_user_id)
        if record is None:
            return query

        return resolve_pronouns(
            query=query,
            entities=record["last_entities"],
        )

    async def add_turn(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        result: LoopResult,
    ) -> None:
        """保存一轮完整的 Agent ReAct 对话。"""
        ctx = await self.get(session_id, owner_user_id)
        if ctx is None:
            return

        new_messages: list[dict[str, Any]] = [
            {"role": "user", "content": query},
        ]

        for step in result.steps:
            if not step.tool_calls:
                continue

            new_messages.append(
                {
                    "role": "assistant",
                    "content": step.thought,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": {
                                "name": tool_call.name,
                                "arguments": json.dumps(
                                    tool_call.arguments,
                                    ensure_ascii=False,
                                ),
                            },
                        }
                        for tool_call in step.tool_calls
                    ],
                }
            )

            for tool_call in step.tool_calls:
                new_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": step.observation or "",
                    }
                )

        new_messages.append({"role": "assistant", "content": result.answer})

        entities = dict(ctx.last_entities)
        entities.update(result.last_entities)
        title = query[:50] if not ctx.title else None

        await append_session_messages(
            session_id,
            owner_user_id,
            new_messages,
            title=title,
            last_entities=entities,
        )

        # 更新本次调用中的上下文；数据库中保存的仍然是完整消息。
        ctx.messages.extend(new_messages)
        ctx.last_entities = entities
        if title is not None:
            ctx.title = title
        ctx.last_active = time.time()

    async def add_turn_simple(
        self,
        session_id: str,
        owner_user_id: int,
        query: str,
        answer: str,
    ) -> None:
        """只保存 user 和 assistant 两条消息。"""
        ctx = await self.get(session_id, owner_user_id)
        if ctx is None:
            return

        new_messages = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": answer},
        ]
        title = query[:50] if not ctx.title else None

        await append_session_messages(
            session_id,
            owner_user_id,
            new_messages,
            title=title,
            last_entities=ctx.last_entities,
        )

        ctx.messages.extend(new_messages)
        if title is not None:
            ctx.title = title
        ctx.last_active = time.time()
