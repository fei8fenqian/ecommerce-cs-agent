from agent.product_context import (
    attach_candidate_frame,
    extract_price_update,
    filter_candidates_by_context,
    ordinal_choice_ref,
    set_choice_refs,
    update_product_context,
)

CANDIDATES = [
    {
        "product_id": "p-a55",
        "product_name": "三星 Galaxy A55（12GB/256GB）",
        "display_title": "三星 Galaxy A55（12GB/256GB）",
        "category": "phones",
        "price": 2999,
        "comparison_metadata": {"brand": "三星", "ram": "12GB", "storage": "256GB"},
    },
    {
        "product_id": "p-s60-12",
        "product_name": "vivo S60（12GB/512GB）",
        "display_title": "vivo S60（12GB/512GB）",
        "category": "phones",
        "price": 3999,
        "comparison_metadata": {"brand": "vivo", "ram": "12GB", "storage": "512GB"},
    },
    {
        "product_id": "p-s60-16",
        "product_name": "vivo S60（16GB/512GB）",
        "display_title": "vivo S60（16GB/512GB）",
        "category": "phones",
        "price": 4399,
        "comparison_metadata": {"brand": "vivo", "ram": "16GB", "storage": "512GB"},
    },
    {
        "product_id": "p-iphone",
        "product_name": "Apple iPhone 16 Pro（128GB）",
        "display_title": "Apple iPhone 16 Pro（128GB）",
        "category": "phones",
        "price": 7999,
        "comparison_metadata": {"brand": "Apple", "storage": "128GB"},
    },
]


def test_explicit_range_becomes_catalog_eligibility_constraint():
    assert extract_price_update("4000-5000的手机随便推荐") == {
        "min_price_cents": 400000,
        "max_price_cents": 500000,
        "target_price_cents": None,
    }
    context, changed = update_product_context({}, query="4000-5000", table="phone_products", is_product_turn=True)
    assert changed is True
    assert [item["product_id"] for item in filter_candidates_by_context(CANDIDATES, context)] == ["p-s60-16"]


def test_common_budget_unit_typo_is_normalized():
    assert extract_price_update("5000快以内") == {
        "min_price_cents": None,
        "max_price_cents": 500000,
        "target_price_cents": None,
    }


def test_target_budget_does_not_become_hard_range():
    context, _ = update_product_context({}, query="预算5000块左右", table="phone_products", is_product_turn=True)
    assert context["target_price_cents"] == 500000
    assert "min_price_cents" not in context
    assert "max_price_cents" not in context


def test_brand_mention_is_semantic_preference_not_automatic_hard_filter():
    base, _ = update_product_context({}, query="4000-5000", table="phone_products", is_product_turn=True)
    mentioned, changed = update_product_context(
        base, query="你们不是还有 vivo S60 之类的吗", table="", is_product_turn=True
    )
    assert changed is False
    assert "brand_keys" not in mentioned
    assert [item["product_id"] for item in filter_candidates_by_context(CANDIDATES, mentioned)] == ["p-s60-16"]


def test_service_turn_cannot_mutate_product_constraints():
    base, _ = update_product_context({}, query="4000-5000", table="phone_products", is_product_turn=True)
    after, changed = update_product_context(
        base,
        query="我刚买的4399那台想退款",
        table="",
        category="",
        is_product_turn=False,
    )
    assert changed is False
    assert after == base


def test_product_choice_ordinal_uses_server_owned_choice_refs():
    context = attach_candidate_frame({}, CANDIDATES[1:3])
    context = set_choice_refs(context, ["candidate_2", "candidate_1"])
    assert ordinal_choice_ref("1", context) == "candidate_2"
    assert ordinal_choice_ref("第二个", context) == "candidate_1"
