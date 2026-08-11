"""tests/test_auth_utils.py — password_utils + jwt_utils 纯逻辑测试"""

from pathlib import Path

import jwt
import pytest

from utils.jwt_utils import generate_jwt, parse_jwt
from utils.password_utils import generate_hashed_password, verify_hashed_password


# =============================================================================
# password_utils
# =============================================================================
class TestPasswordUtils:
    def test_hash_and_verify_str(self):
        pw = "test_password"
        hashed = generate_hashed_password(pw)
        assert isinstance(hashed, bytes)
        assert verify_hashed_password(pw, hashed)

    def test_hash_and_verify_bytes(self):
        pw = b"test_bytes"
        hashed = generate_hashed_password(pw)
        assert verify_hashed_password(pw, hashed)

    def test_wrong_password_fails(self):
        hashed = generate_hashed_password("correct")
        assert not verify_hashed_password("wrong", hashed)

    def test_verify_with_str_hash(self):
        """DB 读出来可能是 str，验证兼容"""
        hashed_bytes = generate_hashed_password("mypass")
        hashed_str = hashed_bytes.decode("utf-8")
        assert verify_hashed_password("mypass", hashed_str)

    def test_same_password_different_hash(self):
        """同一密码两次 hash 结果不同（salt 随机）"""
        h1 = generate_hashed_password("same")
        h2 = generate_hashed_password("same")
        assert h1 != h2
        assert verify_hashed_password("same", h1)
        assert verify_hashed_password("same", h2)


# =============================================================================
# jwt_utils
# =============================================================================
class TestJWTUtils:
    def test_generate_and_parse(self):
        token = generate_jwt(1, "admin", "internal")
        payload = parse_jwt(token)
        assert payload["sub"] == "1"
        assert payload["role"] == "admin"
        assert payload["user_type"] == "internal"
        assert "exp" in payload
        assert "iat" in payload

    def test_token_with_different_roles(self):
        for role, utype in [
            ("customer", "external"),
            ("agent", "internal"),
            ("operator", "internal"),
            ("admin", "internal"),
        ]:
            token = generate_jwt(42, role, utype)
            payload = parse_jwt(token)
            assert payload["role"] == role
            assert payload["user_type"] == utype

    def test_expired_token_raises(self):
        """故意改 exp 为过去时间，验证 PyJWT 抛过期异常"""
        # JWT exp 最小粒度为秒，所以过期 1 秒就检验
        from datetime import datetime, timedelta, timezone

        expired_payload = {
            "sub": 1,
            "role": "admin",
            "user_type": "internal",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
        }
        private_key = Path("private_key.pem").read_text()
        token = jwt.encode(expired_payload, private_key, algorithm="RS256")

        with pytest.raises(jwt.ExpiredSignatureError):
            parse_jwt(token)

    def test_tampered_token_raises(self):
        """签名被篡改 → InvalidSignatureError → InvalidTokenError"""
        token = generate_jwt(1, "admin", "internal")
        tampered = token[:-5] + "xxxxx"
        with pytest.raises(jwt.InvalidTokenError):
            parse_jwt(tampered)

    @pytest.mark.skip(reason="session 级别密钥文件由 conftest 管理，测试中不可删除")
    def test_missing_keys_raises(self):
        Path("public_key.pem").unlink(missing_ok=True)
        with pytest.raises((FileNotFoundError, ValueError)):
            parse_jwt("fake_token")
