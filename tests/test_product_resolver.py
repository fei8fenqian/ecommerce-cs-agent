import json
from types import SimpleNamespace

import pytest

from agent.product_resolver import ProductResolver

CANDIDATES = [
    {
        "product_id": "p-s60-12",
        "product_name": "vivo S60（12GB/512GB）",
        "display_title": "vivo S60（12GB/512GB）",
        "category": "phones",
        "price": 3999,
        "public_attributes": {"brand": "vivo", "ram": "12GB", "storage": "512GB"},
    },
    {
        "product_id": "p-s60-16",
        "product_name": "vivo S60（16GB/512GB）",
        "display_title": "vivo S60（16GB/512GB）",
        "category": "phones",
        "price": 4399,
        "public_attributes": {"brand": "vivo", "ram": "16GB", "storage": "512GB"},
    },
]


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.messages = None
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.messages = messages
        self.calls.append((messages, kwargs))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_product_resolver_cannot_select_outside_server_frame():
    llm = FakeLLM({"status": "selected", "selected_ref": "candidate_99"})
    result = await ProductResolver(llm).resolve(
        query="就这个",
        candidates=CANDIDATES,
        product_context={},
        history=[],
    )
    assert result.status == "unknown"
    prompt = llm.messages[-1]["content"]
    assert "p-s60-12" not in prompt
    assert "p-s60-16" not in prompt


@pytest.mark.asyncio
async def test_product_resolver_preserves_variant_ambiguity():
    llm = FakeLLM({"status": "ambiguous", "ambiguous_refs": ["candidate_1", "candidate_2"]})
    result = await ProductResolver(llm).resolve(
        query="那就vivo S60吧",
        candidates=CANDIDATES,
        product_context={},
        history=[],
    )
    assert result.status == "ambiguous"
    assert result.ambiguous_refs == ["candidate_1", "candidate_2"]


@pytest.mark.asyncio
async def test_product_resolver_gets_only_opaque_previous_selected_ref():
    llm = FakeLLM({"status": "selected", "selected_ref": "candidate_2"})
    result = await ProductResolver(llm).resolve(
        query="就这款，给我入口",
        candidates=CANDIDATES,
        product_context={},
        history=[],
        previous_selected=CANDIDATES[1],
        purpose="inspect",
    )
    assert result.status == "selected"
    prompt = json.loads(llm.messages[-1]["content"])
    assert prompt["product_context"]["previous_selected_ref"] == "candidate_2"
    assert "p-s60-16" not in llm.messages[-1]["content"]


@pytest.mark.asyncio
async def test_product_resolver_owns_recommendation_order_inside_server_frame():
    llm = FakeLLM(
        {
            "status": "unknown",
            "recommended_refs": ["candidate_2", "candidate_1", "candidate_99"],
        }
    )
    result = await ProductResolver(llm).resolve(
        query="8000以内，显卡最好，给我推荐几款",
        candidates=[
            {
                "product_id": "p-4060",
                "product_name": "候选4060",
                "display_title": "候选4060",
                "category": "laptops",
                "price": 6999,
                "public_attributes": {"gpu_chip": "RTX 4060", "cpu": "i9 13900HX"},
            },
            {
                "product_id": "p-4070",
                "product_name": "候选4070",
                "display_title": "候选4070",
                "category": "laptops",
                "price": 7088,
                "public_attributes": {"gpu_chip": "RTX 4070", "cpu": "i7 13700HX"},
            },
        ],
        product_context={"max_price_cents": 800000},
        history=[],
        purpose="recommend",
    )
    assert result.status == "unknown"
    assert result.recommended_refs == ["candidate_2", "candidate_1"]
    assert len(llm.calls) == 1
    assert llm.calls[0][1]["response_format"] == {"type": "json_object"}
    prompt = llm.messages[-1]["content"]
    assert '"gpu_chip":"RTX 4070"' in prompt
    assert "p-4070" not in prompt
    assert "推荐排序器" in llm.messages[0]["content"]


@pytest.mark.asyncio
async def test_product_resolver_collective_followup_reuses_only_current_verified_choice_refs():
    llm = FakeLLM(
        {
            "status": "unknown",
            "recommended_refs": ["candidate_2", "candidate_1", "candidate_99"],
        }
    )
    result = await ProductResolver(llm).resolve(
        query="这些对应链接呢",
        candidates=CANDIDATES,
        product_context={
            "choice_refs": ["candidate_2", "candidate_1", "candidate_99"],
        },
        history=[{"role": "assistant", "content": "刚才推荐了两款。"}],
        purpose="inspect",
    )

    assert result.status == "unknown"
    assert result.recommended_refs == ["candidate_2", "candidate_1"]
    payload = json.loads(llm.messages[-1]["content"])
    assert payload["product_context"]["previous_choice_refs"] == ["candidate_2", "candidate_1"]
    assert "candidate_99" not in payload["product_context"]["previous_choice_refs"]


@pytest.mark.asyncio
async def test_product_resolver_inspect_may_keep_verified_previous_selection():
    llm = FakeLLM({"status": "selected", "selected_ref": "candidate_2"})
    result = await ProductResolver(llm).resolve(
        query="能把这款详情页给我吗",
        candidates=CANDIDATES,
        product_context={},
        history=[],
        previous_selected=CANDIDATES[1],
        purpose="inspect",
    )
    assert result.status == "selected"
    assert result.selected_ref == "candidate_2"


class FailingLLM:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        from exceptions import LLMError

        self.calls += 1
        raise LLMError("provider unavailable")


@pytest.mark.asyncio
async def test_product_resolver_does_not_outer_retry_after_llmclient_failure():
    llm = FailingLLM()
    result = await ProductResolver(llm).resolve(
        query="就这款",
        candidates=CANDIDATES,
        product_context={},
        history=[],
        purpose="inspect",
    )
    assert result.status == "unknown"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_product_resolver_recommendation_degrades_to_server_frame_order_on_llm_failure():
    llm = FailingLLM()
    result = await ProductResolver(llm).resolve(
        query="6000左右推荐几台",
        candidates=CANDIDATES,
        product_context={},
        history=[],
        purpose="recommend",
    )
    assert result.status == "unknown"
    assert result.recommended_refs == ["candidate_1", "candidate_2"]
    assert llm.calls == 1
