"""情绪升级边界：退款咨询不是投诉，明确维权才进入升级提示。"""

from agent.llm.sentiment import detect_sentiment


def test_refund_policy_question_does_not_force_human_escalation():
    result = detect_sentiment("退款条件是什么")

    assert result.is_negative is False
    assert result.should_escalate is False


def test_explicit_complaint_forces_human_escalation():
    result = detect_sentiment("我要投诉你们，准备打12315")

    assert result.is_negative is True
    assert result.should_escalate is True
