import secrets

from fastapi import APIRouter, Header, HTTPException
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from config import settings
from infra.metrics import METRICS_REGISTRY

metrics_router = APIRouter(include_in_schema=False)


def _has_valid_metrics_token(authorization: str | None) -> bool:
    expected_token = settings.metrics_bearer_token.get_secret_value()
    if not expected_token or not authorization or not authorization.startswith("Bearer "):
        return False
    return secrets.compare_digest(authorization.removeprefix("Bearer "), expected_token)


@metrics_router.get("/internal/metrics", response_class=Response)
async def metrics(authorization: str | None = Header(default=None)) -> Response:
    """仅供内部监控系统抓取的 Prometheus 指标端点。"""
    if not _has_valid_metrics_token(authorization):
        # 不向公网调用者暴露 metrics endpoint 是否启用。
        raise HTTPException(status_code=404, detail="资源不存在")
    return Response(content=generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST)
