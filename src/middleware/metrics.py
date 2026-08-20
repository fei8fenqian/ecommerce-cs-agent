import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from infra.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS

_METRICS_PATH = "/internal/metrics"


def _route_template(scope: Scope) -> str:
    """返回路由模板，避免把真实资源 ID 放进指标标签。"""
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    return route_path if isinstance(route_path, str) else "unmatched"


def _record_request(scope: Scope, status_code: int, duration_seconds: float) -> None:
    """记录请求信息。"""
    method = str(scope.get("method", "UNKNOWN"))
    route = _route_template(scope)
    status = str(status_code)

    HTTP_REQUESTS.labels(
        method=method,
        route=route,
        status_code=status,
    ).inc()

    HTTP_REQUEST_DURATION.labels(
        method=method,
        route=route,
    ).observe(duration_seconds)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("path") == "/internal/metrics":
            await self.app(scope, receive, send)
            return

        try:
            start_time = time.perf_counter()
            status_code = 500
            recorded = False

            async def send_wrapper(message: Message) -> None:
                nonlocal status_code, recorded
                if message["type"] == "http.response.start":
                    status_code = message["status"]

                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    request_duration = time.perf_counter() - start_time
                    if not recorded:
                        _record_request(scope, status_code, request_duration)
                        recorded = True

                await send(message)
                return

            await self.app(scope, receive, send_wrapper)

        except Exception:
            if not recorded:
                _record_request(scope, 500, time.perf_counter() - start_time)
                recorded = True
            raise
