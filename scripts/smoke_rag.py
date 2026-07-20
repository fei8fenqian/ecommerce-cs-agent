"""
scripts/smoke_rag.py — RAG + LLM 端到端冒烟测试

跑通"用户问题 → 检索 → LLM 生成回答"的完整链路。
Agent Loop 和 Tool Calling 不参与，这是最简版，验证两件事：
  1. 检索能召回相关内容
  2. LLM 能基于检索结果生成合理回答

用法：
  python scripts/smoke_rag.py                        # 跑全部 5 个示例问题
  python scripts/smoke_rag.py --query "X1 Carbon 接口"  # 自定义问题
"""

import asyncio
import sys
from pathlib import Path

# 把项目根目录加入 sys.path，让脚本能 import src/ 下的模块
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config import settings
from core.llm_client import LLMClient
from core.retrieve import hybrid_search

SYSTEM_PROMPT = """你是"极客数码"的 AI 客服助手。请遵守以下规则：

1. 回答严格基于「参考信息」中的内容，不要编造参数
2. 参考信息中没有的内容，诚实告知用户"暂时查不到，建议咨询人工"
3. 语气简洁专业，不废话
4. 用户要对比产品时，列出关键参数差异"""

# 5 个覆盖不同场景的示例问题
SAMPLES = [
    {
        "query": "联想拯救者用的什么CPU？",
        "table": "laptop_products",
        "label": "精确参数查询（笔记本）",
    },
    {
        "query": "华为MateBook 14内存多大？",
        "table": "laptop_products",
        "label": "精确参数查询（笔记本）",
    },
    {
        "query": "笔记本退货什么流程？",
        "table": "knowledge_chunks",
        "label": "售后政策查询",
    },
    {
        "query": "拯救者和微星泰坦哪个性能强？",
        "table": "laptop_products",
        "label": "商品对比",
    },
    {
        "query": "学生党编程买什么笔记本？",
        "table": "knowledge_chunks",
        "label": "选购建议",
    },
]


async def ask(query: str, table: str = "laptop_products", label: str = "") -> str:
    """
    一次完整的 RAG + LLM 调用。

    1. 检索：hybrid_search 返回 Top-5
    2. 拼 prompt：系统提示 + 检索结果 + 用户问题
    3. 调 LLM 生成回答
    """
    client = LLMClient(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=settings.llm_model,
    )

    # -- 检索 --
    docs = hybrid_search(query, table=table, top_k=5)
    if not docs:
        context = "（未找到相关内容）"
    else:
        lines = [f"[来源: {d.get('title', '?')}] {d.get('content', '')[:300]}" for d in docs]
        context = "\n---\n".join(lines)

    # -- 调 LLM --
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"参考信息：\n{context}\n\n用户问题：{query}"},
    ]

    response = await client.chat(messages)

    # -- 输出 --
    if label:
        print(f"\n{'=' * 60}")
        print(f"场景: {label}")
    print(f"问: {query}")
    print(f"检索表: {table}  |  Top-5 条数: {len(docs)}")
    print(f"答: {response.content}")
    print(f"延迟: {response.latency_ms:.0f}ms  |  tokens: {response.usage.total_tokens}")
    return response.content or ""


async def main(query: str | None = None):
    """入口：自定义问题跑 1 次，否则跑全部 5 个示例"""
    if query:
        await ask(query, table="laptop_products")
        return

    for sample in SAMPLES:
        await ask(sample["query"], table=sample["table"], label=sample["label"])


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RAG + LLM 端到端冒烟测试")
    parser.add_argument("--query", type=str, default=None, help="自定义问题")
    args = parser.parse_args()

    asyncio.run(main(query=args.query))
