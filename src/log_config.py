"""
结构化日志模块
==============
把日志从"给人看的多行文本"变成"给机器看的单行 JSON"。
每条日志带 request_id，出问题时能把一次请求的所有日志串起来。
"""

import contextvars
import json
import logging
import re
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

# ContextVar：每个 asyncio Task 有独立的副本，请求间互不干扰
_request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")
_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("span_id", default="-")
_traceparent_var: contextvars.ContextVar[str] = contextvars.ContextVar("traceparent", default="-")

REDACTED = "[REDACTED]"

_SENSITIVE_KEY_NAMES = frozenset(
    {
        ("password",),
        ("password", "hash"),
        ("passwd",),
        ("token",),
        ("access", "token"),
        ("refresh", "token"),
        ("authorization",),
        ("cookie",),
        ("api", "key"),
        ("secret",),
        ("client", "secret"),
        ("signature",),
        ("payment", "signature"),
        ("phone",),
        ("email",),
        ("address",),
        ("evidence",),
        ("prompt",),
        ("query",),
        ("username",),
        ("customer", "name"),
        ("issue",),
        ("reason",),
        ("payload",),
        ("body",),
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    ("password",),
    ("password", "hash"),
    ("token",),
    ("api", "key"),
    ("secret",),
    ("signature",),
)

_SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<label>\b(?:password(?:[_ -]?hash)?|passwd|"
    r"access[_ -]?token|refresh[_ -]?token|token|authorization|cookie|"
    r"api[_ -]?key|client[_ -]?secret|secret|payment[_ -]?(?:signature|sign)|"
    r"signature|phone|email)\b\s*[:=]\s*)"
    r"(?:(?:Bearer|Basic)\s+)?(?P<value>[^\s,;]+)"
)
_CONTENT_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<label>\b(?:evidence|prompt|query|address|username|"
    r"customer[_ -]?name|issue|reason|payload|body)\b\s*[:=]\s*)"
    r"(?P<value>.*?)(?=(?:\s+\b(?:password|token|authorization|cookie|"
    r"api[_ -]?key|secret|signature|phone|email|address|username|"
    r"customer[_ -]?name|issue|reason|payload|body|status[_ -]?code|"
    r"duration[_ -]?ms)\b\s*[:=])|$)"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_API_KEY_RE = re.compile(r"\b(?:sk|pk)[-_][A-Za-z0-9_-]{8,}\b")

_STANDARD_LOG_RECORD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)
_CONTEXT_LOG_FIELDS = frozenset({"request_id", "trace_id", "span_id"})


def set_request_id(request_id: str) -> None:
    """middleware 调这个，把 request_id 注入当前协程"""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    """任何下游代码调这个，拿到当前请求的 request_id"""
    return _request_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """设置当前请求的 W3C trace_id。"""
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    """获取当前请求的 W3C trace_id。"""
    return _trace_id_var.get()


def get_span_id() -> str:
    """获取当前请求生成的 span_id。"""
    return _span_id_var.get()


def get_traceparent() -> str:
    """获取当前请求生成的 traceparent，可用于下游调用传播。"""
    return _traceparent_var.get()


def get_trace_headers() -> dict[str, str]:
    """生成下游 HTTP 调用应携带的标准链路请求头。"""
    traceparent = get_traceparent()
    return {} if traceparent == "-" else {"traceparent": traceparent}


def get_trace_metadata() -> dict[str, str]:
    """生成异步任务或消息可携带的链路元数据。"""
    return {
        "trace_id": get_trace_id(),
        "span_id": get_span_id(),
        "traceparent": get_traceparent(),
    }


def set_request_context(
    request_id: str,
    trace_id: str,
    span_id: str,
    traceparent: str,
) -> tuple[
    contextvars.Token[str],
    contextvars.Token[str],
    contextvars.Token[str],
    contextvars.Token[str],
]:
    """设置一次请求的上下文，并返回稍后恢复上下文所需的 token。"""
    return (
        _request_id_var.set(request_id),
        _trace_id_var.set(trace_id),
        _span_id_var.set(span_id),
        _traceparent_var.set(traceparent),
    )


def reset_request_context(
    tokens: tuple[
        contextvars.Token[str],
        contextvars.Token[str],
        contextvars.Token[str],
        contextvars.Token[str],
    ],
) -> None:
    """恢复请求进入前的上下文，避免后续请求继承本次请求的数据。"""
    request_token, trace_token, span_token, traceparent_token = tokens
    _traceparent_var.reset(traceparent_token)
    _span_id_var.reset(span_token)
    _trace_id_var.reset(trace_token)
    _request_id_var.reset(request_token)


def _is_sensitive_key(key: object) -> bool:
    text = str(key).strip()
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text).lower()
    parts = tuple(part for part in re.split(r"[^a-z0-9]+", text) if part)
    if parts in _SENSITIVE_KEY_NAMES:
        return True
    return any(len(parts) >= len(suffix) and parts[-len(suffix) :] == suffix for suffix in _SENSITIVE_KEY_SUFFIXES)


def redact_text(text: str) -> str:
    """脱敏日志文本中的常见敏感键值、邮箱、手机号和 API key。"""

    def replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group('label')}{REDACTED}"

    text = _CONTENT_ASSIGNMENT_RE.sub(replace_assignment, text)
    text = _SENSITIVE_ASSIGNMENT_RE.sub(replace_assignment, text)
    text = _EMAIL_RE.sub(REDACTED, text)
    text = _PHONE_RE.sub(REDACTED, text)
    return _API_KEY_RE.sub(REDACTED, text)


def redact_value(value: Any, key: object | None = None) -> Any:
    """递归脱敏结构化日志值，敏感键对应的整个值直接隐藏。"""
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(child_key): redact_value(child_value, child_key) for child_key, child_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _record_extra(record: logging.LogRecord) -> dict[str, Any]:
    """提取 logging.extra，排除 LogRecord 内置字段和上下文重复字段。"""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_LOG_RECORD_FIELDS and key not in _CONTEXT_LOG_FIELDS
    }


# Filter：每条日志输出前自动把 contextvar 里的 request_id 塞到 record 上
class _RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        record.trace_id = _trace_id_var.get()
        record.span_id = _span_id_var.get()
        return True


# JSON Formatter：把日志格式化成单行 JSON
class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "func": record.funcName,
            "logger": record.name,
            "rid": getattr(record, "request_id", "-"),
            "request_id": getattr(record, "request_id", "-"),
            "trace_id": getattr(record, "trace_id", "-"),
            "span_id": getattr(record, "span_id", "-"),
            "msg": redact_text(record.getMessage()),
        }
        extra = _record_extra(record)
        if extra:
            entry["extra"] = redact_value(extra)
        if record.exc_info and record.exc_info[0]:
            entry["exc"] = redact_text(self.formatException(record.exc_info))

        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    """应用启动时调用，配置全局 JSON 日志。"""

    root = logging.getLogger()
    root.setLevel(level)

    # 清掉旧 handler，避免 uvicorn reload 时重复输出
    for h in root.handlers[:]:
        root.removeHandler(h)

    # stdout：终端实时查看
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestIDFilter())
    root.addHandler(handler)

    # 文件：持久化，每天一个文件，保留 30 天
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(JSONFormatter())
    file_handler.addFilter(_RequestIDFilter())
    root.addHandler(file_handler)

    # 抑制第三方库的 DEBUG 日志噪音
    for noisy in (
        "chromadb",
        "httpx",
        "sentence_transformers",
        "urllib3",
        "psycopg",
        "watchfiles",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
