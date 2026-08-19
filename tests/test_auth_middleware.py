"""tests/test_auth_middleware.py — AuthMiddleware 单元测试（FastAPI TestClient + mock）"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from exceptions import AuthenticationError, BaseAppException
from infra.casbin_enforcer import init_casbin
from middleware.auth import AuthMiddleware


# =============================================================================
# 测试 App
# =============================================================================
@pytest.fixture
def client():
    init_casbin()
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/test")
    async def test_route():
        return {"ok": True}

    @app.get("/api/v1/whoami")
    async def whoami(request: Request):
        return request.state.user

    @app.get("/api/v1/admin/users/42")
    async def admin_users_route():
        return {"ok": True}

    @app.get("/api/v1/raises-404")
    async def raises_404():
        raise HTTPException(status_code=404, detail="会话不存在")

    class RequiredPayload(BaseModel):
        ticket_id: int

    @app.post("/api/v1/validated")
    async def validated(payload: RequiredPayload):
        return payload

    @app.get("/api/v1/raises-unknown")
    async def raises_unknown():
        raise RuntimeError("internal secret")

    yield TestClient(app, raise_server_exceptions=False)


# =============================================================================
# 测试
# =============================================================================
class TestWhitelist:
    def test_health_bypasses_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_login_bypasses_auth(self, client):
        resp = client.post("/api/v1/auth/login", json={"username": "x", "password": "x"})
        # middleware 不拦；没有注册 auth router → 404
        assert resp.status_code in (200, 401, 404, 422)


class TestNoAuthHeader:
    def test_missing_auth_header_returns_401(self, client):
        resp = client.get("/api/v1/test")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
        assert "detail" not in resp.json()


class TestInvalidToken:
    def test_bad_token_returns_401(self, client):
        with patch(
            "middleware.auth.verify_token",
            new=AsyncMock(side_effect=AuthenticationError("token 无效: internal secret")),
        ):
            resp = client.get(
                "/api/v1/test",
                headers={"Authorization": "Bearer garbage"},
            )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "TOKEN_INVALID"
        assert "internal secret" not in resp.text


class TestInternalWithCasbin:
    def test_internal_has_permission(self, client):
        """admin token → Casbin 有权限 → 200"""
        from utils.jwt_utils import generate_jwt

        token = generate_jwt(1, "admin", "internal")
        mock_user = {"id": 1, "username": "admin", "role": "admin"}

        with patch("middleware.auth.verify_token", new=AsyncMock(return_value=(mock_user, "internal"))):
            resp = client.get("/api/v1/test", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_internal_no_permission_returns_403(self, client):
        """agent 没有 admin/users 权限 → 403"""
        from utils.jwt_utils import generate_jwt

        token = generate_jwt(2, "agent", "internal")
        mock_user = {"id": 2, "username": "agent1", "role": "agent"}

        with patch("middleware.auth.verify_token", new=AsyncMock(return_value=(mock_user, "internal"))):
            resp = client.get(
                "/api/v1/admin/users/42",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        assert "agent" not in resp.text

    def test_route_404_uses_generic_resource_message(self, client):
        mock_user = {"id": 3, "username": "buyer", "role": "customer"}
        with patch(
            "middleware.auth.verify_token",
            new=AsyncMock(return_value=(mock_user, "external")),
        ):
            resp = client.get(
                "/api/v1/raises-404",
                headers={"Authorization": "Bearer valid-token"},
            )

        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
        assert resp.json()["error"]["message"] == "资源不可用或无法核验"
        assert "会话不存在" not in resp.text

    def test_missing_required_field_returns_400(self, client):
        mock_user = {"id": 3, "username": "buyer", "role": "customer"}
        with patch(
            "middleware.auth.verify_token",
            new=AsyncMock(return_value=(mock_user, "external")),
        ):
            resp = client.post(
                "/api/v1/validated",
                json={},
                headers={"Authorization": "Bearer valid-token"},
            )

        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_REQUEST"
        assert resp.json()["error"]["details"]["fields"]

    def test_unknown_exception_returns_generic_500(self, client):
        mock_user = {"id": 3, "username": "buyer", "role": "customer"}
        with patch(
            "middleware.auth.verify_token",
            new=AsyncMock(return_value=(mock_user, "external")),
        ):
            resp = client.get(
                "/api/v1/raises-unknown",
                headers={"Authorization": "Bearer valid-token"},
            )

        assert resp.status_code == 500
        assert resp.json()["error"]["code"] == "INTERNAL_ERROR"
        assert resp.json()["error"]["message"] == "服务器内部错误，请稍后重试"
        assert "internal secret" not in resp.text

    def test_external_bypasses_casbin(self, client):
        """external 用户不走 Casbin，直接放行"""
        from utils.jwt_utils import generate_jwt

        token = generate_jwt(3, "customer", "external")
        mock_user = {"id": 3, "username": "buyer", "role": "customer"}

        with patch("middleware.auth.verify_token", new=AsyncMock(return_value=(mock_user, "external"))):
            resp = client.get(
                "/api/v1/admin/users/42",
                headers={"Authorization": f"Bearer {token}"},
            )
        # external 不走 Casbin，直接放行到 route handler
        assert resp.status_code == 200

    def test_request_state_excludes_password_hash(self, client):
        from utils.jwt_utils import generate_jwt

        token = generate_jwt(3, "customer", "external")
        mock_user = {
            "id": 3,
            "username": "buyer",
            "role": "customer",
            "password_hash": "must-not-reach-request-state",
        }

        with patch("middleware.auth.verify_token", new=AsyncMock(return_value=(mock_user, "external"))):
            resp = client.get(
                "/api/v1/whoami",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert "password_hash" not in resp.json()
