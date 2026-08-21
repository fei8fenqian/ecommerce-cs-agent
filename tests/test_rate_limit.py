import asyncio
from collections import defaultdict
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from prometheus_client import generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
)
from exceptions import BaseAppException
from infra.metrics import METRICS_REGISTRY
from infra.rate_limiter import check_rate_limit
from middleware.auth import AuthMiddleware
from middleware.rate_limit import (
    CHAT_PATH,
    CHAT_STREAM_PATH,
    LOGIN_PATH,
    RateLimitMiddleware,
    RateLimitPolicy,
)
from middleware.request_id import RequestIDMiddleware


class FakeRedis:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.counts: defaultdict[str, int] = defaultdict(int)
        self.calls: list[tuple[str, str, int]] = []
        self.lock = asyncio.Lock()

    async def eval(self, script: str, numkeys: int, key: str, window_seconds: int) -> int:
        if self.error is not None:
            raise self.error
        async with self.lock:
            self.calls.append((script, key, window_seconds))
            self.counts[key] += 1
            return self.counts[key]


def make_app(handler_calls: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(httpx.RequestError, handle_unexpected_exception)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)

    @app.post(LOGIN_PATH)
    async def login():
        handler_calls.append("login")
        return {"ok": True}

    @app.post(CHAT_PATH)
    async def chat(request: Request):
        handler_calls.append(f"chat:{request.state.user['id']}")
        return {"ok": True}

    @app.post(CHAT_STREAM_PATH)
    async def chat_stream(request: Request):
        handler_calls.append(f"stream:{request.state.user['id']}")
        return {"ok": True}

    # 注册顺序决定实际调用顺序：RequestID → Auth → RateLimit → 路由。
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


async def request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_check_rate_limit_uses_atomic_script_and_rejects_after_limit():
    redis = FakeRedis()

    first = await check_rate_limit(
        redis,
        key="rate_limit:test:ip:hash:1",
        limit=2,
        window_seconds=60,
        now=60.0,
    )
    second = await check_rate_limit(
        redis,
        key="rate_limit:test:ip:hash:1",
        limit=2,
        window_seconds=60,
        now=60.0,
    )
    third = await check_rate_limit(
        redis,
        key="rate_limit:test:ip:hash:1",
        limit=2,
        window_seconds=60,
        now=60.0,
    )

    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False
    assert len(redis.calls) == 3
    script = redis.calls[0][0]
    assert "INCR" in script
    assert "EXPIRE" in script


@pytest.mark.asyncio
async def test_concurrent_counter_is_serialized_by_atomic_operation():
    redis = FakeRedis()

    results = await asyncio.gather(
        *(
            check_rate_limit(
                redis,
                key="rate_limit:chat:user:101:1",
                limit=3,
                window_seconds=60,
                now=60.0,
            )
            for _ in range(10)
        )
    )

    assert sum(result.allowed for result in results) == 3
    assert redis.counts["rate_limit:chat:user:101:1"] == 10


@pytest.mark.asyncio
async def test_login_is_limited_by_client_ip_and_handler_is_not_called_after_limit(monkeypatch):
    redis = FakeRedis()
    calls: list[str] = []
    app = make_app(calls)
    monkeypatch.setattr("middleware.rate_limit.get_redis", lambda: redis)

    responses = [await request(app, "POST", LOGIN_PATH) for _ in range(6)]

    assert [response.status_code for response in responses[:5]] == [200] * 5
    assert responses[5].status_code == 429
    assert responses[5].json()["error"]["code"] == "RATE_LIMITED"
    assert responses[5].headers["Retry-After"].isdigit()
    assert len(calls) == 5


@pytest.mark.asyncio
async def test_authenticated_chat_is_limited_per_user(monkeypatch):
    redis = FakeRedis()
    calls: list[str] = []
    app = make_app(calls)
    monkeypatch.setattr("middleware.rate_limit.get_redis", lambda: redis)
    monkeypatch.setitem(
        __import__("middleware.rate_limit", fromlist=["_POLICIES"])._POLICIES,
        CHAT_PATH,
        RateLimitPolicy(CHAT_PATH, 2),
    )

    async def fake_verify(token: str):
        user_id = 101 if token == "token-a" else 202
        return {"id": user_id, "username": f"user-{user_id}", "role": "customer"}, "external"

    with patch("middleware.auth.verify_token", new=AsyncMock(side_effect=fake_verify)):
        a1 = await request(app, "POST", CHAT_PATH, headers={"Authorization": "Bearer token-a"})
        a2 = await request(app, "POST", CHAT_PATH, headers={"Authorization": "Bearer token-a"})
        a3 = await request(app, "POST", CHAT_PATH, headers={"Authorization": "Bearer token-a"})
        b1 = await request(app, "POST", CHAT_PATH, headers={"Authorization": "Bearer token-b"})

    assert [a1.status_code, a2.status_code, a3.status_code] == [200, 200, 429]
    assert b1.status_code == 200
    assert calls == ["chat:101", "chat:101", "chat:202"]


@pytest.mark.asyncio
async def test_unauthenticated_chat_is_limited_by_ip_and_rejected_before_handler(monkeypatch):
    redis = FakeRedis()
    calls: list[str] = []
    app = make_app(calls)
    monkeypatch.setattr("middleware.rate_limit.get_redis", lambda: redis)
    monkeypatch.setitem(
        __import__("middleware.rate_limit", fromlist=["_POLICIES"])._POLICIES,
        CHAT_PATH,
        RateLimitPolicy(CHAT_PATH, 2),
    )

    responses = [await request(app, "POST", CHAT_PATH) for _ in range(3)]

    assert [response.status_code for response in responses] == [401, 401, 429]
    assert responses[2].json()["error"]["code"] == "RATE_LIMITED"
    assert calls == []


@pytest.mark.asyncio
async def test_stream_has_separate_policy(monkeypatch):
    redis = FakeRedis()
    calls: list[str] = []
    app = make_app(calls)
    monkeypatch.setattr("middleware.rate_limit.get_redis", lambda: redis)

    async def fake_verify(token: str):
        return {"id": 303, "username": "stream-user", "role": "customer"}, "external"

    with patch("middleware.auth.verify_token", new=AsyncMock(side_effect=fake_verify)):
        response = await request(
            app,
            "POST",
            CHAT_STREAM_PATH,
            headers={"Authorization": "Bearer stream-token"},
        )

    assert response.status_code == 200
    assert calls == ["stream:303"]
    assert any(key.startswith(f"rate_limit:{CHAT_STREAM_PATH}:user:") for key in redis.counts)


@pytest.mark.asyncio
async def test_redis_failure_returns_503_and_does_not_call_handler(monkeypatch):
    redis = FakeRedis(error=ConnectionError("redis down"))
    calls: list[str] = []
    app = make_app(calls)
    monkeypatch.setattr("middleware.rate_limit.get_redis", lambda: redis)

    response = await request(app, "POST", LOGIN_PATH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.headers.get("X-Request-ID", "").startswith("req_")
    assert calls == []


def test_rate_limit_metrics_do_not_contain_subject_ids():
    output = generate_latest(METRICS_REGISTRY).decode()
    assert "rate_limit_rejected_total" in output
    assert "rate_limit_dependency_failures_total" in output
    assert 'subject="' not in output
    assert 'user_id="' not in output
