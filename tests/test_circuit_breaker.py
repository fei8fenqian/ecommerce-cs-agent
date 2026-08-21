import asyncio

import pytest

from infra.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_failure_threshold_opens_circuit_and_blocks_calls():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, open_seconds=30, clock=clock)

    await breaker.before_call()
    await breaker.record_failure()
    await breaker.before_call()
    await breaker.record_failure()
    await breaker.before_call()
    await breaker.record_failure()

    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.before_call()


@pytest.mark.asyncio
async def test_cooldown_allows_one_half_open_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, clock=clock)

    await breaker.before_call()
    await breaker.record_failure()
    clock.value = 30

    await breaker.before_call()
    assert breaker.state == CircuitState.HALF_OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.before_call()

    await breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.asyncio
async def test_failed_half_open_probe_reopens_circuit():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, clock=clock)

    await breaker.before_call()
    await breaker.record_failure()
    clock.value = 30
    await breaker.before_call()
    await breaker.record_failure()

    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        await breaker.before_call()


@pytest.mark.asyncio
async def test_half_open_allows_only_one_concurrent_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, clock=clock)

    await breaker.before_call()
    await breaker.record_failure()
    clock.value = 30

    results = await asyncio.gather(
        breaker.before_call(),
        breaker.before_call(),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, CircuitOpenError) for result in results) == 1
