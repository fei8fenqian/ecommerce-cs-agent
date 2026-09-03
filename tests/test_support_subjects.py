"""Support Case 客户候选选择的确定性解析测试。"""

from agent.support_subjects import (
    looks_like_pending_subject_choice,
    match_subject_identity_choices,
    resolve_pending_subject_choice,
)

CHOICES = [
    {"order_id": "SOREAL_A6", "product_name": "戴尔 Inspiron 14 笔记本", "amount_cents": 920000},
    {"order_id": "SOREAL_A7", "product_name": "Sony WF-1000XM5 无线耳机", "amount_cents": 189900},
]


def test_pending_choice_ordinal_uses_persisted_order():
    result = resolve_pending_subject_choice("第二个", CHOICES)

    assert result is not None
    assert result["choice"]["order_id"] == "SOREAL_A7"
    assert result["selection_source"] == "ordinal"


def test_pending_choice_exact_order_id():
    result = resolve_pending_subject_choice("查 SOREAL_A6", CHOICES)

    assert result is not None
    assert result["choice"]["order_id"] == "SOREAL_A6"
    assert result["selection_source"] == "exact_id"


def test_pending_choice_unique_product():
    result = resolve_pending_subject_choice("戴尔那台", CHOICES)

    assert result is not None
    assert result["choice"]["order_id"] == "SOREAL_A6"
    assert result["selection_source"] == "product"


def test_pending_choice_higher_and_lower_amount():
    higher = resolve_pending_subject_choice("贵一点的", CHOICES)
    lower = resolve_pending_subject_choice("便宜那个", CHOICES)

    assert higher is not None and higher["choice"]["order_id"] == "SOREAL_A6"
    assert higher["selection_source"] == "comparative_higher_amount"
    assert lower is not None and lower["choice"]["order_id"] == "SOREAL_A7"
    assert lower["selection_source"] == "comparative_lower_amount"


def test_pending_choice_exclusion_selects_remaining_order():
    result = resolve_pending_subject_choice("不是 Sony 那一笔", CHOICES)

    assert result is not None
    assert result["choice"]["order_id"] == "SOREAL_A6"
    assert result["selection_source"] == "exclusion"


def test_pending_choice_ambiguous_or_unknown_does_not_guess():
    assert resolve_pending_subject_choice("那个", CHOICES) is None
    assert resolve_pending_subject_choice("第三个", CHOICES) is None


def test_ambiguous_deictic_reply_stays_in_pending_case():
    assert looks_like_pending_subject_choice("那个", CHOICES) is True


def test_model_specific_identity_does_not_match_shared_brand_only():
    choices = [
        {"order_id": "SO-17", "product_name": "苹果 iPhone 17（512GB）"},
        {"order_id": "SO-16", "product_name": "苹果 iPhone 16（128GB）"},
    ]

    for query in (
        "我想把iphone16退了",
        "我说的是iphone16",
        "你听得懂iphone16吗 我想退款",
    ):
        matches = match_subject_identity_choices(query, choices)
        assert [item["order_id"] for item in matches] == ["SO-16"]


def test_shared_brand_remains_ambiguous():
    choices = [
        {"order_id": "SO-KC", "product_name": "金士顿 KC3000 NVMe SSD", "recency_rank": 1},
        {"order_id": "SO-NV2", "product_name": "金士顿 NV2 NVMe SSD", "recency_rank": 8},
    ]

    assert resolve_pending_subject_choice("金士顿那个", choices) is None
    assert len(match_subject_identity_choices("金士顿", choices)) == 2


def test_catalog_brand_identity_normalization_is_candidate_scoped_and_cross_language():
    choices = [
        {"order_id": "SO-HW", "product_name": "HUAWEI Mate 70"},
        {"order_id": "SO-AP", "product_name": "Apple iPhone Air 1TB"},
        {"order_id": "SO-SO", "product_name": "Sony WH-1000XM6"},
    ]

    assert [item["order_id"] for item in match_subject_identity_choices("我问的是华为手机", choices)] == [
        "SO-HW"
    ]
    assert [item["order_id"] for item in match_subject_identity_choices("苹果那台", choices)] == ["SO-AP"]
    assert [item["order_id"] for item in match_subject_identity_choices("索尼耳机", choices)] == ["SO-SO"]


def test_same_brand_identity_evidence_keeps_multiple_server_candidates_ambiguous():
    choices = [
        {"order_id": "SO-HW-1", "product_name": "HUAWEI Mate 70"},
        {"order_id": "SO-HW-2", "product_name": "HUAWEI Pura 80"},
        {"order_id": "SO-AP", "product_name": "Apple iPhone Air 1TB"},
    ]

    matches = match_subject_identity_choices("刚买的华为手机", choices)

    assert [item["order_id"] for item in matches] == ["SO-HW-1", "SO-HW-2"]
