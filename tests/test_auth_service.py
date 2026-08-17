"""tests/test_auth_service.py — auth_service 业务逻辑测试（mock DB + Redis）"""

from unittest.mock import AsyncMock, patch

import jwt
import pytest

from exceptions import AuthenticationError
from service.auth_service import login, logout, verify_token
from utils.jwt_utils import generate_jwt
from utils.password_utils import generate_hashed_password


# =============================================================================
# 辅助
# =============================================================================
def _mock_user(role="admin", user_id=1, username="admin"):
    return {
        "id": user_id,
        "username": username,
        "password_hash": generate_hashed_password("correct_password").decode(),
        "role": role,
    }


# =============================================================================
# login
# =============================================================================
class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success(self):
        """正确账号密码 → 返回 token + user_info"""
        user = _mock_user()
        mock_redis = AsyncMock()

        with (
            patch("service.auth_service.get_user_by_username", new=AsyncMock(return_value=user)),
            patch("service.auth_service.get_redis", return_value=mock_redis),
        ):
            token, info = await login("admin", "correct_password")

        assert isinstance(token, str)
        assert info["username"] == "admin"
        assert "password_hash" not in info
        mock_redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_login_user_not_found(self):
        with patch("service.auth_service.get_user_by_username", new=AsyncMock(return_value={})):
            with pytest.raises(AuthenticationError, match="账号或密码错误"):
                await login("ghost", "any")

    @pytest.mark.asyncio
    async def test_login_wrong_password(self):
        user = _mock_user()
        with patch("service.auth_service.get_user_by_username", new=AsyncMock(return_value=user)):
            with pytest.raises(AuthenticationError, match="账号或密码错误"):
                await login("admin", "wrong_password")

    @pytest.mark.asyncio
    async def test_login_user_type_derivation(self):
        """验证 external/internal 推导正确进 JWT"""
        user = _mock_user(role="customer", user_id=2, username="buyer")
        mock_redis = AsyncMock()

        with (
            patch("service.auth_service.get_user_by_username", new=AsyncMock(return_value=user)),
            patch("service.auth_service.get_redis", return_value=mock_redis),
        ):
            token, _ = await login("buyer", "correct_password")

        from utils.jwt_utils import parse_jwt

        payload = parse_jwt(token)
        assert payload["role"] == "customer"
        assert payload["user_type"] == "external"


# =============================================================================
# logout
# =============================================================================
class TestLogout:
    @pytest.mark.asyncio
    async def test_logout_deletes_redis_key(self):
        token = generate_jwt(1, "admin", "internal")
        mock_redis = AsyncMock()

        with (
            patch("service.auth_service.parse_jwt", return_value={"sub": "1"}),
            patch("service.auth_service.get_redis", return_value=mock_redis),
        ):
            await logout(token)

        mock_redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_logout_invalid_token(self):
        with pytest.raises(AuthenticationError):
            await logout("not_a_valid_token")


# =============================================================================
# verify_token
# =============================================================================
class TestVerifyToken:
    @pytest.mark.asyncio
    async def test_verify_success(self):
        token = generate_jwt(1, "agent", "internal")
        user = _mock_user(role="agent", user_id=1, username="agent1")

        with (
            patch("service.auth_service.get_redis", return_value=AsyncMock(get=AsyncMock(return_value=token))),
            patch("service.auth_service.get_user_by_id", new=AsyncMock(return_value=user)),
        ):
            info, user_type = await verify_token(token)

        assert info["username"] == "agent1"
        assert info["role"] == "agent"
        assert "password_hash" not in info
        assert user_type == "internal"

    @pytest.mark.asyncio
    async def test_verify_success_with_redis_bytes(self):
        """Redis 默认返回 bytes 时，合法 token 仍应通过校验。"""
        token = generate_jwt(1, "agent", "internal")
        user = _mock_user(role="agent", user_id=1, username="agent1")

        with (
            patch(
                "service.auth_service.get_redis",
                return_value=AsyncMock(get=AsyncMock(return_value=token.encode())),
            ),
            patch("service.auth_service.get_user_by_id", new=AsyncMock(return_value=user)),
        ):
            info, user_type = await verify_token(token)

        assert info["id"] == 1
        assert user_type == "internal"

    @pytest.mark.asyncio
    async def test_verify_expired_token(self):
        """过期 token → AuthenticationError"""
        from datetime import datetime, timedelta, timezone
        from pathlib import Path

        expired_payload = {
            "sub": 1,
            "role": "admin",
            "user_type": "internal",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        private_key = Path("private_key.pem").read_text()
        token = jwt.encode(expired_payload, private_key, algorithm="RS256")

        with pytest.raises(AuthenticationError, match="登录已过期"):
            await verify_token(token)

    @pytest.mark.asyncio
    async def test_verify_redis_not_found(self):
        """token 不在 Redis → 已过期/已登出"""
        token = generate_jwt(1, "admin", "internal")

        with (
            patch("service.auth_service.parse_jwt", return_value={"sub": "1"}),
            patch("service.auth_service.get_redis", return_value=AsyncMock(get=AsyncMock(return_value=None))),
        ):
            with pytest.raises(AuthenticationError, match="登录已过期"):
                await verify_token(token)

    @pytest.mark.asyncio
    async def test_verify_token_mismatch(self):
        """token 和 Redis 里存的不一样 → 已在其他设备登录"""
        token = generate_jwt(1, "admin", "internal")

        with (
            patch("service.auth_service.parse_jwt", return_value={"sub": "1"}),
            patch(
                "service.auth_service.get_redis",
                return_value=AsyncMock(get=AsyncMock(return_value=b"different_token")),
            ),
        ):
            with pytest.raises(AuthenticationError, match="其他设备登录"):
                await verify_token(token)

    @pytest.mark.asyncio
    async def test_verify_user_deleted(self):
        """token 有效但 DB 里用户被删了"""
        token = generate_jwt(1, "admin", "internal")

        with (
            patch("service.auth_service.get_redis", return_value=AsyncMock(get=AsyncMock(return_value=token))),
            patch("service.auth_service.get_user_by_id", new=AsyncMock(return_value={})),
        ):
            with pytest.raises(AuthenticationError, match="用户不存在"):
                await verify_token(token)
