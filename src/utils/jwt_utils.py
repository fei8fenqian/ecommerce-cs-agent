import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

logger = logging.getLogger(__name__)


def generate_jwt(
    user_id: int,
    role: str,
    user_type: str,
    *,
    expires_in_seconds: int = 3600,
) -> str:
    """生成签名的访问令牌。

    Args:
        user_id: 已认证用户的数据库 ID。
        role: 当前用户角色。
        user_type: ``internal`` 或 ``external``。
        expires_in_seconds: 令牌的有效期；调用方必须与服务端会话有效期保持一致。

    Returns:
        可供 Authorization Bearer 使用的 RS256 JWT。
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,  # admin | agent | customer
        "user_type": user_type,  # external | internal
        "exp": now + timedelta(seconds=expires_in_seconds),
        "iat": now,
    }
    private_key: str = Path("private_key.pem").read_text()
    token: str = jwt.encode(payload, private_key, algorithm="RS256")
    return token


def parse_jwt(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(token, Path("public_key.pem").read_text(), algorithms=["RS256"])
    return payload
