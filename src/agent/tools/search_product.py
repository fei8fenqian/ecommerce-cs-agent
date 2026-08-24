import logging
import re
from typing import Any

from agent.rag.retrieve import hybrid_search
from agent.tools_registry import BaseTool, ToolContext, ToolResult
from config import settings

logger = logging.getLogger(__name__)


class SearchProduct(BaseTool):
    @property
    def requires_tool_context(self) -> bool:
        return True

    @property
    def name(self) -> str:
        return "search_product"

    @property
    def description(self) -> str:
        return "搜索商品参数和知识库文档。当用户询问产品规格、选购建议、售后政策时使用。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户输入的搜索关键词"},
                "table": {
                    "type": "string",
                    "enum": ["laptop_products", "phone_products", "knowledge_chunks"],
                    "default": "laptop_products",
                    "description": """根据用户问题类型选择要查询的数据库表：
                    查笔记本参数→laptop_products，
                    查手机参数→phone_products，
                    查售后政策/使用指南→knowledge_chunks。""",
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "返回条数，默认5",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        table: str = "laptop_products",
        top_k: int = settings.retrieval_top_k,
        *,
        tool_context: ToolContext | None = None,
    ) -> ToolResult:
        try:
            candidates: list[dict] = await hybrid_search(query, table=table, where=None, top_k=top_k)
            if not candidates:
                return ToolResult(name=self.name, status="error", error="未找到相关内容")
            results: list[dict[str, Any]] = []
            for c in candidates:
                content = str(c.get("content") or "")
                if tool_context is not None and tool_context.role == "customer":
                    content = _customer_visible_content(content)
                results.append(
                    {
                        "title": c.get("title"),
                        "content": content[:200] + ("..." if len(content) > 200 else ""),
                        "score": c.get("score"),
                    }
                )

            return ToolResult(
                name=self.name,
                status="success",
                data={"count": len(candidates), "results": results},
            )
        except Exception:
            logger.error("search_product 检索失败")
            return ToolResult(name=self.name, status="error", error="检索失败")


def _customer_visible_content(content: str) -> str:
    """移除商品检索文本中的仓库和精确库存描述。"""
    return re.sub(r"(?:库存|仓库|[\u4e00-\u9fa5]+仓)[^。；\n]*[。；]?", "", content).strip()
