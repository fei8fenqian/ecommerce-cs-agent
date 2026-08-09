import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from agent.llm.session import SessionManager
from infra.db_pool import check_alive

logger = logging.getLogger(__name__)

health_router = APIRouter(tags=["健康测试"])


@health_router.get("/health")
async def health(request: Request):
    session: SessionManager = request.app.state.session
    redis_check: bool = await session.health_check()
    pg_check: bool = await check_alive()
    if not redis_check and not pg_check:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "redis, postgres 不可用"},
        )
    elif redis_check and not pg_check:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "postgres 不可用"},
        )
    elif pg_check and not redis_check:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "redis 不可用"},
        )
    else:
        return JSONResponse(
            status_code=200,
            content={"status": "OK"},
        )
