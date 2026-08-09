import logging
from typing import Any

from agent.rag.retrieve import hybrid_search
from agent.tools.tools_registry import BaseTool, ToolResult
from config import settings

logger = logging.getLogger(__name__)


class SearchProduct(BaseTool):
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
    ) -> ToolResult:
        try:
            candidates: list[dict] = await hybrid_search(query, table=table, where=None, top_k=top_k)
            if not candidates:
                return ToolResult(name=self.name, status="error", error="未找到相关内容")
            results: list[dict[str, Any]] = []
            for c in candidates:
                results.append(
                    {
                        "title": c.get("title"),
                        "content": (c.get("content") or "")[:200]
                        + ("..." if len(c.get("content") or "") > 200 else ""),
                        "score": c.get("score"),
                    }
                )

            return ToolResult(
                name=self.name,
                status="success",
                data={"count": len(candidates), "results": results},
            )
        except Exception as e:
            logger.error(
                "search_product 检索失败: query=%s table=%s error=%s",
                query,
                table,
                str(e),
            )
            return ToolResult(name=self.name, status="error", error=f"检索失败: {str(e)}")
