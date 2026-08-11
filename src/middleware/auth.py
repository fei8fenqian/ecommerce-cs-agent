from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from exceptions import AuthenticationError
from infra.casbin_enforcer import enforce
from service.auth_service import verify_token

ALLOWLIST_PATHS = {"/health", "/api/v1/auth/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    """登录认证中间件"""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ALLOWLIST_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(status_code=401, content={"detail": "缺少 Authorization header"})
        token = auth_header.removeprefix("Bearer ")
        try:
            user_info, user_type = await verify_token(token)
            request.state.user = user_info
            # internal
            if user_type == "internal":
                if not enforce(user_info["role"], request.url.path, request.method):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": f"{user_info['role']}无权通过{request.method}访问{request.url.path}"},
                    )
        except AuthenticationError as e:
            return JSONResponse(status_code=401, content={"detail": e.to_dict()})
        return await call_next(request)
