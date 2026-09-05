"""Candidate-binding harness for the migrated semantic subject path.

The fake LLM supplies only candidate refs; authoritative order ids live only in
this server-owned fixture.  The test therefore exercises the protocol boundary,
not natural-language understanding quality.
"""

import asyncio
import json
from types import SimpleNamespace

from agent.order_subject_resolver import resolve_order_subject

ORDERS = [
    {"order_id": "SO-HUAWEI-X-NEW", "product_name": "HUAWEI Pura X 1TB", "amount_cents": 999900, "recency_rank": 1},
    {"order_id": "SO-HUAWEI-80", "product_name": "华为 Pura 80 Pro 512GB", "amount_cents": 699900, "recency_rank": 2},
    {"order_id": "SO-HUAWEI-M70", "product_name": "HUAWEI Mate 70 512GB", "amount_cents": 599900, "recency_rank": 5},
    {
        "order_id": "SO-IPHONE-AIR-NEW",
        "product_name": "Apple iPhone Air 512GB",
        "amount_cents": 999900,
        "recency_rank": 3,
    },
    {
        "order_id": "SO-IPHONE-AIR-OLD",
        "product_name": "Apple iPhone Air 512GB",
        "amount_cents": 999900,
        "recency_rank": 8,
    },
    {"order_id": "SO-IPHONE16", "product_name": "Apple iPhone 16 256GB", "amount_cents": 699900, "recency_rank": 6},
    {"order_id": "SO-XIAOMI14", "product_name": "Xiaomi 14 512GB", "amount_cents": 399900, "recency_rank": 4},
    {"order_id": "SO-SSD", "product_name": "Samsung SSD 1TB", "amount_cents": 89900, "recency_rank": 7},
]


class FakeResolverLLM:
    def __init__(self, payload):
        self.payload = payload
        self.last_prompt = None

    async def chat(self, messages, **kwargs):
        self.last_prompt = json.loads(messages[-1]["content"])
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


async def _run() -> None:
    # Broad Huawei description remains ambiguous: no newest/default selection.
    llm = FakeResolverLLM(
        {
            "status": "ambiguous",
            "selected_ref": "",
            "ambiguous_refs": ["order_candidate_1", "order_candidate_2", "order_candidate_3"],
        }
    )
    r = await resolve_order_subject(llm, "华为手机", ORDERS)
    assert r.status == "ambiguous"
    assert r.ambiguous_refs == ("order_candidate_1", "order_candidate_2", "order_candidate_3")

    # Complex semantic summary can bind one authenticated candidate.
    llm = FakeResolverLLM({"status": "resolved", "selected_ref": "order_candidate_2", "ambiguous_refs": []})
    r = await resolve_order_subject(llm, "昨天买的华为，但不是 Pura X", ORDERS)
    assert r.status == "resolved"
    assert ORDERS[int(r.selected_ref.rsplit("_", 1)[1]) - 1]["order_id"] == "SO-HUAWEI-80"

    # Same-model Apple orders preserve ambiguity.
    iphone_air = [ORDERS[3], ORDERS[4]]
    llm = FakeResolverLLM(
        {"status": "ambiguous", "selected_ref": "", "ambiguous_refs": ["order_candidate_1", "order_candidate_2"]}
    )
    r = await resolve_order_subject(llm, "iPhone Air 512GB", iphone_air)
    assert r.status == "ambiguous"

    # The model prompt never receives authoritative order ids.
    prompt_text = json.dumps(llm.last_prompt, ensure_ascii=False)
    assert "SO-IPHONE-AIR-NEW" not in prompt_text
    assert "SO-IPHONE-AIR-OLD" not in prompt_text
    assert "order_candidate_1" in prompt_text

    # Hallucinated/unknown refs are rejected server-side.
    llm = FakeResolverLLM({"status": "resolved", "selected_ref": "SO-HUAWEI-X-NEW", "ambiguous_refs": []})
    r = await resolve_order_subject(llm, "Pura X", ORDERS)
    assert r.status == "unknown"


def test_support_command_binding_harness() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
    print("support_command binding harness PASS")
