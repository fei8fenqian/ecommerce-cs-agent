"""
scripts/smoke_agent.py — 全链路冒烟测试

跑通: 用户问题 → 意图路由 → RAG 或 Agent Loop → 回答

用法:
python scripts/smoke_agent.py
python scripts/smoke_agent.py --query "拯救者有货吗"
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from agent.loop import AgentLoop
from agent.tools.check_stock import CheckStock
from agent.tools.create_ticket import CreateTicket
from agent.tools.search_product import SearchProduct
from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolRegistry
from config import settings
from core.intent_router import IntentRouter
from core.llm_client import LLMClient
from core.retrieve import hybrid_search


def build_context(docs: list[dict]) -> str:
    """把检索结果拼成 Agent 能用的上下文字符串"""
    if not docs:
        return "（未找到相关内容）"
    lines = []
    for d in docs[:5]:
        title = d.get("title", "?")
        content = d.get("content", "")[:300]
        lines.append(f"[{title}] {content}")
    return "\n---\n".join(lines)


async def main(query: str | None = None):
    # 1. 基础设施
    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )

    registry = ToolRegistry()
    registry.register(SearchProduct())
    registry.register(CheckStock())
    registry.register(TrackOrder())
    registry.register(CreateTicket())

    router = IntentRouter(llm)
    agent = AgentLoop(llm, registry)

    # 2. 测试用例
    tests = [
        {"query": "联想拯救者用的什么CPU？", "label": "RAG: 参数查询"},
        {"query": "笔记本退货什么流程？", "label": "RAG: 政策查询"},
        {"query": "拯救者有货吗？", "label": "Agent: 库存查询"},
        {"query": "帮我查一下订单 ORD2026070100138", "label": "Agent: 订单追踪"},
        {"query": "17146160937 这个手机号下有什么订单？", "label": "Agent: 手机号查单"},
    ]

    queries = [{"query": query, "label": "自定义"}] if query else tests

    for case in queries:
        print(f"\n{'=' * 60}")
        print(f"场景: {case['label']}")
        print(f"用户: {case['query']}")

        # 3. 意图分类
        intent = await router.route(case["query"])
        print(f"意图: {intent.target} | 表: {intent.table} | 置信度:{intent.confidence:.2f}")

        # 4. 检索（仅 rag 需要；agent/ticket 走工具）
        if intent.target == "rag":
            docs = hybrid_search(intent.query, table=intent.table, top_k=5)
            # 直接 RAG 回答（不需要工具）
            context = build_context(docs)
            messages = [
                {"role": "system", "content": agent.system_prompt},
                {
                    "role": "user",
                    "content": f"参考信息:\n{context}\n\n用户问题: {intent.query}",
                },
            ]
            response = await llm.chat(messages)
            print(f"回答: {response.content}")

        elif intent.target == "agent":
            # Agent Loop + 工具（不需要 RAG 检索）
            result = await agent.run(intent.query)
            print(f"步数: {result.total_steps} | tokens:{result.total_tokens}")
            print(f"回答: {result.answer}")

        elif intent.target == "ticket":
            # Agent Loop → 可能调 create_ticket
            result = await agent.run(intent.query)
            print(f"步数: {result.total_steps} | tokens: {result.total_tokens}")
            print(f"回答: {result.answer}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="全链路冒烟测试")
    parser.add_argument("--query", type=str, default=None, help="自定义问题")
    args = parser.parse_args()
    asyncio.run(main(query=args.query))
