import json
import logging
import sys

import pytest

from log_config import (
    REDACTED,
    JSONFormatter,
    _RequestIDFilter,
    get_request_id,
    get_span_id,
    get_trace_id,
    reset_request_context,
    set_request_context,
)


def format_record(message: str, extra: dict | None = None, exc_info=None) -> dict:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )
    record.request_id = "req-test"
    record.trace_id = "trace-test"
    record.span_id = "span-test"
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return json.loads(JSONFormatter().format(record))


def test_sensitive_structured_fields_are_redacted():
    payload = format_record(
        "login failed",
        {
            "password": "secret123",
            "password_hash": "hash-value",
            "token": "token-value",
            "Authorization": "Bearer auth-value",
            "cookie": "session=cookie-value",
            "api_key": "sk-test-secret",
            "secret": "secret-value",
            "payment_signature": "signature-value",
            "phone": "13800138000",
            "email": "user@example.com",
            "address": "北京市朝阳区某某路",
            "evidence": "完整证据内容",
            "prompt": "完整系统提示词",
            "query": "完整用户查询",
        },
    )

    extra = payload["extra"]
    for key in (
        "password",
        "password_hash",
        "Authorization",
        "cookie",
        "api_key",
        "secret",
        "payment_signature",
        "phone",
        "email",
        "address",
        "evidence",
        "prompt",
        "query",
    ):
        assert extra[key] == REDACTED
    assert extra["token"] == REDACTED
    assert "secret123" not in json.dumps(payload)
    assert "user@example.com" not in json.dumps(payload)


def test_nested_structured_values_are_redacted_recursively():
    payload = format_record(
        "diagnostic",
        {
            "diagnostic": {
                "status_code": 200,
                "duration_ms": 12.5,
                "token": "nested-token",
                "user": {
                    "password": "nested-password",
                    "email": "nested@example.com",
                    "attempt": 2,
                },
            }
        },
    )

    diagnostic = payload["extra"]["diagnostic"]
    assert diagnostic["status_code"] == 200
    assert diagnostic["duration_ms"] == 12.5
    assert diagnostic["token"] == REDACTED
    assert diagnostic["user"] == {
        "password": REDACTED,
        "email": REDACTED,
        "attempt": 2,
    }


def test_additional_pii_fields_are_redacted_without_losing_diagnostics():
    payload = format_record(
        "ticket",
        {
            "username": "alice",
            "customer_name": "张三",
            "issue": "用户描述的问题",
            "reason": "退款原因",
            "token_count": 3,
            "query_duration_ms": 12.5,
            "email_retry_count": 2,
        },
    )

    extra = payload["extra"]
    assert extra["username"] == REDACTED
    assert extra["customer_name"] == REDACTED
    assert extra["issue"] == REDACTED
    assert extra["reason"] == REDACTED
    assert extra["token_count"] == 3
    assert extra["query_duration_ms"] == 12.5
    assert extra["email_retry_count"] == 2


def test_message_sensitive_values_are_redacted():
    payload = format_record(
        "password=secret token=abc Authorization: Bearer bearer-value "
        "email=user@example.com phone=13800138000 api_key=sk-live-secret "
        "username=alice customer_name=张三 reason=地址写错了 issue=设备无法开机"
    )

    message = payload["msg"]
    assert "secret" not in message
    assert "abc" not in message
    assert "bearer-value" not in message
    assert "user@example.com" not in message
    assert "13800138000" not in message
    assert "sk-live-secret" not in message
    assert "alice" not in message
    assert "张三" not in message
    assert "地址写错了" not in message
    assert "设备无法开机" not in message


def test_exception_text_is_redacted():
    try:
        raise RuntimeError("database password=secret token=abc email=user@example.com")
    except RuntimeError:
        exc_info = sys.exc_info()

    payload = format_record("request failed", exc_info=exc_info)

    assert "password=secret" not in payload["exc"]
    assert "token=abc" not in payload["exc"]
    assert "user@example.com" not in payload["exc"]
    assert REDACTED in payload["exc"]


@pytest.mark.asyncio
async def test_context_fields_are_preserved_and_not_redacted():
    tokens = set_request_context(
        request_id="req-123",
        trace_id="trace-123",
        span_id="span-123",
        traceparent="00-trace-123-span-123-01",
    )
    try:
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            "request completed",
            (),
            None,
        )
        _RequestIDFilter().filter(record)
        payload = json.loads(JSONFormatter().format(record))
    finally:
        reset_request_context(tokens)

    assert payload["rid"] == "req-123"
    assert payload["request_id"] == "req-123"
    assert payload["trace_id"] == "trace-123"
    assert payload["span_id"] == "span-123"
    assert get_request_id() == "-"
    assert get_trace_id() == "-"
    assert get_span_id() == "-"
