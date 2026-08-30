"""Support Case 客户候选选择的确定性解析测试。"""

from agent.support_subjects import looks_like_pending_subject_choice, resolve_pending_subject_choice

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
