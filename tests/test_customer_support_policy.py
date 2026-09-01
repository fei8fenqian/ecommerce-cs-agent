"""客户售后动作闸门的回归测试：只验证动作与状态边界，不锁死文案。"""

import pytest

from service.customer_support_policy import CustomerSupportAction, decide_customer_support_action


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("我要申请退款", CustomerSupportAction.OFFER_REFUND_SELF_SERVICE),
        ("我哪知道订单号，反正最近那单想退", CustomerSupportAction.OFFER_REFUND_SELF_SERVICE),
        ("我要退款，请转人工", CustomerSupportAction.OFFER_REFUND_SELF_SERVICE),
        ("已经退了", CustomerSupportAction.SHOW_REFUND_PROGRESS),
        ("我都申请过退款了", CustomerSupportAction.SHOW_REFUND_PROGRESS),
        ("退款没到账", CustomerSupportAction.SHOW_REFUND_PROGRESS),
        ("咋还没退款呢", CustomerSupportAction.SHOW_REFUND_PROGRESS),
        ("退货已经寄出，退款到哪了", CustomerSupportAction.SHOW_REFUND_PROGRESS),
        ("你们已经寄出去了嘛", CustomerSupportAction.ASK_FOR_CLARIFICATION),
        ("退款失败", CustomerSupportAction.CREATE_TICKET),
        ("退款金额不对", CustomerSupportAction.CREATE_TICKET),
        ("支付重复扣款了", CustomerSupportAction.CREATE_TICKET),
        ("支付失败怎么回事", CustomerSupportAction.ASK_FOR_CLARIFICATION),
        ("付款失败，帮我看看", CustomerSupportAction.ASK_FOR_CLARIFICATION),
        ("电脑坏了，怎么办", CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING),
        ("我的设备开不了机，帮我看看", CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING),
        ("电脑冒烟了", CustomerSupportAction.CREATE_TICKET),
        ("我要投诉你们", CustomerSupportAction.CREATE_TICKET),
        ("这个事情你们看着办", CustomerSupportAction.ASK_FOR_CLARIFICATION),
    ],
)
def test_customer_ticket_proposal_is_gated_by_business_rules(query, expected):
    decision = decide_customer_support_action(
        intent_target="ticket",
        role="customer",
        query=query,
    )

    assert decision.action == expected


def test_refund_human_request_requires_one_guidance_turn_before_ticket_creation():
    first = decide_customer_support_action(
        intent_target="ticket",
        role="customer",
        query="我要退款，直接转人工",
    )
    assert first.action == CustomerSupportAction.OFFER_REFUND_SELF_SERVICE

    second = decide_customer_support_action(
        intent_target="ticket",
        role="customer",
        query="退款页面我不想弄，仍需人工",
        history=[{"role": "assistant", "content": first.answer}],
    )
    assert second.action == CustomerSupportAction.CREATE_TICKET


def test_device_repair_request_requires_troubleshooting_before_ticket_creation():
    first = decide_customer_support_action(
        intent_target="ticket",
        role="customer",
        query="我那个电脑开不了机，帮我转人工",
    )
    assert first.action == CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING

    second = decide_customer_support_action(
        intent_target="ticket",
        role="customer",
        query="还是不行，需要报修",
        history=[{"role": "assistant", "content": first.answer}],
    )
    assert second.action == CustomerSupportAction.CREATE_TICKET


def test_non_customer_and_non_ticket_proposals_cannot_trigger_customer_support_actions():
    assert (
        decide_customer_support_action(intent_target="ticket", role="agent", query="我要退款").action
        == CustomerSupportAction.CONTINUE
    )
    assert (
        decide_customer_support_action(intent_target="rag", role="customer", query="我要退款").action
        == CustomerSupportAction.CONTINUE
    )
