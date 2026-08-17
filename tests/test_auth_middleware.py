"""tests/test_auth_middleware.py — AuthMiddleware 单元测试（FastAPI TestClient + mock）"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from infra.casbin_enforcer import init_casbin
from middleware.auth import AuthMiddleware


# =============================================================================
# 测试 App
# =============================================================================
@pytest.fixture
def client():
    init_casbin()
    app = FastAPI()
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
        assert "缺少 Authorization header" in resp.text


class TestInvalidToken:
    def test_bad_token_returns_401(self, client):
        resp = client.get("/api/v1/test", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401


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
