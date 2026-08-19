"""tests/test_auth_api.py — /api/v1/auth/login + /logout 端点测试"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.auth import auth_router
from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import AuthenticationError, BaseAppException


# =============================================================================
# 测试 App
# =============================================================================
@pytest.fixture
def client():
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.include_router(auth_router)
    yield TestClient(app, raise_server_exceptions=False)


# =============================================================================
# Login
# =============================================================================
class TestLoginAPI:
    def test_login_success_returns_token_and_user(self, client):
        """mock login 成功 → 200 + token + user"""
        mock_user = {"id": 1, "username": "admin", "role": "admin"}
        mock_token = "eyJfake.admin.token"

        with patch(
            "api.auth.auth_login",
            new=AsyncMock(return_value=(mock_token, mock_user)),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "correct"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["token"] == mock_token
        assert data["user"]["id"] == 1
        assert data["user"]["username"] == "admin"
        # 不应暴露 password_hash
        assert "password_hash" not in data.get("user", {})

    def test_login_wrong_credentials_returns_401(self, client):
        with patch(
            "api.auth.auth_login",
            new=AsyncMock(side_effect=AuthenticationError("账号或密码错误")),
        ):
            resp = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "wrong"},
            )

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


# =============================================================================
# Logout
# =============================================================================
class TestLogoutAPI:
    def test_logout_success(self, client):
        with patch("api.auth.auth_logout", new=AsyncMock(return_value=None)):
            resp = client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": "Bearer some.valid.token"},
            )

        assert resp.status_code == 200
        assert "登出" in resp.json()["message"]

    def test_logout_missing_auth_header_returns_401(self, client):
        resp = client.post("/api/v1/auth/logout")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    def test_logout_invalid_token_returns_401(self, client):
        with patch(
            "api.auth.auth_logout",
            new=AsyncMock(side_effect=AuthenticationError("token 无效")),
        ):
            resp = client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": "Bearer bad.token"},
            )

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "TOKEN_INVALID"
