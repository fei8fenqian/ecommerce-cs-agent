from fastapi import APIRouter

from exceptions import DependencyUnavailableError
from infra.db_pool import check_alive
from infra.redis_client import health_check

health_router = APIRouter(tags=["健康测试"])


@health_router.get("/health")
async def health():
    redis_check: bool = await health_check()
    pg_check: bool = await check_alive()
    if not redis_check or not pg_check:
        raise DependencyUnavailableError("健康检查依赖不可用")
    return {"status": "OK"}
