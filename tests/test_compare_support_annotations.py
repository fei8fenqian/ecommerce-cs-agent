"""标注对比脚本的纯函数测试，不连接数据库。"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "compare_support_annotations",
    Path(__file__).parents[1] / "scripts" / "compare_support_annotations.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
compare = _MODULE.compare


def test_compare_reports_proxy_agreement_and_confusion_matrix():
    left = {
        "a": {"id": "a", "provisional_action": "CONTINUE"},
        "b": {"id": "b", "provisional_action": "CREATE_TICKET"},
        "c": {"id": "c", "provisional_action": "ASK_FOR_CLARIFICATION"},
    }
    right = {
        "a": {"id": "a", "human_gold_action": "CONTINUE"},
        "b": {"id": "b", "human_gold_action": "ASK_FOR_CLARIFICATION"},
        "d": {"id": "d", "human_gold_action": "CREATE_TICKET"},
    }

    result = compare(
        left,
        right,
        left_field="provisional_action",
        right_field="human_gold_action",
        left_name="machine",
        right_name="reviewer",
        max_examples=5,
    )

    assert result["common_ids"] == 2
    assert result["comparable"] == 2
    assert result["agreement_count"] == 1
    assert result["proxy_agreement"] == 0.5
    assert result["missing_from_left"] == 1
    assert result["missing_from_right"] == 1
    assert result["disagreement_count"] == 1


def test_compare_rejects_unknown_action():
    with pytest.raises(ValueError, match="不是有效动作"):
        compare(
            {"a": {"id": "a", "provisional_action": "NOT_AN_ACTION"}},
            {"a": {"id": "a", "human_gold_action": "CONTINUE"}},
            left_field="provisional_action",
            right_field="human_gold_action",
            left_name="machine",
            right_name="reviewer",
            max_examples=5,
        )
