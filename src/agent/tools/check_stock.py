import logging
from typing import Any

import psycopg2

from agent.tools_registry import BaseTool, ToolResult
from config import settings

logger = logging.getLogger(__name__)


class CheckStock(BaseTool):
    @property
    def name(self) -> str:
        return "check_stock"

    @property
    def description(self) -> str:
        return (
            "查询商品库存和仓库信息。用户询问'有没有货''多久到'还有多少台'时使用。"
            "**一次查询返回所有匹配子型号的库存**，一次调用即包含该产品线的全部变体，"
            "不需要按具体型号逐个重复查询。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "商品名称"},
                "table": {
                    "type": "string",
                    "enum": ["laptop_products", "phone_products"],
                    "description": "笔记本电脑填 laptop_products，手机填 phone_products",
                    "default": "laptop_products",
                },
            },
            "required": ["product_name"],
        }

    def execute(self, product_name: str, table: str = "laptop_products") -> ToolResult:
        # 表名白名单，防注入
        if table not in ("laptop_products", "phone_products"):
            return ToolResult(name=self.name, status="error", error=f"不支持的表: {table}")

        try:
            conn = psycopg2.connect(
                host=settings.pg_host,
                port=settings.pg_port,
                user=settings.pg_user,
                password=settings.pg_password.get_secret_value(),
                dbname=settings.pg_dbname,
            )
            cur = conn.cursor()
            cur.execute(
                f"SELECT product_name, brand, price, stock, warehouse "
                f"FROM {table} WHERE product_name ILIKE %s LIMIT 10",
                (f"%{product_name}%",),
            )
            rows = cur.fetchall()

            if not rows:
                return ToolResult(
                    name=self.name, status="error", error=f"未找到 {product_name} 的库存信息"
                )

            results: list[dict[str, Any]] = []
            for name, brand, price, stock, warehouse in rows:
                results.append(
                    {
                        "name": name,
                        "brand": brand,
                        "price": float(price) if price else 0.0,
                        "stock": stock,
                        "warehouse": warehouse,
                    }
                )

            return ToolResult(
                name=self.name,
                status="success",
                data={"count": len(results), "results": results},
            )

        except Exception as e:
            logger.error(
                "check_stock 查询失败: name=%s table=%s error=%s",
                product_name,
                table,
                str(e),
            )
            return ToolResult(name=self.name, status="error", error=f"库存查询失败: {str(e)}")

        finally:
            if "cur" in locals():
                cur.close()
            if "conn" in locals():
                conn.close()
