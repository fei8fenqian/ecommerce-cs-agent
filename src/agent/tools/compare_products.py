import logging
from typing import Any

from agent.tools_registry import BaseTool, ToolResult
from core.retrieve import hybrid_search

logger = logging.getLogger(__name__)


class CompareProducts(BaseTool):
    @property
    def name(self) -> str:
        return "compare_products"

    @property
    def description(self) -> str:
        return (
            "对比两款商品的关键参数差异。当用户问'A和B有什么区别''哪个更好'"
            "'这个和那个对比'时使用。需要提供两款产品的名称。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product_a": {"type": "string", "description": "用户提到的第一个商品"},
                "product_b": {"type": "string", "description": "用户提到的第二个商品"},
                "table": {
                    "type": "string",
                    "description": """根据用户问题类型选择要查询的数据库表：
                    比较笔记本时→laptop_products，
                    比较手机时→phone_products，""",
                    "enum": ["laptop_products", "phone_products"],
                    "default": "laptop_products",
                },
            },
            "required": ["product_a", "product_b"],
        }

    def execute(
        self, product_a: str = "", product_b: str = "", table: str = "laptop_products"
    ) -> ToolResult:
        if not product_a or not product_b:
            return ToolResult(name=self.name, status="error", error="需要提供两款产品的名称")

        if table not in ("laptop_products", "phone_products"):
            return ToolResult(name=self.name, status="error", error=f"不支持的产品类别: {table}")
        try:
            results_a = hybrid_search(query=product_a, table=table, where=None, top_k=3)
            results_b = hybrid_search(query=product_b, table=table, where=None, top_k=3)

            if not results_a:
                return ToolResult(
                    name=self.name,
                    status="error",
                    error=f"未找到 {product_a} 信息，无法比较",
                )
            if not results_b:
                return ToolResult(
                    name=self.name,
                    status="error",
                    error=f"未找到 {product_b} 信息，无法比较",
                )

            # 取 Top-1
            return ToolResult(
                name=self.name,
                status="success",
                data={
                    "product_a": {
                        "title": results_a[0].get("title", ""),
                        "content": results_a[0].get("content", "")[:300],
                        "score": results_a[0].get("score", 0.0),
                        "source": results_a[0].get("source", ""),
                    },
                    "product_b": {
                        "title": results_b[0].get("title", ""),
                        "content": results_b[0].get("content", "")[:300],
                        "score": results_b[0].get("score", 0.0),
                        "source": results_b[0].get("source", ""),
                    },
                },
            )
        except Exception as e:
            logger.error(
                "compare_products 失败: a=%s b=%s table=%s error=%s",
                product_a,
                product_b,
                table,
                str(e),
            )
            return ToolResult(name=self.name, status="error", error=f"商品对比失败: {str(e)}")
