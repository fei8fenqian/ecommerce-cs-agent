import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent.llm.llm_client import LLMClient, LLMError
from exceptions import DependencyUnavailableError
from infra.circuit_breaker import CircuitBreaker, CircuitState


class ProviderError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"provider status {status_code}")
        self.status_code = status_code


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def response(content: str = "ok") -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test-model", usage=None)


def stream_chunk(content: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def make_client(
    *,
    breaker: CircuitBreaker | None = None,
    timeout: float = 10.0,
    stream_timeout: float = 30.0,
    max_attempts: int = 2,
    backoff: float = 0.5,
):
    with patch("agent.llm.llm_client.AsyncOpenAI") as factory:
        client = LLMClient(
            api_key="test-key",
            timeout=timeout,
            stream_timeout=stream_timeout,
            max_attempts=max_attempts,
            retry_backoff_seconds=backoff,
            sdk_max_retries=0,
            circuit_breaker=breaker,
        )
    return client, factory


def set_create(client: LLMClient, side_effect) -> AsyncMock:
    create = AsyncMock(side_effect=side_effect)
    client._client.chat.completions.create = create
    return create


@pytest.mark.asyncio
async def test_async_openai_disables_sdk_retries():
    client, factory = make_client()

    assert client is not None
    assert factory.call_args.kwargs["max_retries"] == 0


@pytest.mark.asyncio
async def test_timeout_then_success_retries_once_and_records_success():
    breaker = CircuitBreaker(failure_threshold=3)
    client, _ = make_client(breaker=breaker, backoff=0.5)
    create = set_create(client, [asyncio.TimeoutError(), response("success")])

    with patch("agent.llm.llm_client.asyncio.sleep", new=AsyncMock()) as sleep:
        result = await client.chat([{"role": "user", "content": "hello"}])

    assert result.content == "success"
    assert create.await_count == 2
    sleep.assert_awaited_once_with(0.5)
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500])
async def test_rate_limit_and_server_errors_are_retried(status_code: int):
    client, _ = make_client(backoff=0)
    create = set_create(client, [ProviderError(status_code), response()])

    result = await client.chat([{"role": "user", "content": "hello"}])

    assert result.content == "ok"
    assert create.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_auth_errors_are_not_retried_or_counted(status_code: int):
    breaker = CircuitBreaker(failure_threshold=1)
    client, _ = make_client(breaker=breaker)
    create = set_create(client, [ProviderError(status_code)])

    with pytest.raises(LLMError) as raised:
        await client.chat([{"role": "user", "content": "hello"}])

    assert raised.value.status_code == status_code
    assert create.await_count == 1
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


@pytest.mark.asyncio
async def test_retry_exhaustion_records_one_circuit_failure():
    breaker = CircuitBreaker(failure_threshold=1)
    client, _ = make_client(breaker=breaker, max_attempts=2, backoff=0)
    create = set_create(client, [asyncio.TimeoutError(), asyncio.TimeoutError()])

    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "hello"}])

    assert create.await_count == 2
    assert breaker.state == CircuitState.OPEN
    assert breaker.failure_count == 1


@pytest.mark.asyncio
async def test_open_circuit_does_not_call_llm_and_returns_dependency_error():
    breaker = CircuitBreaker(failure_threshold=1)
    first_client, _ = make_client(breaker=breaker, max_attempts=1)
    set_create(first_client, [asyncio.TimeoutError()])

    with pytest.raises(LLMError):
        await first_client.chat([{"role": "user", "content": "hello"}])

    second_client, _ = make_client(breaker=breaker)
    create = set_create(second_client, [response()])
    with pytest.raises(DependencyUnavailableError):
        await second_client.chat([{"role": "user", "content": "hello"}])

    assert create.await_count == 0


@pytest.mark.asyncio
async def test_cooldown_allows_only_one_half_open_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, open_seconds=30, clock=clock)
    first_client, _ = make_client(breaker=breaker, max_attempts=1)
    set_create(first_client, [asyncio.TimeoutError()])

    with pytest.raises(LLMError):
        await first_client.chat([{"role": "user", "content": "hello"}])

    clock.value = 30
    probe_client, _ = make_client(breaker=breaker, max_attempts=1)
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe_create(**kwargs):
        probe_started.set()
        await release_probe.wait()
        return response()

    create = set_create(probe_client, probe_create)
    first_probe = asyncio.create_task(probe_client.chat([{"role": "user", "content": "one"}]))
    await probe_started.wait()
    second_probe = asyncio.create_task(probe_client.chat([{"role": "user", "content": "two"}]))
    await asyncio.sleep(0)
    release_probe.set()
    results = await asyncio.gather(first_probe, second_probe, return_exceptions=True)

    assert sum(isinstance(result, DependencyUnavailableError) for result in results) == 1
    assert sum(getattr(result, "content", None) == "ok" for result in results) == 1
    assert create.await_count == 1
    assert breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_timeout_limits_total_attempt_time():
    client, _ = make_client(timeout=0.01, max_attempts=2, backoff=0)

    async def slow_create(**kwargs):
        await asyncio.sleep(0.05)

    create = set_create(client, slow_create)
    started = time.perf_counter()

    with pytest.raises(LLMError):
        await client.chat([{"role": "user", "content": "hello"}])

    elapsed = time.perf_counter() - started
    assert create.await_count == 2
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_stream_failure_before_first_token_retries():
    client, _ = make_client(max_attempts=2, backoff=0)

    async def successful_stream():
        yield stream_chunk("success")

    create = set_create(client, [asyncio.TimeoutError(), successful_stream()])
    events = []
    async for event in client.chat_stream([{"role": "user", "content": "hello"}]):
        events.append(event)

    assert events == [{"type": "content", "content": "success"}]
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_stream_failure_after_first_token_does_not_retry():
    breaker = CircuitBreaker(failure_threshold=3)
    client, _ = make_client(breaker=breaker, max_attempts=2, backoff=0)

    async def broken_stream():
        yield stream_chunk("partial")
        raise asyncio.TimeoutError()

    create = set_create(client, [broken_stream()])
    events = []
    with pytest.raises(DependencyUnavailableError):
        async for event in client.chat_stream([{"role": "user", "content": "hello"}]):
            events.append(event)

    assert events == [{"type": "content", "content": "partial"}]
    assert create.await_count == 1
    assert breaker.failure_count == 1


@pytest.mark.asyncio
async def test_stream_timeout_is_applied_to_the_whole_stream():
    client, _ = make_client(stream_timeout=0.01, max_attempts=1)

    async def slow_stream():
        await asyncio.sleep(0.05)
        yield stream_chunk("too late")

    create = set_create(client, [slow_stream()])
    with pytest.raises(DependencyUnavailableError):
        async for _ in client.chat_stream([{"role": "user", "content": "hello"}]):
            pass

    assert create.await_count == 1
