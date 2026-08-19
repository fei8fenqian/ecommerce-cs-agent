from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from api.errors import error_response_body
from exceptions import AuthenticationError
from infra.casbin_enforcer import enforce
from log_config import get_request_id
from service.auth_service import verify_token

ALLOWLIST_PATHS = {"/health", "/api/v1/auth/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """登录认证中间件"""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ALLOWLIST_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return self._error_response(
                code="AUTHENTICATION_REQUIRED",
                message="需要登录后访问",
            )
        token = auth_header.removeprefix("Bearer ")
        try:
            user_info, user_type = await verify_token(token)
            # 认证层只向业务传递最小身份上下文，避免 password_hash 等敏感字段下沉。
            request.state.user = {
                "id": user_info["id"],
                "username": user_info["username"],
                "role": user_info["role"],
            }
            # internal
            if user_type == "internal":
                if not enforce(user_info["role"], request.url.path, request.method):
                    return self._error_response(
                        code="FORBIDDEN",
                        message="无权访问该资源",
                        status_code=403,
                    )
        except AuthenticationError:
            return self._error_response(
                code="TOKEN_INVALID",
                message="登录状态无效，请重新登录",
            )
        return await call_next(request)

    @staticmethod
    def _error_response(
        code: str,
        message: str,
        status_code: int = 401,
    ) -> JSONResponse:
        content = error_response_body(
            code,
            message,
            {},
            get_request_id(),
        )
        return JSONResponse(content=content, status_code=status_code)
