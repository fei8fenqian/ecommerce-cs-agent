"""受控业务事实的 subject/provenance 容器。

退款、订单等交易事实不能只靠一组扁平字典在会话中累积。这个模块保持一个很小的
JSON 结构：每组事实都带有可信 subject、来源和 current/historical provenance，供
Workflow、Case 恢复和客户响应边界共同使用。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# 这些事实必须绑定订单 subject；如果没有可信 subject，只能作为旧版本兼容
# metadata 留在 flat 字段中，不能参与新的客户可见交易事实合并。
SUBJECT_BOUND_FACTS = frozenset(
    {
        "order_identified",
        "order_status",
        "shipping_status",
        "expected_ship_time",
        "refund_status",
        "refund_amount",
        "refund_eligibility",
        "refund_entry",
        "refund_destination",
        "expected_arrival_time",
        "refund_processing_sla",
        "refund_failure_reason",
        "warehouse_receipt_status",
        "payment_status",
    }
)

# Internal Case metadata used when a customer explicitly disputes the current
# subject. It is not a business fact and must never be exposed to the model or
# customer-facing projections.
SUBJECT_CONTEXT_RESET_MARKER = "_subject_context_reset"


def normalize_decision_contexts(
    contexts: Iterable[Mapping[str, Any]] | None,
    *,
    default_provenance: str = "current",
) -> list[dict[str, Any]]:
    """过滤并按 ``subject + provenance`` 合并受控事实组。

    没有可信 subject 的交易事实不会进入 context。旧版本只保存 flat facts 的数据由
    调用方作为兼容输入单独处理，不能在这里伪造 subject。
    """
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in contexts or []:
        if not isinstance(raw, Mapping):
            continue
        subject_type = str(raw.get("subject_type") or "order")
        subject_id = raw.get("subject_id")
        provenance = str(raw.get("provenance") or default_provenance)
        facts = raw.get("facts")
        if subject_type != "order" or not isinstance(subject_id, str) or not subject_id.startswith("SO"):
            continue
        if provenance not in {"current", "historical"} or not isinstance(facts, Mapping) or not facts:
            continue
        key = (subject_type, subject_id, provenance)
        item = merged.setdefault(
            key,
            {
                "subject_type": subject_type,
                "subject_id": subject_id,
                "provenance": provenance,
                "source": str(raw.get("source") or "tool"),
                "facts": {},
            },
        )
        item["facts"].update({str(name): value for name, value in facts.items()})
        source = str(raw.get("source") or "").strip()
        if source and source != item.get("source") and source not in item.setdefault("sources", []):
            item.setdefault("sources", []).append(source)
    return list(merged.values())


def merge_decision_contexts(
    *groups: Iterable[Mapping[str, Any]] | None,
    default_provenance: str = "current",
) -> list[dict[str, Any]]:
    """合并多批 context，同时保留 subject 和 provenance 隔离。"""
    flattened: list[Mapping[str, Any]] = []
    for group in groups:
        if group:
            flattened.extend(item for item in group if isinstance(item, Mapping))
    return normalize_decision_contexts(flattened, default_provenance=default_provenance)


def historicalize_decision_contexts(
    contexts: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """把已结束 turn 的事实标成 historical，同时保留 subject/source。"""
    return normalize_decision_contexts(
        [{**item, "provenance": "historical"} for item in (contexts or []) if isinstance(item, Mapping)],
        default_provenance="historical",
    )


def context_facts_for_subject(
    contexts: Iterable[Mapping[str, Any]] | None,
    subject_id: str | None,
    *,
    provenance: str | None = None,
) -> tuple[dict[str, Any], str | None]:
    """读取指定 subject 的 facts，并返回事实中使用的 provenance。

    current facts 优先于 historical facts；同一事实名不会从另一个订单补入。
    """
    if not subject_id:
        return {}, None
    candidates = [
        item
        for item in normalize_decision_contexts(contexts)
        if item.get("subject_id") == subject_id and (provenance is None or item.get("provenance") == provenance)
    ]
    candidates.sort(key=lambda item: 0 if item.get("provenance") == "current" else 1)
    facts: dict[str, Any] = {}
    used: str | None = None
    for item in candidates:
        item_facts = item.get("facts")
        if not isinstance(item_facts, Mapping):
            continue
        if used is None:
            used = str(item.get("provenance") or "") or None
        for name, value in item_facts.items():
            facts.setdefault(str(name), value)
    return facts, used
