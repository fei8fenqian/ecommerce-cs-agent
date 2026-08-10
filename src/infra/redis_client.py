import logging

import redis.asyncio as aioredis
from redis.asyncio import Redis

from config import settings

logger = logging.getLogger(__name__)
_redis: Redis | None = None


def init_redis(redis_url: str | None = None):
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(redis_url or settings.redis_url)


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis 未初始化，请先调用 init_redis()")
    return _redis


async def health_check() -> bool:
    """启动时检查 Redis 连通性。失败不阻塞启动，只打日志。"""
    global _redis
    try:
        _redis = get_redis()
        await _redis.ping()
        logger.info(
            "Redis 连接成功: %s",
            _redis.connection_pool.connection_kwargs.get("host", "?"),
        )
        return True
    except Exception as e:
        logger.warning("Redis 连接失败: %s，会话功能将不可用", e)
        return False


async def close_redis() -> None:
    """关闭 Redis 连接池。"""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
