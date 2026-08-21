"""Redis-backed fixed-window rate limiting."""

import math
import time
from dataclasses import dataclass

from redis.asyncio import Redis


class RateLimitDependencyError(RuntimeError):
    """Redis 限流依赖不可用。"""


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    current: int
    limit: int
    retry_after: int


_INCREMENT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


def build_rate_limit_key(
    *,
    route: str,
    subject_type: str,
    subject: str,
    now: float,
    window_seconds: int,
) -> str:
    window = int(now) // window_seconds
    return f"rate_limit:{route}:{subject_type}:{subject}:{window}"


async def check_rate_limit(
    redis: Redis,
    *,
    key: str,
    limit: int,
    window_seconds: int,
    now: float | None = None,
) -> RateLimitResult:
    """原子递增固定窗口计数，并返回是否允许本次请求。"""
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if window_seconds <= 0:
        raise ValueError("window_seconds 必须大于 0")

    request_time = time.time() if now is None else now
    try:
        raw_current = await redis.eval(
            _INCREMENT_SCRIPT,
            1,
            key,
            window_seconds,
        )
    except Exception as exc:
        raise RateLimitDependencyError("Redis 限流操作失败") from exc

    current = int(raw_current)
    window_end = (int(request_time) // window_seconds + 1) * window_seconds
    retry_after = max(1, math.ceil(window_end - request_time))
    return RateLimitResult(
        allowed=current <= limit,
        current=current,
        limit=limit,
        retry_after=retry_after,
    )
