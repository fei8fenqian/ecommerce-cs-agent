from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import (
    handle_app_exception,
    handle_http_exceptions,
    handle_unexpected_exception,
    handle_validation_error,
)
from api.health import health as health_endpoint
from exceptions import AuthenticationError, BaseAppException
from infra.casbin_enforcer import init_casbin
from middleware.auth import AuthMiddleware


class RequiredPayload(BaseModel):
    ticket_id: int


def make_app() -> FastAPI:
    init_casbin()
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(BaseAppException, handle_app_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)
    app.add_middleware(AuthMiddleware)

    @app.get("/health")
    async def health_route():
        return await health_endpoint()

    @app.get("/api/v1/raises-404")
    async def raises_404():
        raise HTTPException(status_code=404, detail="会话不存在")

    @app.post("/api/v1/validated")
    async def validated(payload: RequiredPayload):
        return payload

    @app.get("/api/v1/admin/users/42")
    async def admin_users_route():
        return {"ok": True}

    @app.get("/api/v1/raises-unknown")
    async def raises_unknown():
        raise RuntimeError("internal secret")

    @app.get("/api/v1/raises-500")
    async def raises_500():
        raise HTTPException(status_code=500, detail="internal-secret")

    @app.get("/api/v1/raises-503")
    async def raises_503():
        raise HTTPException(status_code=503, detail="dependency-secret")

    @app.get("/api/v1/raises-422")
    async def raises_422():
        raise HTTPException(
            status_code=422,
            detail={"code": "ORDER_NOT_ELIGIBLE", "message": "ignored"},
        )

    @app.get("/api/v1/raises-422-invalid")
    async def raises_invalid_422():
        raise HTTPException(
            status_code=422,
            detail={"code": "CLIENT_CONTROLLED_CODE"},
        )

    return app


async def request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.asyncio
async def test_missing_authorization_uses_unified_response():
    response = await request(make_app(), "GET", "/api/v1/raises-404")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"
    assert "detail" not in response.json()


@pytest.mark.asyncio
async def test_invalid_token_uses_token_invalid():
    app = make_app()
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(side_effect=AuthenticationError("token 无效: internal secret")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-404",
            headers={"Authorization": "Bearer invalid"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"
    assert "internal secret" not in response.text


@pytest.mark.asyncio
async def test_casbin_denial_uses_forbidden():
    app = make_app()
    user = {"id": 2, "username": "agent", "role": "agent"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "internal")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/admin/users/42",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert "agent" not in response.text


@pytest.mark.asyncio
async def test_route_404_hides_resource_detail():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-404",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_AVAILABLE"
    assert response.json()["error"]["message"] == "资源不可用或无法核验"
    assert "会话不存在" not in response.text


@pytest.mark.asyncio
async def test_validation_error_returns_400():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "POST",
            "/api/v1/validated",
            json={},
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["error"]["details"]["fields"]


@pytest.mark.asyncio
async def test_unknown_exception_returns_generic_500():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-unknown",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "服务器内部错误，请稍后重试"
    assert "internal secret" not in response.text


@pytest.mark.asyncio
async def test_http_500_does_not_expose_detail():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-500",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["message"] == "服务器内部错误，请稍后重试"
    assert "internal-secret" not in response.text


@pytest.mark.asyncio
async def test_http_503_does_not_expose_detail():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-503",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["error"]["message"] == "服务暂时不可用，请稍后重试"
    assert "dependency-secret" not in response.text


@pytest.mark.asyncio
async def test_health_redis_failure_uses_unified_dependency_error():
    app = make_app()
    with (
        patch("api.health.health_check", new=AsyncMock(return_value=False)),
        patch("api.health.check_alive", new=AsyncMock(return_value=True)),
    ):
        response = await request(app, "GET", "/health")

    body = response.json()
    assert response.status_code == 503
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["message"] == "依赖服务暂时不可用，请稍后重试"
    assert body["error"]["details"] == {}
    assert set(body["error"]) == {"code", "message", "details", "request_id"}


@pytest.mark.asyncio
async def test_health_postgres_failure_uses_unified_dependency_error():
    app = make_app()
    with (
        patch("api.health.health_check", new=AsyncMock(return_value=True)),
        patch("api.health.check_alive", new=AsyncMock(return_value=False)),
    ):
        response = await request(app, "GET", "/health")

    body = response.json()
    assert response.status_code == 503
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["details"] == {}
    assert "postgres" not in response.text


@pytest.mark.asyncio
async def test_422_accepts_only_whitelisted_business_code():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-422",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "ORDER_NOT_ELIGIBLE"
    assert response.json()["error"]["message"] == "订单不符合退款条件"


@pytest.mark.asyncio
async def test_422_unknown_code_becomes_internal_error():
    app = make_app()
    user = {"id": 3, "username": "buyer", "role": "customer"}
    with patch(
        "middleware.auth.verify_token",
        new=AsyncMock(return_value=(user, "external")),
    ):
        response = await request(
            app,
            "GET",
            "/api/v1/raises-422-invalid",
            headers={"Authorization": "Bearer valid"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "CLIENT_CONTROLLED_CODE" not in response.text
