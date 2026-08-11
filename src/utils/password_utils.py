import logging

import bcrypt

logger = logging.getLogger(__name__)


def generate_hashed_password(password: str | bytes) -> bytes:
    if isinstance(password, str):
        password = password.encode("utf-8")
    hashed_password: bytes = bcrypt.hashpw(password, bcrypt.gensalt())
    return hashed_password


def verify_hashed_password(password: str | bytes, hashed_password: str | bytes) -> bool:
    if isinstance(password, str):
        password = password.encode("utf-8")
    if isinstance(hashed_password, str):
        hashed_password = hashed_password.encode("utf-8")
    return bcrypt.checkpw(password, hashed_password)
