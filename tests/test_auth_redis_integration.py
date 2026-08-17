"""S1-02 Redis integration checks.

These tests intentionally use redis-py's default ``decode_responses=False`` so
that Redis returns ``bytes`` for stored tokens, matching the production client.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import FastAPI

from config import settings
from middleware.auth import AuthMiddleware
from service.auth_service import _key, login
from utils.jwt_utils import generate_jwt


@pytest_asyncio.fixture
async def real_redis():
    redis = aioredis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        await redis.ping()
    except Exception as exc:
        await redis.aclose()
        pytest.skip(f"Redis 不可用，跳过 S1-02 集成测试: {exc}")

    try:
        yield redis
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_login_stores_token_as_bytes_in_real_redis(real_redis):
    user_id = 970001
    user = {
        "id": user_id,
        "username": "redis-test-customer",
        "password_hash": "unused",
        "role": "customer",
    }
    key = _key(user_id)

    try:
        with (
            patch("service.auth_service.get_user_by_username", new=AsyncMock(return_value=user)),
            patch("service.auth_service.get_redis", return_value=real_redis),
            patch("service.auth_service.verify_hashed_password", return_value=True),
        ):
            token, _ = await login(user["username"], "correct_password")

        saved_token = await real_redis.get(key)
        assert isinstance(saved_token, bytes)
        assert saved_token.decode() == token
    finally:
        await real_redis.delete(key)


@pytest.mark.asyncio
async def test_real_redis_bytes_token_allows_protected_request(real_redis):
    user_id = 970002
    token = generate_jwt(user_id, "customer", "external")
    key = _key(user_id)
    user = {"id": user_id, "username": "redis-test-customer", "role": "customer"}

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    try:
        await real_redis.set(key, token)
        assert isinstance(await real_redis.get(key), bytes)
        with (
            patch("service.auth_service.get_redis", return_value=real_redis),
            patch("service.auth_service.get_user_by_id", new=AsyncMock(return_value=user)),
        ):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json() == {"ok": True}
    finally:
        await real_redis.delete(key)


@pytest.mark.asyncio
async def test_real_redis_rejects_stale_token(real_redis):
    user_id = 970003
    valid_token = generate_jwt(user_id, "customer", "external")
    stale_token = generate_jwt(user_id, "agent", "internal")
    key = _key(user_id)

    try:
        await real_redis.set(key, valid_token)
        with (
            patch("service.auth_service.get_redis", return_value=real_redis),
            patch("service.auth_service.get_user_by_id", new=AsyncMock()),
        ):
            from exceptions import AuthenticationError
            from service.auth_service import verify_token

            with pytest.raises(AuthenticationError, match="其他设备登录"):
                await verify_token(stale_token)
    finally:
        await real_redis.delete(key)
