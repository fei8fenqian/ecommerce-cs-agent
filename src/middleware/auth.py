from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from exceptions import AuthenticationError
from service.auth_service import verify_token

ALLOWLIST_PATHS = {"/health", "/api/v1/auth/login"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ALLOWLIST_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            raise HTTPException(status_code=401, detail="缺少 Authorization header")
        token = auth_header.removeprefix("Bearer ")
        try:
            user_info = await verify_token(token)
        except AuthenticationError as e:
            raise HTTPException(status_code=401, detail=e.to_dict())
        request.state.user = user_info
        return await call_next(request)
