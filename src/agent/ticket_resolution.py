"""自主处理知识型客服工单的最小执行器。

它不是另一套 Agent 框架：复用现有 RAG、LLMClient 和 tickets/ticket_messages
作为任务队列与执行记录。它只生成有知识依据的客服文本，不能调用退款、支付或
订单写工具。
"""

import asyncio
import logging
from typing import Any, Protocol

from agent.rag.retrieve import hybrid_search
from log_config import redact_text
from store.ticket_store import (
    claim_next_ticket_for_ai,
    complete_ai_ticket,
    escalate_ai_ticket,
)

logger = logging.getLogger(__name__)

_ESCALATE_MARKER = "[ESCALATE]"
_NEED_MORE_DETAILS_REPLY = (
    "我已收到您的报修。当前信息还不足以准确判断故障原因，请补充设备型号、"
    "开机后是否有指示灯或异常声音，以及近期是否发生过摔落、进液或升级。"
    "我已将工单转给人工客服继续跟进。"
)
_AGENT_UNAVAILABLE_REPLY = (
    "我已收到您的问题，但智能诊断暂时不可用。工单已转给人工客服继续跟进；您也可以补充设备型号和故障现象，以便更快处理。"
)


class ChatClient(Protocol):
    """工单执行器需要的最小 LLM 能力。"""

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        """根据受控消息生成一次回复。"""
        ...


def _knowledge_context(documents: list[dict[str, Any]]) -> str:
    """将最多三条知识资料变成有长度上限的模型上下文。"""
    excerpts: list[str] = []
    for document in documents[:3]:
        content = str(document.get("content") or "").strip()
        if content:
            title = str(document.get("title") or "知识资料")
            excerpts.append(f"资料（{title}）：\n{content[:1200]}")
    return "\n\n".join(excerpts)


class TicketResolutionAgent:
    """领取一张工单并在有可靠知识时自动回复的业务 Agent。"""

    def __init__(self, llm_client: ChatClient, *, claim_timeout_seconds: int) -> None:
        self._llm_client = llm_client
        self._claim_timeout_seconds = claim_timeout_seconds

    async def process_next(self) -> bool:
        """处理队列中的一张工单。

        Returns:
            True 表示本轮领取到了工单（无论自动结案还是转人工）；False 表示队列为空。
        """
        ticket = await claim_next_ticket_for_ai(self._claim_timeout_seconds)
        if ticket is None:
            return False

        ticket_id = str(ticket["ticket_id"])
        try:
            # 工单正文是客户自由输入；只让脱敏副本进入检索和模型边界。
            safe_issue = redact_text(str(ticket["issue"]))
            documents = await hybrid_search(safe_issue, table="knowledge_chunks")
            context = _knowledge_context(documents)
            if not context:
                await escalate_ai_ticket(ticket_id, _NEED_MORE_DETAILS_REPLY)
                return True

            response = await self._llm_client.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "你是极客数码的自主工单处理 Agent。只根据给定知识资料回答客户问题。"
                            "不得编造订单、物流、支付、退款、维修结论或政策。"
                            "如果资料不足、问题涉及退款/支付/订单写入，或无法可靠处理，"
                            f"只输出 {_ESCALATE_MARKER}。"
                            "不要输出客户姓名、手机号、邮箱等个人信息。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"工单问题：\n{safe_issue}\n\n可用知识资料：\n{context}",
                    },
                ],
                temperature=0.0,
                max_tokens=500,
            )
            answer = str(getattr(response, "content", "") or "").strip()
            if not answer or _ESCALATE_MARKER in answer:
                await escalate_ai_ticket(ticket_id, _NEED_MORE_DETAILS_REPLY)
                return True

            await complete_ai_ticket(ticket_id, answer)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            # 依赖异常和未知模型异常都不能让工单无限重试或泄露给客户；但客户必须
            # 看得到 Agent 已接手及后续去向，不能只留下一个没有解释的状态。
            logger.warning("AI 工单处理失败，已转人工队列")
            await escalate_ai_ticket(ticket_id, _AGENT_UNAVAILABLE_REPLY)
            return True


class TicketResolutionWorker:
    """以固定间隔驱动 TicketResolutionAgent 的轻量后台循环。"""

    def __init__(self, agent: TicketResolutionAgent, *, interval_seconds: float) -> None:
        self._agent = agent
        self._interval_seconds = interval_seconds

    async def run(self) -> None:
        """持续处理工单；每轮最多领取一张，避免占满应用事件循环。"""
        while True:
            try:
                await self._agent.process_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("AI 工单 worker 本轮执行失败")
            finally:
                await asyncio.sleep(self._interval_seconds)
