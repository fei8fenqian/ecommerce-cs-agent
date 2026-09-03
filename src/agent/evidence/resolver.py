"""将已识别的业务 Goal 映射为证据需求。

这里不判断客户当前订单事实，也不选择具体 Tool。它只表达一个 canonical Goal
是否需要知识库、商品目录或实时业务事实，具体的事实读取仍由 SupportWorkflow /
Control Plane 负责。
"""

from dataclasses import dataclass
from typing import Literal

KnowledgeNeed = Literal["none", "optional", "required"]


@dataclass(frozen=True)
class EvidencePlan:
    """一次请求需要的证据类型，而不是执行路由。"""

    knowledge: KnowledgeNeed = "none"
    catalog: bool = False
    live_facts: bool = False

    @property
    def needs_deep_knowledge(self) -> bool:
        return self.knowledge == "required"


# 只收录当前已稳定的 canonical Goal。没有列出的目标走保守默认值，不用 LLM
# 反过来决定是否读取当前订单、退款或库存事实。
_EVIDENCE_BY_ROUTE: dict[tuple[str, str], EvidencePlan] = {
    ("refund", "status"): EvidencePlan(live_facts=True),
    ("refund", "amount"): EvidencePlan(live_facts=True),
    ("refund", "eligibility"): EvidencePlan(live_facts=True),
    ("refund", "request"): EvidencePlan(knowledge="optional", live_facts=True),
    ("refund", "cancel"): EvidencePlan(knowledge="optional", live_facts=True),
    # 这些目标同时需要当前业务状态和受控的规则/SLA 说明。知识不是当前退款事实。
    ("refund", "expected_arrival"): EvidencePlan(knowledge="required", live_facts=True),
    ("refund", "processing_time"): EvidencePlan(knowledge="required", live_facts=True),
    ("refund", "anomaly"): EvidencePlan(knowledge="required", live_facts=True),
    ("refund", "destination"): EvidencePlan(knowledge="required", live_facts=True),
    ("return", "refund_dependency"): EvidencePlan(knowledge="required", live_facts=True),
    ("price_protection", "refund_status"): EvidencePlan(knowledge="required", live_facts=True),
    ("product", "device_troubleshooting"): EvidencePlan(knowledge="required"),
    ("product", "product_compatibility"): EvidencePlan(knowledge="optional", catalog=True),
    ("product", "search_product"): EvidencePlan(catalog=True),
    ("product", "purchase"): EvidencePlan(catalog=True),
}


def resolve_evidence(
    *,
    domain: str,
    operation: str,
    target: str = "",
    table: str = "",
) -> EvidencePlan:
    """返回 Goal 的确定性证据需求。

    ``target`` / ``table`` 仅为尚未迁完的通用咨询兼容入口：它们不能覆盖一个已知
    的业务 Goal，也不参与 SupportWorkflow 的事实/能力决策。
    """

    route_plan = _EVIDENCE_BY_ROUTE.get((domain, operation))
    if route_plan is not None:
        return route_plan

    if table in {"laptop_products", "phone_products", "component_products"}:
        return EvidencePlan(catalog=True)
    if table == "knowledge_chunks" or target == "rag":
        return EvidencePlan(knowledge="required")
    return EvidencePlan()
