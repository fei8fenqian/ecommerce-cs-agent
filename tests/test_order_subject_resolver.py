from types import SimpleNamespace

import pytest

from agent.order_subject_resolver import resolve_order_subject


class _ResolverLLM:
    def __init__(self, content: str):
        self.content = content
        self.messages = []

    async def chat(self, messages, **kwargs):
        self.messages.append((messages, kwargs))
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_resolver_returns_candidate_ref_without_exposing_order_id():
    llm = _ResolverLLM('{"status":"resolved","selected_ref":"order_candidate_2","ambiguous_refs":[]}')
    candidates = [
        {"order_id": "SO-SSD", "product_name": "Acer NVMe", "component_category": "solid_state_drive"},
        {"order_id": "SO-RAM", "product_name": "金士顿内存", "component_category": "memory"},
    ]

    result = await resolve_order_subject(llm, "我想把内存退了", candidates)

    assert result.status == "resolved"
    assert result.selected_ref == "order_candidate_2"
    assert "SO-SSD" not in llm.messages[0][0][1]["content"]
    assert "SO-RAM" not in llm.messages[0][0][1]["content"]


@pytest.mark.asyncio
async def test_resolver_rejects_unknown_refs():
    llm = _ResolverLLM('{"status":"resolved","selected_ref":"SO-SSD","ambiguous_refs":[]}')

    result = await resolve_order_subject(
        llm,
        "我想退款",
        [{"order_id": "SO-SSD", "product_name": "固态硬盘"}],
    )

    assert result.status == "unknown"


@pytest.mark.asyncio
async def test_resolver_preserves_narrow_ambiguity():
    llm = _ResolverLLM(
        '{"status":"ambiguous","selected_ref":"","ambiguous_refs":["order_candidate_1","order_candidate_2"]}'
    )

    result = await resolve_order_subject(
        llm,
        "刚下单的金士顿",
        [
            {"order_id": "SO-KC", "product_name": "金士顿 KC3000", "recency_rank": 1},
            {"order_id": "SO-NV2", "product_name": "金士顿 NV2", "recency_rank": 8},
        ],
    )

    assert result.status == "ambiguous"
    assert result.ambiguous_refs == ("order_candidate_1", "order_candidate_2")


@pytest.mark.asyncio
async def test_resolver_receives_trusted_previous_subject_context_without_real_order_id():
    llm = _ResolverLLM('{"status":"resolved","selected_ref":"order_candidate_2","ambiguous_refs":[]}')
    candidates = [
        {"order_id": "SO-OLD", "product_name": "苹果 iPhone 16"},
        {"order_id": "SO-NEW", "product_name": "苹果 iPhone 17"},
    ]

    result = await resolve_order_subject(
        llm,
        "我还想处理另一部苹果手机",
        candidates,
        previous_subject_ref="order_candidate_1",
    )

    assert result.selected_ref == "order_candidate_2"
    prompt = llm.messages[0][0][1]["content"]
    import json

    payload = json.loads(prompt)
    assert payload["previous_subject_ref"] == "order_candidate_1"
    assert "subject_relation" not in payload
    assert "SO-OLD" not in prompt and "SO-NEW" not in prompt


@pytest.mark.asyncio
async def test_resolver_receives_recent_product_context_as_non_authoritative_public_hint():
    llm = _ResolverLLM('{"status":"resolved","selected_ref":"order_candidate_1","ambiguous_refs":[]}')
    candidates = [
        {"order_id": "SO-HW", "product_name": "HUAWEI Pura 80 Pro"},
        {"order_id": "SO-IP", "product_name": "Apple iPhone Air"},
    ]

    result = await resolve_order_subject(
        llm,
        "我想退款了，我之后还想买 iPhone",
        candidates,
        recent_product_context={
            "product": "HUAWEI Pura 80 Pro",
            "product_category": "phones",
            "product_id": "must-not-leak",
        },
    )

    assert result.status == "resolved"
    import json

    payload = json.loads(llm.messages[0][0][1]["content"])
    assert payload["recent_product_context"] == {
        "product": "HUAWEI Pura 80 Pro",
        "product_category": "phones",
    }
    assert "must-not-leak" not in llm.messages[0][0][1]["content"]
