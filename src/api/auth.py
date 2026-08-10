import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from exceptions import AuthenticationError
from service.auth_service import login as auth_login
from service.auth_service import logout as auth_logout

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])


class LoginRequest(BaseModel):
    username: str
    password: str


@auth_router.post("/login")
async def login(login_req: LoginRequest):
    username = login_req.username
    password = login_req.password
    try:
        token, user = await auth_login(username, password)
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=e.to_dict())
    return JSONResponse(
        content={
            "token": token,
            "user": {"id": user.get("id"), "username": user.get("username")},
        },
        status_code=200,
    )


@auth_router.post("/logout")
async def logout(req: Request):
    auth_header = req.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="缺少 Authorization header")
    token = auth_header.removeprefix("Bearer ")
    try:
        await auth_logout(token)
    except AuthenticationError as e:
        raise HTTPException(status_code=401, detail=e.to_dict())
    return JSONResponse(content={"message": "账号已登出"}, status_code=200)
