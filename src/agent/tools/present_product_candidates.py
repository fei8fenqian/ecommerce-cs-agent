"""Structured presentation declaration for server-owned product candidates.

This tool has no business side effects.  It gives the Operator LLM a structured
way to declare which opaque candidate refs it is about to show to the customer
and in what order.  The server validates membership against the current
Ecommerce candidate frame; canonical product ids never enter model arguments.
"""

from __future__ import annotations

from typing import Any

from agent.tools_registry import BaseTool, ToolContext, ToolResult


class PresentProductCandidates(BaseTool):
    @property
    def name(self) -> str:
        return "present_product_candidates"

    @property
    def description(self) -> str:
        return (
            "在商品对话中，当你准备向客户展示一个有顺序的真实商品列表时调用。"
            "mode=recommend 表示你自主推荐这些候选；mode=choice 表示这些候选需要客户进一步选择。"
            "candidate_refs 必须来自当前 Ecommerce Role 提供的 candidate_N，并严格按你随后展示给客户的顺序提交。"
            "这不会替客户选择商品、不会下单，也不会修改任何订单/支付/退款状态。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["recommend", "choice"],
                    "description": "recommend=推荐列表；choice=需要客户选择的候选列表",
                },
                "candidate_refs": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^candidate_[1-9][0-9]*$"},
                    "minItems": 1,
                    "maxItems": 12,
                    "uniqueItems": True,
                    "description": "按最终展示顺序排列的当前候选引用",
                },
            },
            "required": ["mode", "candidate_refs"],
            "additionalProperties": False,
        }

    @property
    def requires_tool_context(self) -> bool:
        return True

    async def execute(
        self,
        *,
        mode: str,
        candidate_refs: list[str],
        tool_context: ToolContext,
    ) -> ToolResult:
        if mode not in {"recommend", "choice"}:
            return ToolResult(name=self.name, status="error", error="无效的商品展示模式")
        if not isinstance(candidate_refs, list) or not 1 <= len(candidate_refs) <= 12:
            return ToolResult(name=self.name, status="error", error="商品候选数量必须为 1-12")
        refs = [str(ref).strip() for ref in candidate_refs]
        if len(set(refs)) != len(refs):
            return ToolResult(name=self.name, status="error", error="商品候选不能重复")
        allowed = tool_context.product_candidate_refs
        if not allowed or any(ref not in allowed for ref in refs):
            return ToolResult(name=self.name, status="error", error="商品候选不属于当前服务端候选帧")
        return ToolResult(
            name=self.name,
            status="success",
            data={"mode": mode, "candidate_refs": refs},
        )
