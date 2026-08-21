import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from exceptions import DependencyUnavailableError, LLMError
from infra.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from infra.metrics import LLM_CIRCUIT_OPEN, LLM_DEGRADED, LLM_RETRY, LLM_TIMEOUT

logger = logging.getLogger(__name__)


def _observe_llm(counter, operation: str) -> None:
    counter.labels(dependency="llm", operation=operation).inc()


def _is_timeout_error(exc: Exception) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException))


# 把 OpenAI API 返回值转成自定义的类型


@dataclass
class TokenUsage:
    """一次 LLM 调用的 token 消耗"""

    prompt_tokens: int = 0  # 输入用的 token 数
    completion_tokens: int = 0  # LLM 输出的 token 数
    total_tokens: int = 0

    @classmethod
    def from_openai(cls, usage: Any) -> "TokenUsage":
        if usage is None:
            return cls()
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
        )


@dataclass
class ToolCall:
    """LLM 返回的工具调用请求。"""

    id: str  # 工具调用的唯一 ID
    name: str  # 工具名
    arguments: dict[str, Any] = field(default_factory=dict)  # 参数


@dataclass
class LLMResponse:
    """一次 LLM 调用的完整返回。

    content 和 tool_calls 至少有一个为 None：
    - 普通对话：content 有值，tool_calls 为 None
    - 触发工具：tool_calls 有值，content 可能为 None
    - 先说话再调工具：两个都有值
    """

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: float = 0.0  # 从发请求到收到回答的耗时
    finish_reason: str = ""  # 终止原因 "stop" / "tool_calls" / "length"

    @property
    def has_tool_calls(self) -> bool:
        return self.tool_calls is not None and len(self.tool_calls) > 0


