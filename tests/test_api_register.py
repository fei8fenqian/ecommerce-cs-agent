"""公开客户注册接口测试。"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.auth import auth_router
from api.errors import handle_http_exceptions, handle_validation_error


async def request(app: FastAPI, payload: dict) -> httpx.Response:
    """通过异步 ASGI transport 请求注册接口。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/v1/auth/register", json=payload)


def make_app() -> FastAPI:
    """构造注册接口使用的最小测试 App。"""
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.include_router(auth_router)
    return app


@pytest.mark.asyncio
async def test_register_creates_customer_and_returns_login_identity():
    """公开注册成功后只能得到 customer 身份和登录 token。"""
    registered = ("signed-token", {"id": 42, "username": "new_customer", "role": "customer"})
    with patch("api.auth.register_customer", new=AsyncMock(return_value=registered)) as mocked:
        response = await request(make_app(), {"username": "new_customer", "password": "secure-pass-1"})

    assert response.status_code == 201
    assert response.json()["user"] == {"id": 42, "username": "new_customer", "role": "customer"}
    mocked.assert_awaited_once_with("new_customer", "secure-pass-1")


@pytest.mark.asyncio
async def test_register_existing_name_returns_conflict():
    """冲突账号不会被覆盖或改为其他角色。"""
    with patch("api.auth.register_customer", new=AsyncMock(return_value=None)):
        response = await request(make_app(), {"username": "taken_name", "password": "secure-pass-1"})

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_register_rejects_short_or_invalid_credentials():
    """格式错误在到达账号 Store 前由 API 拒绝。"""
    response = await request(make_app(), {"username": "坏 账号", "password": "short"})

    assert response.status_code == 400
