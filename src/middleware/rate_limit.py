"""登录和聊天入口的 Redis 限流。"""

import hashlib
import time
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.errors import error_response_body
from config import settings
from infra.metrics import RATE_LIMIT_DEPENDENCY_FAILURES, RATE_LIMIT_REJECTED
from infra.rate_limiter import (
    RateLimitDependencyError,
    build_rate_limit_key,
    check_rate_limit,
)
from infra.redis_client import get_redis
from log_config import get_request_id


@dataclass(frozen=True)
class RateLimitPolicy:
    route: str
    limit: int
    window_seconds: int = 60


LOGIN_PATH = "/api/v1/auth/login"
CHAT_PATH = "/api/v1/chat"
CHAT_STREAM_PATH = "/api/v1/chat/stream"

_POLICIES = {
    LOGIN_PATH: RateLimitPolicy(LOGIN_PATH, settings.rate_limit_login_per_minute),
    CHAT_PATH: RateLimitPolicy(CHAT_PATH, settings.rate_limit_chat_per_minute),
    CHAT_STREAM_PATH: RateLimitPolicy(
        CHAT_STREAM_PATH,
        settings.rate_limit_chat_stream_per_minute,
    ),
}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _hashed_ip(request: Request) -> str:
    """将客户端 IP 稳定哈希后再放入 Redis key，避免保存原始 IP。"""
    return hashlib.sha256(_client_ip(request).encode("utf-8")).hexdigest()


def _user_subject(request: Request) -> tuple[str, str]:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict) and user.get("id") is not None:
        return "user", str(user["id"])
    return "ip", _hashed_ip(request)


def _rate_limited_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        content=error_response_body(
            "RATE_LIMITED",
            "请求过于频繁，请稍后重试",
            {},
            get_request_id(),
        ),
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _dependency_unavailable_response() -> JSONResponse:
    return JSONResponse(
        content=error_response_body(
            "DEPENDENCY_UNAVAILABLE",
            "限流服务暂时不可用，请稍后重试",
            {},
            get_request_id(),
        ),
        status_code=503,
    )


async def _apply_policy(
    request: Request,
    policy: RateLimitPolicy,
    subject_type: str,
    subject: str,
) -> Response | None:
    now = time.time()
    key = build_rate_limit_key(
        route=policy.route,
        subject_type=subject_type,
        subject=subject,
        now=now,
        window_seconds=policy.window_seconds,
    )
    try:
        result = await check_rate_limit(
            get_redis(),
            key=key,
            limit=policy.limit,
            window_seconds=policy.window_seconds,
            now=now,
        )
    except (RateLimitDependencyError, RuntimeError):
        RATE_LIMIT_DEPENDENCY_FAILURES.labels(
            route=policy.route,
            method=request.method,
            subject_type=subject_type,
        ).inc()
        return _dependency_unavailable_response()

    if result.allowed:
        return None

    RATE_LIMIT_REJECTED.labels(
        route=policy.route,
        method=request.method,
        subject_type=subject_type,
    ).inc()
    return _rate_limited_response(result.retry_after)


async def check_pre_auth_rate_limit(request: Request) -> Response | None:
    """为尚未通过鉴权的聊天请求按 IP 限流。"""
    policy = _POLICIES.get(request.url.path)
    if policy is None or request.url.path == LOGIN_PATH:
        return None
    return await _apply_policy(request, policy, "ip", _hashed_ip(request))


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        policy = _POLICIES.get(request.url.path)
        if policy is None:
            return await call_next(request)

        if request.url.path == LOGIN_PATH:
            subject_type, subject = "ip", _hashed_ip(request)
        else:
            subject_type, subject = _user_subject(request)

        response = await _apply_policy(request, policy, subject_type, subject)
        if response is not None:
            return response
        return await call_next(request)
