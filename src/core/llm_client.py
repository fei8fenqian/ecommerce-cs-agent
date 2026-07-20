import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from exceptions import LLMError

logger = logging.getLogger(__name__)


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
    """LLM 请求调用工具。

    Phase 3 Agent Loop 里，LLM 返回这个而不是文本时，
    Loop 就知道该调工具了。
    """

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
    timeout:  超时秒数（默认 30）
    max_retries: 最多重试几次（默认 3）
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        """
        从 config.settings 读配置，创建 OpenAI 客户端。
        base_url 指向 DeepSeek。
        """
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    # chat() — 同步调用，Agent Loop 用这个
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        同步调用 LLM，带自动重试。

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

        for attempt in range(self.max_retries):
            try:
                response = await self._client.chat.completions.create(
                    messages=messages,
                    model=self.model,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                result = self._parse_response(response=response)
                result.latency_ms = (time.perf_counter() - t_start) * 1000

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

            except Exception as e:
                last_exception = e
                last_status_code = _extract_status_code(e)

                # 401 没认证, 403 没权限
                if last_status_code in (401, 403):
                    logger.error(
                        "LLM auth error, not retrying",
                        extra={"status_code": last_status_code},
                    )
                    raise LLMError(
                        f"API 鉴权失败 (HTTP {last_status_code})",
                        retry_count=attempt,
                        status_code=last_status_code,
                        last_response=str(e),
                    )

                if attempt < self.max_retries:
                    wait = 2**attempt
                    logger.warning(
                        "LLM retry",
                        extra={
                            "retry_count": attempt + 1,
                            "wait_s": wait,
                            "error": str(e)[:200],
                        },
                    )
                    await asyncio.sleep(wait)
                    continue

                logger.error(
                    "LLM request failed after all retries",
                    extra={"retry_count": self.max_retries},
                )

        raise LLMError(
            f"LLM 调用失败，已重试 {self.max_retries} 次: {last_exception}",
            retry_count=self.max_retries,
            status_code=last_status_code,
            last_response=str(last_exception),
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls = None
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


def _extract_status_code(exc: Exception) -> int | None:
    for attr in ("http_status", "status_code"):
        val = getattr(exc, attr, None)
        if val is not None:
            return int(val)
    return None
