import logging
import re
import time
import uuid
from dataclasses import dataclass

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from log_config import reset_request_context, set_request_context

logger = logging.getLogger(__name__)

_REQUEST_ID_MAX_LENGTH = 128
_TRACEPARENT_RE = re.compile(
    r"(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<trace_flags>[0-9a-f]{2})"
)


@dataclass(frozen=True)
class TraceParent:
    trace_id: str
    trace_flags: str


def _is_valid_request_id(value: str | None) -> bool:
    """只接受非空、无空白/控制字符且不超过 128 字符的请求 ID。"""
    if not value or len(value) > _REQUEST_ID_MAX_LENGTH:
        return False
    return all(0x21 <= ord(character) <= 0x7E for character in value)


def _new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def _new_trace_id() -> str:
    return uuid.uuid4().hex


def _new_span_id() -> str:
    return uuid.uuid4().hex[:16]


def parse_traceparent(value: str | None) -> TraceParent | None:
    """解析当前支持的 W3C traceparent v00 格式，非法值返回 None。"""
    if value is None:
        return None
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None or match.group("version") != "00":
        return None
    if match.group("trace_id") == "0" * 32:
        return None
    if match.group("parent_id") == "0" * 16:
        return None
    return TraceParent(
        trace_id=match.group("trace_id"),
        trace_flags=match.group("trace_flags"),
    )


def build_traceparent(trace_id: str, span_id: str, trace_flags: str = "00") -> str:
    """生成当前服务向下游传播的 W3C traceparent。"""
    return f"00-{trace_id}-{span_id}-{trace_flags}"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t_start = time.perf_counter()
        incoming_request_id = request.headers.get("X-Request-ID")
        request_id = incoming_request_id if _is_valid_request_id(incoming_request_id) else _new_request_id()

        incoming_trace = parse_traceparent(request.headers.get("traceparent"))
        trace_id = incoming_trace.trace_id if incoming_trace else _new_trace_id()
        trace_flags = incoming_trace.trace_flags if incoming_trace else "00"
        span_id = _new_span_id()
        traceparent = build_traceparent(trace_id, span_id, trace_flags)

        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request.state.span_id = span_id
        request.state.traceparent = traceparent

        context_tokens = set_request_context(
            request_id=request_id,
            trace_id=trace_id,
            span_id=span_id,
            traceparent=traceparent,
        )
        try:
            response: Response = await call_next(request)

            total_time = (time.perf_counter() - t_start) * 1000
            response.headers["X-Request-ID"] = request_id
            # 保留项目原有兼容头；标准链路上下文使用 traceparent。
            response.headers["X-Trace-ID"] = trace_id
            response.headers["traceparent"] = traceparent
            response.headers["X-Response-Time-ms"] = f"{total_time:.1f}"
            return response
        finally:
            reset_request_context(context_tokens)
