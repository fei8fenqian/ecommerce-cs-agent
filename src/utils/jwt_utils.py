import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

logger = logging.getLogger(__name__)


def generate_jwt(user_id: int):
    payload: dict[str, Any] = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
    }
    private_key: str = Path("private_key.pem").read_text()
    token: str = jwt.encode(payload, private_key, algorithm="RS256")
    return token


def parse_jwt(token: str) -> dict[str, Any]:
    payload: dict[str, Any] = jwt.decode(token, Path("public_key.pem").read_text(), algorithms=["RS256"])
    return payload
