import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt

logger = logging.getLogger(__name__)


async def generate_jwt(user_id: int):
    payload: dict = {
        "sub": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        "iat": datetime.now(timezone.utc),
    }
    private_key: str = Path("private_key.pem").read_text()
    token: str = jwt.encode(payload, private_key, algorithm="RS256")
    return token
