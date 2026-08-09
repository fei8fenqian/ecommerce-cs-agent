"""scripts/smoke_llm.py — LLMClient 冒烟测试"""

import asyncio

from agent.llm.llm_client import LLMClient
from config import settings


async def main():
    client = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )
    resp = await client.chat(
        messages=[{"role": "user", "content": "你好"}],
    )
    print("content:", resp.content)
    print("latency:", resp.latency_ms, "ms")
    print("usage:", resp.usage)


if __name__ == "__main__":
    asyncio.run(main())
