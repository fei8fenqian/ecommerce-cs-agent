import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolResult
from config import settings
from core.retrieve import hybrid_search

logger = logging.getLogger(__name__)

CATEGORY_MAP = {
    "cpu": "cpu",
    "motherboard": "motherboard",
    "gpu": "vga",
    "ram": "memory",
    "ssd": "solid_state_drive",
    "hdd": "hard_drives",
    "psu": "power",
    "case": "case",
    "cooler": "cooling_product",
}


class SearchComponent(BaseTool):
    @property
    def name(self) -> str:
        return "search_component"

    @property
    def description(self) -> str:
        return "需要为用户检索电脑配件时或用户需要你帮忙配置一台完整的台式电脑时调用"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "用户输入的查询语句"},
                "component": {
                    "type": "string",
                    "description": "用户想要查询的电脑配件类型",
                    "enum": [
                        "cpu",
                        "motherboard",
                        "gpu",
                        "ram",
                        "ssd",
                        "hdd",
                        "psu",
                        "case",
                        "cooler",
                    ],
                },
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "description": "调用检索返回的条目数，默认5",
                },
            },
            "required": ["query", "component"],
        }

    def execute(self, query: str, component: str, top_k: int = settings.retrieval_top_k):
        try:
            table = "component_products"
            where = f"category='{CATEGORY_MAP[component]}'"
            raw_results = hybrid_search(query, table=table, where=where, top_k=top_k)
            results = []
            if not raw_results:
                return ToolResult(name=self.name, status="error", error="未找到相关内容")
            for r in raw_results:
                results.append(
                    {
                        "title": r.get("title"),
                        "content": r.get("content", "")[:200]
                        + ("..." if len(r.get("content", "")) > 200 else ""),
                        "score": r.get("score"),
                    }
                )

            return ToolResult(
                name=self.name,
                status="success",
                data={"count": len(raw_results), "results": results},
            )
        except Exception as e:
            logger.error(
                "search_component 检索失败: query=%s table=%s error=%s",
                query,
                table,
                str(e),
            )
            return ToolResult(name=self.name, status="error", error=f"检索失败: {str(e)}")
