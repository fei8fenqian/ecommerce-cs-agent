from agent.llm.support_command import build_support_command_input, interpretation_log_payload
from agent.support_command_contract import parse_support_command_turn


def test_parse_support_command_correction():
    turn = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "set_subject",
                    "domain": "",
                    "operation": "",
                    "subject_description": "华为手机",
                    "candidate_ref": "",
                    "confidence": 0.98,
                }
            ],
        }
    )

    assert turn.scope == "supported"
    assert len(turn.commands) == 1
    command = turn.commands[0]
    assert command.type == "set_subject"
    assert command.goal == ""
    assert command.subject_description == "华为手机"


def test_parse_support_command_keeps_only_safe_candidate_refs():
    safe = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "set_subject",
                    "domain": "",
                    "operation": "",
                    "candidate_ref": "choice_2",
                    "confidence": 1,
                }
            ],
        }
    )
    unsafe = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "set_subject",
                    "domain": "",
                    "operation": "",
                    "candidate_ref": "SO20260904054350E55F18C893D0",
                    "confidence": 1,
                }
            ],
        }
    )

    assert safe.commands[0].candidate_ref == "choice_2"
    assert unsafe.commands[0].candidate_ref == ""


def test_parse_support_command_does_not_widen_phase2_goal_scope():
    turn = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "start_goal",
                    "domain": "payment",
                    "operation": "check_payment_status",
                    "subject_description": "刚才那笔",
                    "candidate_ref": "",
                    "confidence": 0.9,
                }
            ],
        }
    )

    assert turn.commands[0].goal == ""
    assert turn.commands[0].subject_description == "刚才那笔"


def test_set_subject_cannot_carry_a_second_goal_authority():
    turn = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "set_subject",
                    "domain": "order",
                    "operation": "status",
                    "subject_description": "华为",
                }
            ],
        }
    )

    assert turn.commands[0].type == "set_subject"
    assert turn.commands[0].goal == ""
    assert turn.commands[0].subject_description == "华为"


def test_parse_support_command_accepts_only_ephemeral_current_subject_ref():
    current = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "start_goal",
                    "domain": "refund",
                    "operation": "status",
                    "candidate_ref": "current_subject",
                }
            ],
        }
    )
    real_id = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "start_goal",
                    "domain": "refund",
                    "operation": "status",
                    "candidate_ref": "SO20260904054350E55F18C893D0",
                }
            ],
        }
    )

    assert current.commands[0].candidate_ref == "current_subject"
    assert real_id.commands[0].candidate_ref == ""


def test_support_command_input_only_uses_visible_history_and_masks_assistant_order_refs():
    prompt = build_support_command_input(
        "搞错了，我想退华为",
        history=[
            {"role": "assistant", "content": "请选择 SO20260904054350E55F18C893D0"},
            {"role": "tool", "content": "internal secret"},
            {"role": "user", "content": "上一句话"},
        ],
        case_context={"status": "AWAITING_CUSTOMER", "pending": {"kind": "customer_choice"}},
    )

    assert "SO20260904054350E55F18C893D0" not in prompt
    assert "[ORDER_REF]" in prompt
    assert "internal secret" not in prompt
    assert "customer_choice" in prompt
    assert "搞错了，我想退华为" in prompt


def test_interpretation_log_payload_masks_order_refs_in_subject_summary():
    turn = parse_support_command_turn(
        {
            "scope": "supported",
            "commands": [
                {
                    "type": "start_goal",
                    "domain": "order",
                    "operation": "status",
                    "subject_description": "订单 SO20260904054350E55F18C893D0",
                    "candidate_ref": "",
                    "confidence": 0.8,
                }
            ],
        }
    )

    payload = interpretation_log_payload(turn)
    assert payload["subject_summaries"] == ["订单 [ORDER_REF]"]
    assert payload["goals"] == ["order.status"]


def test_interpreter_requests_provider_json_mode_without_tools():
    import asyncio

    from agent.llm.support_command import SupportCommandInterpreter

    class FakeLLM:
        def __init__(self):
            self.kwargs = None

        async def chat(self, messages, **kwargs):
            self.kwargs = kwargs

            class Response:
                content = '{"scope":"supported","commands":[{"type":"set_subject","domain":"","operation":"","subject_description":"华为","candidate_ref":"","confidence":0.99}]}'

            return Response()

    fake = FakeLLM()
    interpreter = SupportCommandInterpreter(fake)
    turn = asyncio.run(interpreter.interpret("搞错了，我想退华为"))

    assert turn.commands[0].type == "set_subject"
    assert fake.kwargs["response_format"] == {"type": "json_object"}
    assert fake.kwargs["temperature"] == 0.0
    assert fake.kwargs["max_tokens"] == 512
    assert "tools" not in fake.kwargs
