"""Plan-and-Execute Agent 冒烟测试"""

import asyncio

from agent.engines.plan_execute import PlanAndExecuteAgent
from agent.llm.llm_client import LLMClient
from agent.tools.search_component import SearchComponent
from agent.tools_registry import ToolRegistry
from config import settings
from infra.db_pool import init_pool


async def main():
    # 1. 初始化
    init_pool()
    registry = ToolRegistry()
    registry.register(SearchComponent())
    from agent.tools.create_ticket import CreateTicket
    from agent.tools.search_product import SearchProduct
    from agent.tools.track_order import TrackOrder

    registry.register(SearchProduct())
    registry.register(TrackOrder())
    registry.register(CreateTicket())

    llm = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )

    agent = PlanAndExecuteAgent(llm, registry)

    # 2. 测试配机
    print("=" * 60)
    print("测试: build_pc — 5000 预算游戏主机")
    print("=" * 60)
    result = await agent.run("5000 预算 主要打 3A 游戏", scenario="build_pc")
    print(f"\nPlan ({len(result.get('plan', []))} 步骤):")
    for step in result.get("plan", []):
        comp = step.get("component", "?")
        q = step.get("query", "?")
        deps = step.get("depends_on", [])
        print(f"  {step['id']}. {comp}: {q} (依赖: {deps})")
    print(f"\nJudge: passed={result.get('judge_passed')}, reason={result.get('judge_reason', '')}")
    print(f"\nAnswer:\n{result.get('answer', '无')}")

    # 3. 测试诊断
    print("\n" + "=" * 60)
    print("测试: troubleshoot — 笔记本无法开机")
    print("=" * 60)
    result2 = await agent.run("我的联想 Y9000P 无法开机，电源灯不亮", scenario="troubleshoot")
    print(f"\nPlan ({len(result2.get('plan', []))} 步骤):")
    for step in result2.get("plan", []):
        print(f"  {step['id']}. {step.get('action', '?')}: {step.get('purpose', '?')}")
    print(f"\nAnswer:\n{result2.get('answer', '无')}")


if __name__ == "__main__":
    asyncio.run(main())
