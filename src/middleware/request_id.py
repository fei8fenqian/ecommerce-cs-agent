import logging
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from log_config import get_request_id, set_request_id

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        t_start = time.perf_counter()
        # 读请求id
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        # 记录进日志 保证协程安全
        set_request_id(request_id=request_id)

        # 调用下一层
        response: Response = await call_next(request)

        # 返回响应
        total_time = (time.perf_counter() - t_start) * 1000
        response.headers["X-Request-ID"] = get_request_id()
        response.headers["X-Response-Time-ms"] = f"{total_time:.1f}"

        return response
