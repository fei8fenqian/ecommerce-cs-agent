"""客服 Agent 的 canonical Goal taxonomy。

这是 Router、Control Plane 和 Intent Gold 共用的业务语言。一个 Goal 描述客户要解决
什么；Workflow 只描述系统目前如何取得事实和判定结果。因此 Workflow 不存在不表示
Goal 非法，反之也不能由任意 domain/operation 拼出一个新 Goal。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoalDefinition:
    domain: str
    operation: str
    description: str
    workflow_key: str | None = None

    @property
    def key(self) -> str:
        return f"{self.domain}.{self.operation}"


def _goal(
    domain: str,
    operation: str,
    description: str,
    workflow_key: str | None = None,
) -> GoalDefinition:
    return GoalDefinition(domain, operation, description, workflow_key)


# Keep this registry deliberately small and business-facing. It is not a tool registry or
# a generic workflow DSL: it is the shared vocabulary at the Router/Control-Plane boundary.
GOAL_DEFINITIONS: tuple[GoalDefinition, ...] = (
    _goal("general", "answer", "一般知识问答"),
    _goal("general", "execute", "兼容旧执行链的泛化请求"),
    _goal("general", "clarify", "当前目标无法安全确定，需要澄清"),
    _goal("product", "answer", "商品知识、价格或政策问答"),
    _goal("product", "search_product", "查找商品目录"),
    _goal("product", "build_pc", "装机/配件方案"),
    _goal("product", "device_troubleshooting", "设备故障排查"),
    _goal("product", "product_compatibility", "商品或配件兼容性"),
    _goal("order", "track_order", "查询订单状态", "delivery.track_order"),
    _goal("order", "execute", "兼容旧订单执行链"),
    _goal("delivery", "track_order", "查询配送或发货状态", "delivery.track_order"),
    _goal("delivery", "delivery_instruction", "修改配送指示"),
    _goal("delivery", "delivery_exception", "配送异常"),
    _goal("order_fulfillment", "partial_fulfillment", "拆单、部分发货或缺货处理"),
    _goal("inventory", "check_stock", "查询库存", "inventory.check_stock"),
    _goal("after_sales", "check_after_sales", "查询售后单状态", "after_sales.check_after_sales"),
    _goal("after_sales", "return_logistics", "查询退货或取件物流", "after_sales.return_logistics"),
    _goal(
        "after_sales",
        "after_sales_transition",
        "售后流程转换，例如换货改退货",
        "after_sales.after_sales_transition",
    ),
    _goal("after_sales", "exchange", "换货处理"),
    _goal("warranty", "repair", "报修或保修"),
    _goal("payment", "check_payment_status", "查询支付状态"),
    # Refund status-family definitions must remain distinct because their SOP/completion
    # criteria are distinct, even when they share an order/refund read chain.
    _goal(
        "refund",
        "status",
        "当前退款处于什么状态、是否成功或是否完成",
        "refund.refund_status",
    ),
    _goal(
        "refund",
        "expected_arrival",
        "退款资金何时到账；普通退款 ETA，不解释退货回仓是否已触发退款",
        "refund.expected_arrival",
    ),
    _goal(
        "refund",
        "processing_time",
        "退款申请、受理或审核阶段本身需要多久，不等同资金到账时间",
        "refund.processing_time",
    ),
    _goal(
        "refund",
        "anomaly",
        "退款失败、被拒、反复处理、取消或其他异常原因",
        "refund.anomaly",
    ),
    _goal(
        "refund",
        "delivery_after_refund",
        "退款后商品是否仍配送、是否签收或配送与退款交叉状态；不包括普通电话、客服联系或营销通知",
    ),
    _goal("refund", "destination", "退款退回哪个支付渠道或账户", "refund.destination"),
    _goal(
        "refund",
        "request",
        "成功发起退款，包括明确要求发起退款，或反馈退款申请入口不可用、无法发起但仍希望完成申请",
        "refund.request",
    ),
    _goal("refund", "cancel", "撤销或取消已申请的退款", "refund.cancel"),
    _goal("refund", "amount", "退款金额、部分退款、少退或金额不一致", "refund.refund_detail"),
    _goal("refund", "eligibility", "当前订单是否符合退款资格", "refund.eligibility"),
    _goal("refund", "procedure", "询问如何申请、在哪里申请、操作步骤或一般退款流程"),
    _goal("refund", "clarify", "退款话题存在但当前目标不明确"),
    _goal(
        "return",
        "refund_dependency",
        "退货、拒收、取件、回仓或收货审核是否/何时触发并推进退款",
        "return.refund_dependency",
    ),
    _goal("invoice", "invoice", "发票相关处理"),
    _goal("account", "execute", "账户相关兼容执行链"),
    _goal("human", "human_handoff", "人工客服介入"),
    _goal("installation", "installation", "安装服务"),
    _goal(
        "price_protection",
        "refund_status",
        "价保退款或价保处理状态",
        "price_protection.refund_status",
    ),
    _goal("price_protection", "price_protection", "价保申请或资格"),
    _goal("fulfillment", "execute", "履约兼容执行链"),
    # Existing persisted membership payloads use this pair. It is intentionally not an
    # alias of refund.request because the domain changes the business meaning.
    _goal("membership", "refund_request", "会员退款兼容请求"),
    _goal("membership", "request", "会员请求"),
)

GOALS_BY_KEY: dict[str, GoalDefinition] = {goal.key: goal for goal in GOAL_DEFINITIONS}

# Legacy values are accepted only at the boundary, then converted to a canonical pair.
# No new Router output, Gold label, or Workflow definition should use these keys.
LEGACY_GOAL_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("refund", "refund_status"): ("refund", "status"),
    ("refund", "query_refund_status"): ("refund", "status"),
    ("refund", "refund_detail"): ("refund", "amount"),
    ("refund", "refund_request"): ("refund", "request"),
}

# Historical annotation/model operation names that are safe to collapse into a current
# canonical operation. The modifier preserves the lost detail without expanding the
# production Goal taxonomy for every corpus-specific label.
LEGACY_OPERATION_ALIASES: dict[str, tuple[str, str]] = {
    "acknowledgement": ("status", "acknowledgement"),
    "available_resolutions": ("eligibility", "available_resolutions"),
    "decision_support": ("eligibility", "decision_support"),
    "eligibility_window": ("eligibility", "window"),
    "expedite": ("processing_time", "expedite"),
    "order_status": ("track_order", "order_status"),
    "pickup_fee": ("amount", "pickup_fee"),
    "partial": ("amount", "partial"),
    "refund_interaction": ("delivery_after_refund", "delivery_interaction"),
    "request_unavailable": ("request", "unavailable"),
    "self_pickup_after_refund": ("delivery_after_refund", "self_pickup"),
    "status_amount": ("amount", "status_with_amount"),
    "trigger_condition": ("refund_dependency", "trigger_condition"),
}


def canonicalize_goal(domain: str, operation: str) -> tuple[str, str]:
    """Return the canonical pair, preserving an unknown pair for caller-side rejection."""
    return LEGACY_GOAL_ALIASES.get((domain, operation), (domain, operation))


def get_goal_definition(domain: str, operation: str) -> GoalDefinition | None:
    domain, operation = canonicalize_goal(domain, operation)
    return GOALS_BY_KEY.get(f"{domain}.{operation}")


def is_canonical_goal(domain: str, operation: str) -> bool:
    return get_goal_definition(domain, operation) is not None


def workflow_key_for_goal(domain: str, operation: str) -> str | None:
    definition = get_goal_definition(domain, operation)
    return definition.workflow_key if definition is not None else None


REFUND_GOAL_ROUTING_GUIDANCE = """退款 Goal 必须按以下互斥语义选择，不能笼统输出“退款状态”：
- refund.status：问当前退款是否成功、当前进度、是否完成。
- refund.expected_arrival：问钱何时到账、多久到账；不是审核/受理耗时。
- refund.processing_time：问申请、审核或受理要多久；不是钱何时到账。
- refund.anomaly：退款失败、被拒、反复、取消或其他异常原因。
- refund.destination：问退款退到哪个支付渠道/账户。
- refund.amount：问退款金额、部分退款、少退或金额不一致。
- refund.eligibility：问当前订单能否退款或是否在退款期限内。
- refund.procedure：问如何申请、在哪里申请、操作步骤或一般退款流程。用户需要的是流程说明，
  不是要求系统实际发起退款，也不是要求恢复一个当前不可用的申请入口。
- refund.request：用户当前业务目标是成功发起退款。既可以是 ACTION_REQUEST，例如“帮我申请退款”；
  也可以是 INFORMATION_QUERY，例如明确反馈退款入口不可用、无法发起申请，但当前仍希望完成退款。
- refund.cancel：明确撤销已申请退款。
- refund.delivery_after_refund：问退款后商品是否继续配送、是否还要签收等交叉状态。普通电话、客服联系、
  营销通知或其他非物流后续交互，不能仅因发生在退款后就归入此 Goal。
- return.refund_dependency：问退货、拒收、取件、回仓或收货审核之后，退款是否/何时触发或推进。
  只有当前句明确提到该退货节点，或当前句是省略问法且紧邻上下文已明确该节点时才使用；
  不能仅因历史出现过“退货/仓库”就把当前明确的普通退款问题改成该 Goal。
- price_protection.refund_status：价保退款/价保处理进度。
after_sales.refund_status、after_sales.refund_request 等不是合法 Goal，绝不输出。"""
