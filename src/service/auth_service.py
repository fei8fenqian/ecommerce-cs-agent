import logging

from store.user_store import get_user_by_username
from utils.jwt_utils import generate_jwt
from utils.password_utils import verify_hashed_password

logger = logging.getLogger(__name__)


async def login(username: str, password: str):
    """验证账户密码，生成jwt，redis存

    返回 {token, user}
    """
    user_info: dict = await get_user_by_username(username)
    user_id = user_info.get("id")
    hashed_password = user_info.get("password_hash", "")
    if not hashed_password:
        ...
    hashed = await verify_hashed_password(password, hashed_password)
    if not hashed:
        ...
    token = generate_jwt(user_id)
    if not token:
        ...
