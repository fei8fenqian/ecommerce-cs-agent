import asyncio
import re

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.errors import handle_http_exceptions, handle_unexpected_exception
from exceptions import BaseAppException
from log_config import (
    get_request_id,
    get_span_id,
    get_trace_headers,
    get_trace_id,
    get_trace_metadata,
    get_traceparent,
)
from middleware.auth import AuthMiddleware
from middleware.request_id import RequestIDMiddleware, parse_traceparent

TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")
TRACEPARENT_RE = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")

UPSTREAM_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def make_app(*, with_auth: bool = False) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, handle_http_exceptions)
    app.add_exception_handler(BaseAppException, handle_unexpected_exception)
    app.add_exception_handler(Exception, handle_unexpected_exception)

    @app.get("/context")
    async def context(request: Request):
        return {
            "state_request_id": request.state.request_id,
            "state_trace_id": request.state.trace_id,
            "state_span_id": request.state.span_id,
            "state_traceparent": request.state.traceparent,
            "request_id": get_request_id(),
            "trace_id": get_trace_id(),
            "span_id": get_span_id(),
            "traceparent": get_traceparent(),
            "trace_headers": get_trace_headers(),
            "trace_metadata": get_trace_metadata(),
        }

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    @app.get("/raises-404")
    async def raises_404():
        raise HTTPException(status_code=404, detail="secret resource detail")

    @app.get("/raises-500")
    async def raises_500():
        raise HTTPException(status_code=500, detail="internal-secret")

    @app.get("/sleep")
    async def sleep_with_context():
        await asyncio.sleep(0.01)
        return {
            "request_id": get_request_id(),
            "trace_id": get_trace_id(),
            "traceparent": get_traceparent(),
        }

    if with_auth:
        # Starlette 后添加的 RequestIDMiddleware 在外层，未登录请求也能获得 ID。
        app.add_middleware(AuthMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


async def request(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, **kwargs)


def assert_generated_request_id(request_id: str) -> None:
    assert request_id.startswith("req_")
    assert len(request_id) == len("req_") + 32
    assert re.fullmatch(r"req_[0-9a-f]{32}", request_id)


@pytest.mark.asyncio
async def test_valid_request_and_trace_ids_are_propagated():
    response = await request(
        make_app(),
        "GET",
        "/context",
        headers={
            "X-Request-ID": "client-request-1",
            "traceparent": UPSTREAM_TRACEPARENT,
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "client-request-1"
    assert response.headers["X-Trace-ID"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert response.headers["traceparent"] == body["traceparent"]
    assert body["state_request_id"] == "client-request-1"
    assert body["request_id"] == "client-request-1"
    assert body["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert body["state_trace_id"] == body["trace_id"]
    assert body["state_span_id"] == body["span_id"]
    assert body["state_traceparent"] == body["traceparent"]
    assert TRACEPARENT_RE.fullmatch(body["traceparent"])
    assert body["span_id"] != "00f067aa0ba902b7"
    assert body["trace_headers"] == {"traceparent": body["traceparent"]}
    assert body["trace_metadata"] == {
        "trace_id": body["trace_id"],
        "span_id": body["span_id"],
        "traceparent": body["traceparent"],
    }


@pytest.mark.asyncio
async def test_missing_ids_are_generated():
    response = await request(make_app(), "GET", "/context")

    body = response.json()
    assert_generated_request_id(response.headers["X-Request-ID"])
    assert TRACE_ID_RE.fullmatch(response.headers["X-Trace-ID"])
    assert TRACEPARENT_RE.fullmatch(response.headers["traceparent"])
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["trace_id"] == response.headers["X-Trace-ID"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_request_id", ["", "   ", "bad\nid", "x" * 129])
async def test_invalid_request_id_is_replaced(invalid_request_id: str):
    response = await request(
        make_app(),
        "GET",
        "/context",
        headers={"X-Request-ID": invalid_request_id},
    )

    generated = response.headers["X-Request-ID"]
    assert_generated_request_id(generated)
    assert generated != invalid_request_id


@pytest.mark.asyncio
async def test_invalid_traceparent_starts_a_new_trace():
    response = await request(
        make_app(),
        "GET",
        "/context",
        headers={"traceparent": "00-INVALID-INVALID-01"},
    )

    assert response.status_code == 200
    assert response.headers["X-Trace-ID"] != "INVALID"
    assert TRACE_ID_RE.fullmatch(response.headers["X-Trace-ID"])
    assert parse_traceparent("00-" + "0" * 32 + "-00f067aa0ba902b7-01") is None


@pytest.mark.asyncio
async def test_unauthenticated_response_contains_request_context_headers():
    response = await request(make_app(with_auth=True), "GET", "/protected")

    assert response.status_code == 401
    assert_generated_request_id(response.headers["X-Request-ID"])
    assert TRACE_ID_RE.fullmatch(response.headers["X-Trace-ID"])
    assert TRACEPARENT_RE.fullmatch(response.headers["traceparent"])
    assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_error_responses_contain_request_id():
    app = make_app()

    not_found = await request(app, "GET", "/raises-404")
    internal = await request(app, "GET", "/raises-500")

    assert not_found.status_code == 404
    assert not_found.json()["error"]["request_id"] == not_found.headers["X-Request-ID"]
    assert internal.status_code == 500
    assert internal.json()["error"]["request_id"] == internal.headers["X-Request-ID"]
    assert "internal-secret" not in internal.text


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_mix_context():
    app = make_app()
    first_id = "request-a"
    second_id = "request-b"
    first_trace = "00-11111111111111111111111111111111-1111111111111111-01"
    second_trace = "00-22222222222222222222222222222222-2222222222222222-01"

    first, second = await asyncio.gather(
        request(
            app,
            "GET",
            "/sleep",
            headers={"X-Request-ID": first_id, "traceparent": first_trace},
        ),
        request(
            app,
            "GET",
            "/sleep",
            headers={"X-Request-ID": second_id, "traceparent": second_trace},
        ),
    )

    first_body = first.json()
    second_body = second.json()
    assert first_body["request_id"] == first_id
    assert first_body["trace_id"] == "1" * 32
    assert second_body["request_id"] == second_id
    assert second_body["trace_id"] == "2" * 32
    assert first.headers["X-Request-ID"] == first_id
    assert second.headers["X-Request-ID"] == second_id


@pytest.mark.asyncio
async def test_contextvars_are_restored_after_request():
    assert get_request_id() == "-"
    assert get_trace_id() == "-"
    assert get_span_id() == "-"
    assert get_traceparent() == "-"

    response = await request(
        make_app(),
        "GET",
        "/context",
        headers={"X-Request-ID": "temporary-request"},
    )

    assert response.status_code == 200
    assert get_request_id() == "-"
    assert get_trace_id() == "-"
    assert get_span_id() == "-"
    assert get_traceparent() == "-"
