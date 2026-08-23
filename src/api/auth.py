import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from exceptions import AuthenticationError
from service.auth_service import login as auth_login
from service.auth_service import logout as auth_logout
from service.auth_service import register_customer

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    """公开注册请求；不接受 role，外部接口只能创建 customer。"""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=8, max_length=72)


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
            # 前端需要据此展示角色对应的工作台；不返回 password_hash 等账户敏感字段。
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "role": user.get("role"),
            },
        },
        status_code=200,
    )


@auth_router.post("/register")
async def register(register_req: RegisterRequest):
    """注册客户账号并立即登录，内部角色不经过此公开入口创建。"""
    username = register_req.username.strip()
    result = await register_customer(username, register_req.password)
    if result is None:
        raise HTTPException(status_code=409, detail="用户名已存在，请直接登录")
    token, user = result
    return JSONResponse(
        content={
            "token": token,
            "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
        },
        status_code=201,
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
