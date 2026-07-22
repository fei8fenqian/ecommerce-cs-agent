"""tests/test_intent_router.py — 意图路由单元测试"""

import pytest

from core.intent_router import Intent, IntentRouter
from core.llm_client import LLMResponse, TokenUsage


# =============================================================================
# Mock LLMClient — 不发起真实 API 调用
# =============================================================================
class _MockLLM:
    """返回指定 content 的假 LLM 客户端，只实现 chat()"""

    def __init__(self, content: str):
        self._content = content
        self.model = "mock"

    async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        return LLMResponse(
            content=self._content,
            model="mock",
            usage=TokenUsage(),
            finish_reason="stop",
        )


def _router(response_content: str) -> IntentRouter:
    """工厂函数：用 mock LLM 创建 IntentRouter"""
    return IntentRouter(llm=_MockLLM(response_content))


# =============================================================================
# Intent 数据类
# =============================================================================
class TestIntent:
    def test_defaults(self):
        intent = Intent()
        assert intent.target == ""
        assert intent.table == ""
        assert intent.query == ""
        assert intent.confidence == 0.0

    def test_full(self):
        intent = Intent(target="rag", table="laptop_products", query="xxx", confidence=0.9)
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.query == "xxx"
        assert intent.confidence == 0.9


# =============================================================================
# IntentRouter.route 正常场景
# =============================================================================
class TestRouteNormal:
    @pytest.mark.asyncio
    async def test_rag_target(self):
        router = _router('{"target": "rag", "table": "laptop_products", "confidence": 0.95}')
        intent = await router.route("推荐一款笔记本")
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.confidence == 0.95

    @pytest.mark.asyncio
    async def test_agent_target(self):
        router = _router('{"target": "agent", "table": "", "confidence": 0.92}')
        intent = await router.route("拯救者还有货吗")
        assert intent.target == "agent"
        assert intent.table == ""  # agent 强制置空

    @pytest.mark.asyncio
    async def test_ticket_target(self):
        router = _router('{"target": "ticket", "table": "", "confidence": 0.88}')
        intent = await router.route("我要退款")
        assert intent.target == "ticket"
        assert intent.table == ""  # ticket 强制置空

    @pytest.mark.asyncio
    async def test_rag_with_knowledge_chunks(self):
        router = _router('{"target": "rag", "table": "knowledge_chunks", "confidence": 0.90}')
        intent = await router.route("退货需要什么条件")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_rag_with_phone_products(self):
        router = _router('{"target": "rag", "table": "phone_products", "confidence": 0.93}')
        intent = await router.route("iPhone 15 参数")
        assert intent.table == "phone_products"


# =============================================================================
# IntentRouter.route 降级 / 容错
# =============================================================================
class TestRouteFallback:
    @pytest.mark.asyncio
    async def test_low_confidence_falls_back_to_rag(self):
        router = _router('{"target": "agent", "table": "", "confidence": 0.3}')
        intent = await router.route("模糊问题")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back_to_rag(self):
        router = _router("not json at all")
        intent = await router.route("随便")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"
        assert intent.confidence == 0.0

    @pytest.mark.asyncio
    async def test_empty_content_falls_back_to_rag(self):
        router = _router("")
        intent = await router.route("空响应")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"

    @pytest.mark.asyncio
    async def test_invalid_target_falls_back_to_rag(self):
        router = _router('{"target": "unknown", "table": "", "confidence": 0.8}')
        intent = await router.route("奇怪的问题")
        assert intent.target == "rag"

    @pytest.mark.asyncio
    async def test_agent_with_table_gets_cleared(self):
        """即使 LLM 给 agent 写了 table，路由后也应清空"""
        router = _router('{"target": "agent", "table": "laptop_products", "confidence": 0.9}')
        intent = await router.route("查库存")
        assert intent.target == "agent"
        assert intent.table == ""

    @pytest.mark.asyncio
    async def test_rag_invalid_table_falls_back_to_knowledge(self):
        router = _router('{"target": "rag", "table": "weird_table", "confidence": 0.85}')
        intent = await router.route("问题")
        assert intent.target == "rag"
        assert intent.table == "knowledge_chunks"


# =============================================================================
# IntentRouter.route markdown 包裹
# =============================================================================
class TestRouteMarkdown:
    @pytest.mark.asyncio
    async def test_json_wrapped_in_markdown(self):
        router = _router(
            '```json\n{"target": "rag", "table": "laptop_products", "confidence": 0.97}\n```'
        )
        intent = await router.route("推荐笔记本")
        assert intent.target == "rag"
        assert intent.table == "laptop_products"
        assert intent.confidence == 0.97

    @pytest.mark.asyncio
    async def test_json_wrapped_in_generic_markdown(self):
        router = _router('```\n{"target": "ticket", "table": "", "confidence": 0.85}\n```')
        intent = await router.route("投诉")
        assert intent.target == "ticket"
