import json
import logging
from dataclasses import dataclass
from typing import Any

from core.llm_client import LLMClient, LLMResponse

"""用一次轻量 LLM 调用给 query 分类，决定走 RAG 还是 Agent Loop"""
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个意图分类器。分析用户问题，返回 JSON。

分类规则：
- rag: 参数查询、选购建议、售后政策(退货条件/保修范围/换货规则)、使用指南（无需实时数据）
- agent: 库存查询、订单追踪（需要调用工具获取实时数据）
- ticket: 投诉、退款赔偿（用户要"退钱"不是"问退货规则"）、情绪激动骂人、明确要求转人工

关键区别：用户问"退货什么流程/什么条件"→ rag；用户说"我要退款/我要投诉"→ ticket

返回格式（只返回 JSON，不要其他文字。不要照抄示例的 confidence 值）：
{"target": "rag", "table": "laptop_products", "confidence": 0.98}

table 规则（仅 rag 有效，agent 和 ticket 填空字符串即可）：
- 笔记本参数/选购 → laptop_products
- 手机参数/选购 → phone_products
- 政策/指南/使用说明 → knowledge_chunks
- agent 或 ticket → table 填空字符串 ""

confidence 规则：
- 明确能分类的 → 0.9-1.0
- 模糊或不确定 → 0.5-0.8
"""


@dataclass
class Intent:
    target: str = ""  # "rag" | "agent" | "ticket"
    table: str = ""  # 仅 RAG 需要："laptop_products" | "phone_products" | "knowledge_chunks"
    query: str = ""  # 透传原始问题
    confidence: float = 0.0  # 0.0 - 1.0，LLM 返回的置信度（用于降级）


class IntentRouter:
    """用一次轻量 LLM 调用给 query 分类，决定走 RAG 还是 Agent Loop"""

    def __init__(self, llm: LLMClient):
        self.llm = llm
        self.system_prompt = SYSTEM_PROMPT

    async def route(self, query: str = "") -> Intent:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        messages.append({"role": "user", "content": query})

        try:
            response: LLMResponse = await self.llm.chat(messages, temperature=0.0, max_tokens=256)

            answer: str = response.content or ""

            # 提取 LLM 返回的 JSON（可能被 markdown 包裹）
            answer = answer.strip()
            if answer.startswith("```"):
                # 去掉 ```json 和结尾 ```
                lines = answer.split("\n")
                answer = "\n".join(lines[1:-1]) if len(lines) >= 3 else answer

            result = json.loads(answer)
            target = result.get("target", "rag").strip().lower()
            table = result.get("table", "").strip()
            confidence = float(result.get("confidence", 0.0))
            # 校验 target
            if target not in ("rag", "agent", "ticket"):
                target = "rag"

            # 校验 table：仅 rag 需要，agent/ticket 强制置空
            if target != "rag":
                table = ""
            elif table not in ("laptop_products", "phone_products", "knowledge_chunks"):
                table = "knowledge_chunks"

            # 低置信度 → 降级走 RAG
            if confidence < 0.5:
                logger.info("意图分类置信度低 (%.2f)，降级为 RAG", confidence)
                target = "rag"
                table = "knowledge_chunks"

            return Intent(target=target, table=table, query=query, confidence=confidence)

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning("意图分类解析失败: %s，降级为 RAG", str(e))
            return Intent(target="rag", table="knowledge_chunks", query=query, confidence=0.0)
