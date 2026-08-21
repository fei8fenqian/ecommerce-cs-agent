"""异步依赖熔断器。

熔断器只管理状态，不负责调用外部依赖，也不决定哪些异常算失败。
调用方应在一次完整的重试流程结束后调用 record_success() 或 record_failure()。
"""

import asyncio
import time
from enum import StrEnum
from typing import Callable


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    """熔断器处于 OPEN，当前调用被直接拒绝。"""


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        open_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold 必须大于 0")
        if open_seconds <= 0:
            raise ValueError("open_seconds 必须大于 0")

        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self._clock = clock or time.monotonic
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_probe_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    async def before_call(self) -> None:
        """检查当前是否允许调用外部依赖。"""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return

            if self._state == CircuitState.OPEN:
                opened_at = self._opened_at
                if opened_at is None or self._clock() - opened_at < self.open_seconds:
                    raise CircuitOpenError("依赖熔断器已打开")
                self._state = CircuitState.HALF_OPEN

            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_probe_in_flight:
                    raise CircuitOpenError("依赖熔断器正在等待探测结果")
                self._half_open_probe_in_flight = True

    async def record_success(self) -> None:
        """记录一次完整调用成功，关闭熔断器并清零失败计数。"""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._opened_at = None
            self._half_open_probe_in_flight = False

    async def record_failure(self) -> None:
        """记录一次完整调用失败，达到阈值后打开熔断器。"""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._open()
                return

            if self._state == CircuitState.OPEN:
                return

            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self._open()

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._half_open_probe_in_flight = False