class LLMClient:
    """
    通用 LLM 客户端，兼容所有 OpenAI 格式的 API，异步调用。

    参数全部外部注入——换厂商只改 config.py，不动这个文件：
    api_key:  API 密钥
    base_url: API 地址（默认 DeepSeek）
    model:    模型名（默认 deepseek-chat）
    timeout:  单次请求超时秒数
    max_attempts: 整个调用最多尝试次数
    retry_backoff_seconds: 重试退避基数
    sdk_max_retries: SDK 内部重试次数
    stream_timeout: 单次流式调用最长时间
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 10.0,
        max_attempts: int = 2,
        retry_backoff_seconds: float = 0.5,
        sdk_max_retries: int = 0,
        stream_timeout: float = 30.0,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        """
        从 config.settings 读配置，创建 OpenAI 客户端。
        base_url 指向 DeepSeek。
        """
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if max_attempts <= 0:
            raise ValueError("max_attempts 必须大于 0")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds 不能小于 0")
        if sdk_max_retries < 0:
            raise ValueError("sdk_max_retries 不能小于 0")
        if stream_timeout <= 0:
            raise ValueError("stream_timeout 必须大于 0")

        self.model = model
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.sdk_max_retries = sdk_max_retries
        self.stream_timeout = stream_timeout
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=sdk_max_retries,
        )

    # chat() — 异步调用，Agent Loop 用这个
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        异步调用 LLM，带自动重试。

        Agent 场景用 temperature=0.0（确定性输出），
        不要让 LLM 有"创意"——查库存就是查库存，别自己编。

        参数：
        messages: 对话历史，格式 [{"role": "user", "content": "..."}, ...]
        tools:    OpenAI function calling 格式的工具列表
        temperature: 0.0=确定，0.7=随机
        max_tokens: 输出上限

        返回：
        LLMResponse——统一的数据结构，Agent Loop 下一步用它决策
        """

        await self._before_call("chat")

        logger.info(
            "LLM request start",
            extra={
                "model": self.model,
                "messages_count": len(messages),
                "has_tools": tools is not None,
            },
        )
        t_start = time.perf_counter()

        last_exception: Exception | None = None
        last_status_code: int | None = None

        for attempt in range(self.max_attempts):
            try:
                response = await asyncio.wait_for(
                    cast(Any, self._client.chat.completions.create)(
                        messages=messages,
                        model=self.model,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    ),
                    timeout=self.timeout,
                )

                try:
                    result = self._parse_response(response=response)
                except Exception as exc:
                    logger.error("LLM response parse failed")
                    raise LLMError(
                        "智能服务返回格式异常",
                        retry_count=attempt,
                        last_response="response_parse_error",
                    ) from exc

                result.latency_ms = (time.perf_counter() - t_start) * 1000
                await self.circuit_breaker.record_success()

                logger.info(
                    "LLM request done",
                    extra={
                        "model": self.model,
                        "latency_ms": round(result.latency_ms),
                        "tokens": {
                            "prompt": result.usage.prompt_tokens,
                            "completion": result.usage.completion_tokens,
                            "total": result.usage.total_tokens,
                        },
                        "finish_reason": result.finish_reason,
                    },
                )
                return result

            except LLMError:
                raise
            except Exception as e:
                last_exception = e
                last_status_code = _extract_status_code(e)

                if _is_timeout_error(e):
                    _observe_llm(LLM_TIMEOUT, "chat")

                if not _is_retryable_error(e):
                    raise LLMError(
                        "智能服务请求失败",
                        retry_count=attempt,
                        status_code=last_status_code,
                        last_response="non_retryable_provider_error",
                    ) from e

                if attempt + 1 < self.max_attempts:
                    wait = self.retry_backoff_seconds * (2**attempt)
                    _observe_llm(LLM_RETRY, "chat")
                    logger.warning(
                        "LLM retry",
                        extra={
                            "retry_count": attempt + 1,
                            "wait_s": wait,
                        },
                    )
                    await asyncio.sleep(wait)
                    continue

                logger.error(
                    "LLM request failed after all retries",
                    extra={"retry_count": self.max_attempts - 1},
                )
                await self._record_failure("chat")
                _observe_llm(LLM_DEGRADED, "chat")
                raise LLMError(
                    "智能服务暂时不可用",
                    retry_count=self.max_attempts - 1,
                    status_code=last_status_code,
                    last_response="retry_exhausted",
                ) from e

        raise LLMError(
            "智能服务暂时不可用",
            retry_count=self.max_attempts - 1,
            status_code=last_status_code,
            last_response="retry_exhausted",
        ) from last_exception

    async def _before_call(self, operation: str) -> None:
        try:
            await self.circuit_breaker.before_call()
        except CircuitOpenError as exc:
            _observe_llm(LLM_CIRCUIT_OPEN, operation)
            _observe_llm(LLM_DEGRADED, operation)
            raise DependencyUnavailableError("智能服务暂时不可用") from exc

    async def _record_failure(self, operation: str) -> None:
        await self.circuit_breaker.record_failure()
        if self.circuit_breaker.state == CircuitState.OPEN:
            _observe_llm(LLM_CIRCUIT_OPEN, operation)

    def _parse_response(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls: list[ToolCall] | None = None
        if message.tool_calls:
            tool_calls = []
            for tool_call in message.tool_calls:
                args = tool_call.function.arguments
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                tool_calls.append(
                    ToolCall(
                        id=tool_call.id or "",
                        name=tool_call.function.name,
                        arguments=args,
                    )
                )

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            model=response.model or self.model,
            usage=TokenUsage.from_openai(response.usage),
            finish_reason=choice.finish_reason or "unknown",
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ):
        await self._before_call("stream")

        logger.info(
            "LLM SSE request start",
            extra={
                "model": self.model,
                "messages_count": len(messages),
                "has_tools": tools is not None,
            },
        )

        for attempt in range(self.max_attempts):
            stream_started = False
            tool_buf: dict[int, dict] = {}
            try:
                # 这个 timeout 覆盖建连和整个流的读取过程。
                async with asyncio.timeout(self.stream_timeout):
                    response = await cast(Any, self._client.chat.completions.create)(
                        messages=messages,
                        model=self.model,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=True,
                    )

                    async for chunk in response:
                        delta = chunk.choices[0].delta if chunk.choices else None
                        if delta is None:
                            continue

                        # 收到任何有效流片段后，重试会造成重复请求，因此禁止重试。
                        stream_started = True

                        if delta.content:
                            yield {"type": "content", "content": delta.content}

                        if delta.tool_calls:
                            for tool_call in delta.tool_calls:
                                index = tool_call.index
                                if index not in tool_buf:
                                    tool_buf[index] = {"id": "", "name": "", "arguments": ""}
                                buf = tool_buf[index]
                                if tool_call.id:
                                    buf["id"] = tool_call.id
                                if tool_call.function:
                                    if tool_call.function.name:
                                        buf["name"] = tool_call.function.name
                                    if tool_call.function.arguments:
                                        buf["arguments"] += tool_call.function.arguments

                if tool_buf:
                    tool_calls: list[ToolCall] = []
                    for idx in sorted(tool_buf):
                        buf = tool_buf[idx]
                        try:
                            args = json.loads(buf["arguments"])
                        except json.JSONDecodeError:
                            args = {}
                        tool_calls.append(ToolCall(buf["id"], buf["name"], args))
                    yield {"type": "tool_calls", "tool_calls": tool_calls}

                await self.circuit_breaker.record_success()
                return

            except asyncio.CancelledError:
                raise
            except LLMError:
                raise
            except Exception as exc:
                status_code = _extract_status_code(exc)
                retryable = _is_retryable_error(exc)

                if _is_timeout_error(exc):
                    _observe_llm(LLM_TIMEOUT, "stream")

                if stream_started:
                    if retryable:
                        await self._record_failure("stream")
                    _observe_llm(LLM_DEGRADED, "stream")
                    raise DependencyUnavailableError("智能服务暂时不可用") from exc

                if not retryable:
                    raise LLMError(
                        "智能服务请求失败",
                        retry_count=attempt,
                        status_code=status_code,
                        last_response="non_retryable_provider_error",
                    ) from exc

                if attempt + 1 < self.max_attempts:
                    wait = self.retry_backoff_seconds * (2**attempt)
                    _observe_llm(LLM_RETRY, "stream")
                    await asyncio.sleep(wait)
                    continue

                await self._record_failure("stream")
                _observe_llm(LLM_DEGRADED, "stream")
                raise DependencyUnavailableError("智能服务暂时不可用") from exc


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("http_status", "status_code"):
        val = getattr(exc, attr, None)
        if val is not None:
            return int(val)
    return None


def _is_retryable_error(exc: Exception) -> bool:
    """只把依赖暂时不可用类错误纳入应用层重试。"""
    if isinstance(
        exc,
        (
            asyncio.TimeoutError,
            TimeoutError,
            APIConnectionError,
            APITimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    ):
        return True

    status_code = _extract_status_code(exc)
    return status_code == 429 or (status_code is not None and 500 <= status_code < 600)
