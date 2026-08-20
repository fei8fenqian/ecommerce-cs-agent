from contextlib import contextmanager

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import SecretStr

from api.metrics import metrics_router
from config import settings
from middleware.metrics import MetricsMiddleware


@contextmanager
def metrics_token(token: str):
    previous = settings.metrics_bearer_token
    settings.metrics_bearer_token = SecretStr(token)
    try:
        yield
    finally:
        settings.metrics_bearer_token = previous


def make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(metrics_router)

    @app.get("/items/{item_id}")
    async def item(item_id: str):
        return {"item_id": item_id}

    @app.get("/fails")
    async def fails():
        raise HTTPException(status_code=404, detail="missing")

    app.add_middleware(MetricsMiddleware)
    return app


async def request(app: FastAPI, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, **kwargs)


@pytest.mark.asyncio
async def test_metrics_endpoint_requires_configured_bearer_token():
    app = make_app()

    with metrics_token(""):
        response = await request(app, "/internal/metrics")
    assert response.status_code == 404

    with metrics_token("metrics-test-token"):
        missing = await request(app, "/internal/metrics")
        invalid = await request(
            app,
            "/internal/metrics",
            headers={"Authorization": "Bearer wrong-token"},
        )
        valid = await request(
            app,
            "/internal/metrics",
            headers={"Authorization": "Bearer metrics-test-token"},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 404
    assert valid.status_code == 200
    assert valid.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in valid.text


@pytest.mark.asyncio
async def test_metrics_use_route_template_and_exclude_metrics_scrapes():
    app = make_app()

    response = await request(app, "/items/private-item-123")
    failure = await request(app, "/fails")
    assert response.status_code == 200
    assert failure.status_code == 404

    with metrics_token("metrics-test-token"):
        metrics_response = await request(
            app,
            "/internal/metrics",
            headers={"Authorization": "Bearer metrics-test-token"},
        )

    body = metrics_response.text
    assert 'route="/items/{item_id}"' in body
    assert 'route="/fails"' in body
    assert "private-item-123" not in body
    assert 'route="/internal/metrics"' not in body
