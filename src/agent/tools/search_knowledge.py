"""受 runtime manifest 约束的只读知识检索工具。"""

import logging
from typing import Any

from agent.rag.retrieve import hybrid_search
from agent.tools_registry import BaseTool, ToolResult
from config import settings

logger = logging.getLogger(__name__)


class SearchKnowledge(BaseTool):
    """让 Agent 在已有 Pre/Deep-RAG 不足时补查一般知识，而非业务实时事实。"""

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "搜索已审核的产品指南、售后政策和流程说明。仅用于一般知识；不能查询当前订单、退款、支付、库存或售后单状态。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要补充核验的知识问题或关键词"},
                "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 10},
            },
            "required": ["query"],
        }

    async def execute(self, query: str, top_k: int = settings.retrieval_top_k) -> ToolResult:
        if not query.strip():
            return ToolResult(name=self.name, status="error", error="需要提供知识查询关键词")
        safe_top_k = max(1, min(int(top_k), 10))
        try:
            candidates = await hybrid_search(
                query,
                table="knowledge_chunks",
                where=None,
                top_k=safe_top_k,
                use_rerank=True,
            )
        except Exception:
            logger.exception("search_knowledge 检索失败")
            return ToolResult(name=self.name, status="error", error="知识检索暂时不可用")

        if not candidates:
            return ToolResult(name=self.name, status="error", error="未找到已审核的相关知识")
        return ToolResult(
            name=self.name,
            status="success",
            data={
                "count": len(candidates),
                "results": [
                    {
                        "title": candidate.get("title"),
                        "source": candidate.get("source"),
                        "content": str(candidate.get("content") or "")[:500],
                        "score": candidate.get("score"),
                    }
                    for candidate in candidates
                ],
            },
        )
