import logging

import bcrypt

logger = logging.getLogger(__name__)


async def generate_hashed_password(password: str | bytes) -> bytes:
    if isinstance(password, str):
        password = password.encode("utf-8")
    hashed_password: bytes = bcrypt.hashpw(password, bcrypt.gensalt())
    return hashed_password


async def verify_hashed_password(password: str | bytes, hashed_password: bytes) -> bool:
    return bcrypt.checkpw(password, hashed_password)
