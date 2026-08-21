import asyncio
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import handle_app_exception, handle_http_exceptions, handle_unexpected_exception
from config import settings
from exceptions import BaseAppException
from infra.rate_limiter import check_rate_limit
from middleware.auth import AuthMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware
from utils.jwt_utils import generate_jwt


@pytest_asyncio.fixture
async def real_redis() -> AsyncIterator[aioredis.Redis]:
    parsed_url = urlparse(settings.redis_url)
    host = parsed_url.hostname or "localhost"
    port = parsed_url.port or 6379
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=1.0,
        )
        writer.close()
        await writer.wait_closed()
    except Exception as exc:
        pytest.skip(f"Redis 端口不可用，跳过真实限流集成测试: {exc}")

    redis = aioredis.from_url(
        settings.redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        await asyncio.wait_for(redis.ping(), timeout=1.5)
    except Exception as exc:
        await redis.aclose()
        pytest.skip(f"Redis 不可用，跳过真实限流集成测试: {exc}")

    yield redis
    await redis.aclose()


@pytest.mark.asyncio
async def test_real_redis_sets_ttl_and_resets_after_window(real_redis):
    key = f"rate_limit:integration:{uuid.uuid4().hex}"
    try:
        first = await check_rate_limit(
            real_redis,
            key=key,
            limit=2,
            window_seconds=2,
        )
        ttl = await real_redis.ttl(key)

        assert first.allowed is True
        assert 0 < ttl <= 2

        await asyncio.sleep(2.1)
        next_window = await check_rate_limit(
            real_redis,
            key=key,
            limit=2,
            window_seconds=2,
        )
        assert next_window.allowed is True
        assert next_window.current == 1
    finally:
        await real_redis.delete(key)


@pytest.mark.asyncio
async def test_real_redis_concurrent_requests_allow_only_limit(real_redis):
    key = f"rate_limit:integration:{uuid.uuid4().hex}"
    try:
        results = await asyncio.gather(
            *(
                check_rate_limit(
                    real_redis,
                    key=key,
                    limit=5,
                    window_seconds=60,
                )
                for _ in range(20)
            )
        )
        assert sum(result.allowed for result in results) == 5
    finally:
        await real_redis.delete(key)


@pytest.mark.asyncio
async def test_valid_token_with_redis_failure_returns_503_before_handler():
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    calls: list[str] = []

    @app.post("/api/v1/chat")
    async def chat(request: Request):
        calls.append("chat")
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)

    failing_redis = AsyncMock()
    failing_redis.get.side_effect = ConnectionError("redis down")
    token = generate_jwt(999001, "customer", "external")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with patch("service.auth_service.get_redis", return_value=failing_redis):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/chat",
                headers={"Authorization": f"Bearer {token}"},
            )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.headers.get("X-Request-ID", "").startswith("req_")
    assert calls == []
