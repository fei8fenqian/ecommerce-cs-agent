import logging

import jwt

from exceptions import AuthenticationError
from infra.redis_client import get_redis
from store.user_store import get_user_by_id, get_user_by_username
from utils.jwt_utils import generate_jwt, parse_jwt
from utils.password_utils import verify_hashed_password

logger = logging.getLogger(__name__)

_KEY_PREFIX = "login:user"


def _key(user_id: int) -> str:
    return f"{_KEY_PREFIX}:{user_id}"


async def login(username: str, password: str) -> tuple[str, dict]:
    """验证账号密码，生成 JWT 并存入 Redis。

    Returns:
        (token, user_info) — user_info 不含 password_hash

    Raises:
        AuthenticationError: 账号或密码错误
    """
    user_info: dict = await get_user_by_username(username)

    # 不区分"用户不存在"和"密码错误"——防止攻击者通过错误信息枚举用户
    if not user_info:
        logger.warning("login failed: user not found username=%s", username)
        raise AuthenticationError("账号或密码错误")

    hashed_password = user_info.get("password_hash", "")
    if not verify_hashed_password(password, hashed_password):
        logger.warning("login failed: password mismatch username=%s", username)
        raise AuthenticationError("账号或密码错误")

    user_id = user_info["id"]
    role = user_info["role"]
    if role in {"agent", "operator", "admin"}:
        user_type = "internal"
    else:
        user_type = "external"
    token = generate_jwt(user_id, role, user_type)
    redis = get_redis()
    await redis.set(_key(user_id), token, ex=3600)  # 1h

    user_info.pop("password_hash", None)
    logger.info("login success: user_id=%s username=%s", user_id, username)
    return token, user_info


async def logout(token: str) -> None:
    """登出, 删除 Redis 中的登录 token。

    Raises:
        AuthenticationError: token 无效
    """
    payload = parse_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("token 无效，缺少用户标识")

    redis = get_redis()
    await redis.delete(_key(int(user_id)))
    logger.info("logout success: user_id=%s", user_id)


async def verify_token(token: str) -> tuple[dict, str]:
    """验证 token 有效性，返回当前用户信息。

    Raises:
        AuthenticationError: token 无效 / 已过期 / 已登出
    """
    try:
        payload = parse_jwt(token)
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("登录已过期，请重新登录")
    except jwt.InvalidTokenError as e:
        raise AuthenticationError(f"token 无效: {str(e)}")

    user_id = payload.get("sub")
    if not user_id:
        raise AuthenticationError("token 无效，缺少用户标识")

    redis = get_redis()
    saved_token = await redis.get(_key(int(user_id)))
    if saved_token is None:
        raise AuthenticationError("登录已过期，请重新登录")
    if saved_token.decode() if isinstance(saved_token, bytes) else saved_token != token:
        raise AuthenticationError("账号已在其他设备登录，请重新登录")

    user_info = await get_user_by_id(int(user_id))
    if not user_info:
        raise AuthenticationError("用户不存在或已被删除")
    if user_info["role"] in {"agent", "operator", "admin"}:
        user_type = "internal"
    else:
        user_type = "external"
    return user_info, user_type
