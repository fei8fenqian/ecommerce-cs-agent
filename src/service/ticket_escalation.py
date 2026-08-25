"""工单 Agent 的确定性人工升级原因。

模型可以生成回复草稿，但不能自行扩大或缩小人工升级边界。分类结果同时用于
worker 的控制流、日志和人工升级记录的受控原因码。
"""

from enum import StrEnum

from agent.llm.sentiment import detect_sentiment


class TicketEscalationReason(StrEnum):
    """一张工单进入人工队列的受控原因。"""

    EXPLICIT_HUMAN_REQUEST = "EXPLICIT_HUMAN_REQUEST"
    COMPLAINT_OR_DISPUTE = "COMPLAINT_OR_DISPUTE"
    ORDER_OR_PAYMENT_ACTION = "ORDER_OR_PAYMENT_ACTION"
    KNOWLEDGE_UNAVAILABLE = "KNOWLEDGE_UNAVAILABLE"
    MODEL_ESCALATION = "MODEL_ESCALATION"
    REPEATED_UNRESOLVED_FOLLOW_UP = "REPEATED_UNRESOLVED_FOLLOW_UP"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"


_EXPLICIT_HUMAN_MARKERS = (
    "人工客服",
    "转人工",
    "转接人工",
    "真人客服",
    "找人工",
    "人工处理",
    "人工介入",
)

_ORDER_OR_PAYMENT_MARKERS = (
    "退款",
    "退货",
    "赔偿",
    "支付",
    "付款",
    "扣款",
    "取消订单",
    "修改订单",
    "改地址",
    "修改地址",
    "更换收货地址",
    "支付密码",
)


def classify_ticket_escalation(
    issue: str,
    *,
    previous_ai_reply: bool = False,
    knowledge_available: bool = True,
) -> TicketEscalationReason | None:
    """根据工单事实判断是否必须进入人工队列。

    Args:
        issue: 当前客户工单问题；调用方应先传入脱敏文本。
        previous_ai_reply: 该工单此前是否已经收到过 AI 回复。
        knowledge_available: 当前检索是否有可引用知识资料。

    Returns:
        必须升级时返回受控原因；低风险且资料充分时返回 ``None``。
    """
    normalized = issue.strip()
    if any(marker in normalized for marker in _EXPLICIT_HUMAN_MARKERS):
        return TicketEscalationReason.EXPLICIT_HUMAN_REQUEST
    if any(marker in normalized for marker in _ORDER_OR_PAYMENT_MARKERS):
        return TicketEscalationReason.ORDER_OR_PAYMENT_ACTION

    sentiment = detect_sentiment(normalized)
    if sentiment.should_escalate:
        return TicketEscalationReason.COMPLAINT_OR_DISPUTE
    if not knowledge_available:
        return (
            TicketEscalationReason.REPEATED_UNRESOLVED_FOLLOW_UP
            if previous_ai_reply
            else TicketEscalationReason.KNOWLEDGE_UNAVAILABLE
        )
    return None
