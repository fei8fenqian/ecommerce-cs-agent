"""共享的客户可见响应边界。

Control state precedence 只在这里定义一次。SupportWorkflow、普通 ``/chat`` 和流式
出口都委托同一个 composer；LLM 只能提供候选文案，不能越过这里改变控制状态或补充
未经核验的退款事实。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from agent.decision_context import (
    SUBJECT_CONTEXT_RESET_MARKER,
    context_facts_for_subject,
    merge_decision_contexts,
)

if TYPE_CHECKING:
    from agent.engines.loop import LoopResult


_TRUSTED_ENTRY_PATTERN = re.compile(r"\?page=orders&refund_order=SO[A-Z0-9_-]+")
_STATUS_MESSAGES = {
    "PENDING_CONFIRMATION": "这笔退款目前还在等待确认。",
    "PENDING_MERCHANT_REVIEW": "这笔退款目前还在等待商家核验。",
    "PROCESSING": "这笔退款目前还在处理中。",
    "COMPLETED": "系统里的退款记录目前显示已完成。",
    "FAILED": "系统里的退款记录目前显示失败。",
    "NOT_FOUND": "当前账户中没有查到这笔订单的退款记录。",
}
_EXISTING_REFUND_STATUS = {
    "PENDING_CONFIRMATION": "等待确认",
    "PENDING_MERCHANT_REVIEW": "等待商家核验",
    "PROCESSING": "处理中",
    "COMPLETED": "已完成",
    "FAILED": "失败",
}
_PAYMENT_STATUS_MESSAGES = {
    "PAID": "系统已核验到这笔订单的支付状态为已支付。",
    "PENDING": "这笔订单当前仍显示为待支付。",
    "NOT_PAID": "这笔订单当前尚未完成支付。",
    "PAYMENT_NOT_CREATED": "这笔订单当前还没有可核验的支付交易，请从订单页重新打开支付。",
    "PAYMENT_STATUS_UNAVAILABLE": (
        "支付渠道当前暂时无法确认这笔订单的支付状态。"
        "商城不会把本次查询失败解释为支付成功或支付失败。"
        "你可以稍后从订单页重新打开支付，或刷新支付状态。"
    ),
}
_UNAVAILABLE_MESSAGES = {
    "query_refund_expected_arrival": "当前只能确认退款状态，无法核验具体到账时间。",
    "query_refund_processing_sla": "当前没有可核验的退款处理时长。",
    "query_refund_destination": "当前无法核验退款去向，不能仅根据支付方式推断。",
    "query_refund_failure_reason": "当前无法核验具体退款失败原因。",
    "check_refund_cancel_eligibility": "当前无法自动核验或取消退款。",
    "query_return_status": "当前无法核验退货状态。",
    "query_return_logistics": "当前无法核验退货物流状态。",
    "query_warehouse_receipt": "当前无法核验退货是否已回仓。",
    "generate_refund_entry": "退款入口资格发生变化，当前无法生成可用退款入口。",
}
_UNAVAILABLE_STATUS_MESSAGES = {
    "PENDING_CONFIRMATION": "退款申请正在等待确认",
    "PENDING_MERCHANT_REVIEW": "退款正在等待商家核验",
    "PROCESSING": "退款正在处理中",
    "COMPLETED": "退款已完成",
    "FAILED": "退款状态为失败",
    "NOT_FOUND": "当前没有匹配的退款记录",
}
_MISSING_FACT_MESSAGES = {
    "order_identified": "当前还没有确定要查询的订单。",
    "refund_status": "当前还没有拿到这笔订单的退款记录状态。",
    "refund_eligibility": "当前还没有拿到这笔订单的退款资格查询结果，无法确认是否符合退款资格。",
    "refund_amount": "当前还没有拿到退款记录金额。",
    "expected_arrival_time": "当前还没有可核验的具体到账时间。",
    "refund_processing_sla": "当前还没有可核验的退款处理时长。",
    "refund_destination": "当前还没有可核验的退款去向。",
    "refund_failure_reason": "当前还没有可核验的退款失败原因。",
    "refund_entry": "当前暂时无法生成可用的退款入口。",
    "warehouse_receipt_status": "当前还没有可核验的退货回仓状态。",
}
_TRANSACTION_FACTS = {
    "refund_status",
    "refund_amount",
    "refund_eligibility",
    "refund_entry",
    "refund_destination",
    "expected_arrival_time",
    "refund_processing_sla",
    "refund_failure_reason",
    "warehouse_receipt_status",
}
_SUBJECT_SENSITIVE_OPERATIONS = {
    "status",
    "amount",
    "eligibility",
    "request",
    "cancel",
    "expected_arrival",
    "processing_time",
    "anomaly",
    "destination",
    "delivery_after_refund",
}

# 多订单结果常被模型写成 Markdown 表格或紧凑的“订单号 + 状态/金额”行。每次
# 只在同一订单号到下一个订单号之前扫描，避免把相邻订单的事实串成一笔。
_TRANSACTION_ROW_CLAIM_PATTERN = re.compile(
    r"\b(?P<order_id>SO[A-Z0-9_-]+)\b"
    r"(?:(?!\bSO[A-Z0-9_-]+\b)[\s\S]){0,160}?"
    r"(?:处理中|已完成|失败|[¥￥]\s*[\d,]+(?:\.\d{1,2})?)",
    re.IGNORECASE,
)

# 这些模式只用于判断 LLM 是否正在输出交易性结论；命中后由 renderer 重新生成
# 安全答案，不对原文做删词。它们不决定 Intent，也不替代 Control Plane。
_REFUND_TRANSACTION_CLAIM_PATTERNS = (
    (
        "refund_status",
        re.compile(r"退款(?:记录)?(?:目前|现在)?(?:显示|状态(?:为|是))?(?:已完成|处理中|失败|不存在|没有)"),
    ),
    (
        "refund_amount",
        re.compile(r"退款金额(?:为|是)?\s*[¥￥]?\s*\d"),
    ),
    (
        "refund_eligibility",
        re.compile(r"(?:符合|不符合)(?:当前)?退款资格"),
    ),
    (
        "refund_timing",
        re.compile(
            r"(?:\d+\s*[-—到至]\s*\d+\s*(?:个?工作日|天|小时)|(?:几|数)个工作日|"
            r"一般需要.{0,20}(?:到账|审核|处理)|通常需要.{0,20}(?:到账|审核|处理))"
        ),
    ),
    (
        "refund_process_stage",
        re.compile(r"(?:平台审核|审核处理阶段|处于审核阶段|支付渠道的处理速度|款项到账后页面会更新)"),
    ),
    (
        "refund_destination",
        re.compile(r"(?:支付宝|银行卡|原路)退回(?:成功)?|退款去向"),
    ),
    (
        "funds_arrived",
        re.compile(r"(?:退款已经|退款已|钱已经|钱已)到账"),
    ),
    (
        "refund_full_amount_eligible",
        re.compile(r"全额退款资格|符合全额退款|全额退款"),
    ),
    (
        "refund_failure_reason",
        re.compile(r"(?:因为|由于|原因是).{0,30}(?:银行|支付宝|支付渠道|风控|审核|系统)"),
    ),
    (
        "refund_record",
        re.compile(r"(?:查询到|查到|有|存在).{0,24}(?:一|两|多|\d+)\s*笔\s*退款(?:记录)?"),
    ),
    (
        "refund_submission",
        re.compile(r"(?:退款申请已提交|退款已提交|已经提交退款|已为你申请退款|已替您申请退款)"),
    ),
)


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _trusted_entry(value: object, subject_id: str | None = None) -> str | None:
    if not isinstance(value, str) or _TRUSTED_ENTRY_PATTERN.fullmatch(value) is None:
        return None
    if subject_id and f"refund_order={subject_id}" not in value:
        return None
    return value


def _request_operation(requests: Sequence[Mapping[str, Any]]) -> str:
    if len(requests) != 1:
        return ""
    request = requests[0]
    return str(request.get("operation") or "")


def _request_domain(requests: Sequence[Mapping[str, Any]]) -> str:
    if len(requests) != 1:
        return ""
    return str(requests[0].get("domain") or "")


def _selected_subject_id(selected_subjects: object) -> str | None:
    order_id = _field(selected_subjects, "order_id")
    return order_id if isinstance(order_id, str) and order_id.startswith("SO") else None


def _case_pending(case: object) -> Mapping[str, Any]:
    pending = _field(case, "pending", {})
    return pending if isinstance(pending, Mapping) else {}


def _case_selected_subjects(case: object) -> Mapping[str, Any]:
    selected = _field(case, "selected_subjects", {})
    return selected if isinstance(selected, Mapping) else {}


def _case_has_subject_context_reset(case: object | None) -> bool:
    """Keep disputed historical subjects out of a new customer response."""
    verified_facts = _field(case, "verified_facts", {})
    return isinstance(verified_facts, Mapping) and isinstance(verified_facts.get(SUBJECT_CONTEXT_RESET_MARKER), Mapping)


def _legacy_facts(
    facts: Mapping[str, Any] | None,
    *,
    contexts: Sequence[Mapping[str, Any]],
    subject_id: str | None,
    allow_subjectless: bool,
) -> tuple[dict[str, Any], str | None]:
    """保留旧 flat metadata 的调用兼容，但不把它当客户可见事实源。

    旧会话仍可由 API 层使用 flat metadata 判断“这是退款上下文”。但没有可信
    ``subject_id`` 的 flat facts 不能进入 renderer；否则 ``refund_status``、金额、
    资格甚至旧的退款入口都会绕过 subject/provenance 边界。
    """
    if not isinstance(facts, Mapping):
        return {}, None
    # ``contexts``、``subject_id`` 和 ``allow_subjectless`` 保留在签名中以兼容旧
    # 调用方；flat metadata 永远不参与客户可见事实渲染。API 层仍可单独使用
    # ``session_facts`` 判断这是退款上下文，但必须重新查询或确认订单后才能说出
    # status、金额、资格等交易事实。
    del contexts, subject_id, allow_subjectless
    return {}, None


def _select_facts(
    result: LoopResult,
    *,
    current_contexts: Sequence[Mapping[str, Any]],
    historical_contexts: Sequence[Mapping[str, Any]],
    legacy_current_facts: Mapping[str, Any] | None,
    legacy_historical_facts: Mapping[str, Any] | None,
    selected_subjects: Mapping[str, Any],
    pending_choice: bool,
    missing_facts: Iterable[str],
) -> tuple[dict[str, Any], dict[str, str], str | None]:
    """按 subject 及每个 fact 的 provenance 选择 renderer 可用事实。"""
    current = merge_decision_contexts(current_contexts)
    historical = merge_decision_contexts(historical_contexts, default_provenance="historical")
    selected_id = _selected_subject_id(selected_subjects)
    current_ids = {str(item.get("subject_id")) for item in current if item.get("subject_id")}
    historical_ids = {str(item.get("subject_id")) for item in historical if item.get("subject_id")}

    if pending_choice and selected_id is None:
        return {}, {}, None
    if selected_id is None:
        if len(current_ids) == 1:
            selected_id = next(iter(current_ids))
        elif len(current_ids) > 1:
            return {}, {}, None
        elif len(historical_ids) == 1:
            selected_id = next(iter(historical_ids))
        elif len(historical_ids) > 1:
            return {}, {}, None

    facts: dict[str, Any] = {}
    provenance_by_fact: dict[str, str] = {}
    if selected_id:
        # A newly verified subject must not silently fall back to facts cached for
        # the previously selected order.  The caller may update the Case selection
        # after a deterministic choice; until then, a subject mismatch is fail-closed.
        if current_ids and selected_id not in current_ids:
            return {}, {}, None
        current_facts, _ = context_facts_for_subject(current, selected_id, provenance="current")
        historical_facts, _ = context_facts_for_subject(historical, selected_id, provenance="historical")
        for name, value in current_facts.items():
            facts[name] = value
            provenance_by_fact[name] = "current"
        for name, value in historical_facts.items():
            if name not in facts:
                facts[name] = value
                provenance_by_fact[name] = "historical"
    else:
        current_legacy, _ = _legacy_facts(
            legacy_current_facts,
            contexts=current,
            subject_id=selected_id,
            allow_subjectless=True,
        )
        historical_legacy, _ = _legacy_facts(
            legacy_historical_facts,
            contexts=historical,
            subject_id=selected_id,
            allow_subjectless=True,
        )
        for name, value in current_legacy.items():
            facts[name] = value
            provenance_by_fact[name] = "current"
        for name, value in historical_legacy.items():
            if name not in facts:
                facts[name] = value
                provenance_by_fact[name] = "historical"

    # Control Plane 已明确指出的 missing fact 不能被历史缓存反向填充。
    missing = {str(item) for item in missing_facts}
    for name in list(facts):
        if name in missing and provenance_by_fact.get(name) == "historical":
            facts.pop(name, None)
            provenance_by_fact.pop(name, None)
    return facts, provenance_by_fact, selected_id


def _render_choice(
    result: LoopResult,
    choices: Sequence[Mapping[str, Any]],
    *,
    refund_request: bool = False,
) -> None:
    lines = ["我查到有多笔符合条件的订单，请回复序号或订单号选择要查询的那一笔："]
    if refund_request:
        lines.append("目前一次只能处理一笔退款，请先选择一笔；处理完成后可以继续处理另一笔。")
    for index, choice in enumerate(choices, start=1):
        order_id = str(choice.get("order_id") or "")
        detail = [order_id]
        product_name = str(choice.get("product_name") or "").strip()
        if product_name:
            detail.append(product_name)
        amount_cents = choice.get("amount_cents")
        if isinstance(amount_cents, int) and not isinstance(amount_cents, bool):
            detail.append(f"¥{amount_cents / 100:.2f}")
        lines.append(f"{index}. " + " · ".join(detail))
    result.answer = "\n".join(lines)
    result.response_control = {"mode": "ASK_CHOICE", "subject_id": None}


def _render_unavailable(result: LoopResult, facts: Mapping[str, Any]) -> None:
    progress = result.workflow_progress
    capabilities = [str(item) for item in progress.get("unavailable_capabilities", [])]
    details = list(
        dict.fromkeys(
            _UNAVAILABLE_MESSAGES.get(capability, "当前无法核验相关业务信息。") for capability in capabilities
        )
    )
    status = _UNAVAILABLE_STATUS_MESSAGES.get(str(facts.get("refund_status") or ""))
    prefix = f"已核验到：{status}。" if status else ""
    result.answer = prefix + "".join(details) + "如需人工客服协助，请回复“转人工”。"
    # A missing capability is an explanation/offer, not proof that a real
    # staff queue has accepted the case.  Only ticket creation may emit the
    # STAFF_HANDOFF presentation mode.
    result.response_control = {"mode": "FACT", "subject_id": None}


def _render_blocked_missing(
    result: LoopResult,
    facts: Mapping[str, Any],
    *,
    fact_provenance: Mapping[str, str] | None = None,
) -> None:
    progress = result.workflow_progress
    missing = [str(item) for item in progress.get("missing_facts", [])]
    missing_set = set(missing)
    provenance = fact_provenance or {}

    def current_fact(name: str) -> bool:
        # A blocked response may mention only facts freshly verified for this
        # subject.  Historical facts are useful for audit, not for presenting
        # a blocked request as if its current control state were resolved.
        return name in facts and provenance.get(name, "current") == "current" and name not in missing_set

    known: list[str] = []
    status = _STATUS_MESSAGES.get(str(facts.get("refund_status") or ""))
    if status and current_fact("refund_status"):
        known.append(status)
    if isinstance(facts.get("refund_eligibility"), bool) and current_fact("refund_eligibility"):
        eligibility = "符合" if facts["refund_eligibility"] else "不符合"
        known.append(f"系统核验结果显示，这笔订单当前{eligibility}退款资格。")
    if facts.get("shipping_status") == "NOT_SHIPPED" and current_fact("shipping_status"):
        known.append("订单当前尚未发货。")
    details = [_MISSING_FACT_MESSAGES[item] for item in missing if item in _MISSING_FACT_MESSAGES]
    if not details:
        reason = str(progress.get("reason") or "")
        details = [
            {
                "capability_unavailable": "当前缺少可用的业务处理能力，暂时无法继续完成这项操作。",
                "fact_tool_failed": "当前核验订单信息时遇到问题，暂时无法继续完成这项操作。",
                "workflow_unsupported": "当前还没有可用的自动处理流程，暂时无法继续完成这项操作。",
            }.get(reason, "当前还缺少完成这项操作所需的业务信息或能力。")
        ]
    if (
        facts.get("refund_eligibility") is True
        and facts.get("shipping_status") == "NOT_SHIPPED"
        and current_fact("refund_eligibility")
        and current_fact("shipping_status")
        and "refund_entry" in missing_set
    ):
        result.answer = (
            "这笔订单当前符合退款资格且尚未发货，但暂时无法生成退款入口。请从订单页稍后重试；如仍无法操作，可转人工。"
        )
    elif known or details:
        result.answer = "".join(known + details) + "如需人工客服协助，请回复“转人工”。"
    result.response_control = {"mode": "FACT", "subject_id": None}


def _render_limited_refund_facts(
    result: LoopResult,
    operation: str,
    facts: Mapping[str, Any],
    *,
    fact_provenance: Mapping[str, str],
    subject_id: str | None,
) -> bool:
    """Render a verified refund status without pretending an unavailable detail exists."""
    status = str(facts.get("refund_status") or "")
    status_answer = _STATUS_MESSAGES.get(status)
    if status_answer is None:
        return False
    if fact_provenance.get("refund_status") == "historical":
        status_answer = "根据上一轮系统查询，" + status_answer
    limitation = {
        "expected_arrival": "当前只能确认退款状态，无法核验这笔退款的具体到账时间。",
        "processing_time": "当前只能确认退款状态，无法核验这笔退款的具体处理时长。",
        "anomaly": "当前只能确认退款状态，无法核验具体失败或延迟原因。",
        "destination": "当前只能确认退款状态，无法核验退款去向，不能根据支付方式推断。",
        "cancel": "当前只能确认退款状态，暂时无法自动核验是否可以取消退款。",
    }.get(operation)
    if limitation is None:
        return False
    result.answer = status_answer + limitation
    result.response_control = {
        "mode": "FACT",
        "subject_id": subject_id,
        "fact_provenance": dict(fact_provenance),
        "fact_keys": sorted(str(name) for name in facts),
        "limitation": operation,
    }
    return True


def _render_refund_facts(
    result: LoopResult,
    operation: str,
    facts: Mapping[str, Any],
    *,
    fact_provenance: Mapping[str, str],
    subject_id: str | None,
) -> bool:
    if operation == "status":
        status = str(facts.get("refund_status") or "")
        answer = _STATUS_MESSAGES.get(status)
        if answer is None:
            return False
        if fact_provenance.get("refund_status") == "historical":
            answer = "根据上一轮系统查询，" + answer
        amount = facts.get("refund_amount")
        if isinstance(amount, int) and not isinstance(amount, bool):
            answer += f"退款金额为 ¥{amount / 100:.2f}。"
        if status == "FAILED" and "refund_failure_reason" not in facts:
            answer += "目前无法核验具体失败原因。"
        result.answer = answer
        result.response_control = {
            "mode": "FACT",
            "subject_id": subject_id,
            "fact_provenance": dict(fact_provenance),
            "fact_keys": sorted(str(name) for name in facts),
        }
        return True

    if operation == "request":
        status = str(facts.get("refund_status") or "")
        if status and status != "NOT_FOUND":
            status_text = _EXISTING_REFUND_STATUS.get(status)
            if status_text is None:
                return False
            answer = f"系统已查到这笔订单存在退款记录，当前退款状态为{status_text}。"
            amount = facts.get("refund_amount")
            if isinstance(amount, int) and not isinstance(amount, bool):
                answer += f"退款金额为 ¥{amount / 100:.2f}。"
            answer += "客服不会重复创建另一笔退款。"
            result.answer = answer
            result.response_control = {
                "mode": "FACT",
                "subject_id": subject_id,
                "fact_provenance": dict(fact_provenance),
                "fact_keys": sorted(str(name) for name in facts),
            }
            return True
        if facts.get("refund_eligibility") is False:
            result.answer = "系统核验结果显示，这笔订单当前不符合退款资格。"
            result.response_control = {
                "mode": "FACT",
                "subject_id": subject_id,
                "fact_provenance": dict(fact_provenance),
                "fact_keys": sorted(str(name) for name in facts),
            }
            return True

    if operation == "eligibility" and isinstance(facts.get("refund_eligibility"), bool):
        prefix = "根据上一轮系统查询，" if fact_provenance.get("refund_eligibility") == "historical" else ""
        if facts["refund_eligibility"]:
            answer = prefix + "系统核验结果显示，这笔订单当前符合退款资格。"
            if facts.get("shipping_status") == "NOT_SHIPPED":
                if fact_provenance.get("shipping_status") == "historical":
                    answer += "上一轮系统查询显示订单尚未发货。"
                else:
                    answer += "订单当前尚未发货。"
        else:
            answer = prefix + "系统核验结果显示，这笔订单当前不符合退款资格。"
        result.answer = answer
        result.response_control = {
            "mode": "FACT",
            "subject_id": subject_id,
            "fact_provenance": dict(fact_provenance),
            "fact_keys": sorted(str(name) for name in facts),
        }
        return True
    return False


def _render_payment_facts(
    result: LoopResult,
    facts: Mapping[str, Any],
    *,
    fact_provenance: Mapping[str, str],
    subject_id: str | None,
) -> bool:
    status = str(facts.get("payment_status") or "")
    answer = _PAYMENT_STATUS_MESSAGES.get(status)
    if answer is None:
        return False
    if fact_provenance.get("payment_status") == "historical":
        answer = "根据上一轮系统查询，" + answer
    result.answer = answer
    result.response_control = {
        "mode": "FACT",
        "subject_id": subject_id,
        "fact_provenance": dict(fact_provenance),
        "fact_keys": sorted(str(name) for name in facts),
    }
    return True


def _render_pending_payment_cancel_handoff(
    result: LoopResult,
    *,
    subject_id: str | None,
    fact_provenance: Mapping[str, str],
) -> None:
    result.answer = (
        "这笔订单当前尚未完成支付，因此没有已支付款项需要退款。"
        "如果不再购买，可以前往订单页取消这笔待支付订单。"
        "提交取消时系统会再次核验支付渠道交易状态。"
    )
    result.response_control = {
        "mode": "SELF_SERVICE_ORDER_CANCEL",
        "subject_id": subject_id,
        "fact_provenance": dict(fact_provenance),
    }


def _render_order_cancel_facts(
    result: LoopResult,
    facts: Mapping[str, Any],
    *,
    subject_id: str | None,
    fact_provenance: Mapping[str, str],
) -> bool:
    order_status = str(facts.get("order_status") or "")
    if not order_status:
        return False
    if order_status == "PENDING_PAYMENT":
        _render_pending_payment_cancel_handoff(
            result,
            subject_id=subject_id,
            fact_provenance=fact_provenance,
        )
        return True
    result.answer = "系统核验到这笔订单当前不是待支付状态，暂时不能通过待支付订单取消入口取消。"
    result.response_control = {
        "mode": "FACT",
        "subject_id": subject_id,
        "fact_provenance": dict(fact_provenance),
    }
    return True


def _unverified_refund_claim(
    answer: str,
    facts: Mapping[str, Any],
    *,
    trusted_subject_id: str | None = None,
) -> tuple[str, str] | None:
    """返回没有当前可信事实支持的退款 claim。

    这是 customer-visible boundary 的最后一道确定性检查。它只做 claim
    classification；一旦发现越界，调用方会完整替换答案，而不是修改模型原文。
    """
    # LLM 经常用 Markdown 表格强调交易状态；装饰符不应改变 claim 的语义，
    # 但也不能把原文删词后返回给客户。这里只用规范化副本做分类，命中后仍由
    # 调用方完整替换答案。
    claim_text = re.sub(r"[*_`]", "", answer)
    row_claims = list(_TRANSACTION_ROW_CLAIM_PATTERN.finditer(claim_text))
    row_subjects = {match.group("order_id") for match in row_claims}
    if row_subjects and (trusted_subject_id is None or row_subjects != {trusted_subject_id}):
        return "unbound_refund_transaction", row_claims[0].group(0)
    for claim_type, pattern in _REFUND_TRANSACTION_CLAIM_PATTERNS:
        match = pattern.search(claim_text)
        if match is None:
            continue
        if claim_type == "refund_status" and isinstance(facts.get("refund_status"), str):
            continue
        if claim_type == "refund_amount" and isinstance(facts.get("refund_amount"), int):
            continue
        if claim_type == "refund_eligibility" and isinstance(facts.get("refund_eligibility"), bool):
            continue
        if claim_type == "refund_timing" and (
            facts.get("expected_arrival_time") is not None or facts.get("refund_processing_sla") is not None
        ):
            continue
        if claim_type == "refund_process_stage" and facts.get("refund_processing_stage"):
            continue
        if claim_type == "refund_destination" and facts.get("refund_destination"):
            continue
        if claim_type == "funds_arrived" and facts.get("funds_arrived") is True:
            continue
        if claim_type == "refund_full_amount_eligible" and facts.get("refund_full_amount_eligible") is True:
            continue
        if claim_type == "refund_failure_reason" and facts.get("refund_failure_reason"):
            continue
        # 当前实现没有把 refund_record/refund_submission 作为可接受的 flat fact；
        # 它们必须来自 subject-bound refund_status/refund_entry 分支。
        return claim_type, match.group(0)
    return None


def compose_customer_response(
    result: LoopResult,
    requests: Sequence[Mapping[str, Any]] | None = None,
    *,
    current_contexts: Sequence[Mapping[str, Any]] | None = None,
    historical_contexts: Sequence[Mapping[str, Any]] | None = None,
    legacy_current_facts: Mapping[str, Any] | None = None,
    legacy_historical_facts: Mapping[str, Any] | None = None,
    case: object | None = None,
    selected_subjects: Mapping[str, Any] | None = None,
    enforce_refund_boundary: bool = False,
) -> None:
    """按固定 precedence 生成客户答案。

    ``SELF_SERVICE_HANDOFF > ASK_CHOICE/AWAITING_CUSTOMER > BLOCKED/STAFF >
    resolved facts > LLM``。函数可重复调用：后续出口只会重新确认更高优先级，
    不会把选单或阻塞结果改写成普通退款状态。
    """
    progress = result.workflow_progress if isinstance(result.workflow_progress, dict) else {}
    requests_list = [item for item in (requests or []) if isinstance(item, Mapping)]
    # ``None`` means use the LoopResult contexts; an explicit empty list means this
    # turn produced no fresh context.  Treating ``[]`` as false here would let a
    # follow-up turn accidentally reuse the previous turn as if it were current.
    current = list(result.decision_contexts if current_contexts is None else current_contexts)
    historical = list([] if historical_contexts is None else historical_contexts)
    if _case_has_subject_context_reset(case):
        # The old subject is retained as historical audit only. Until a fresh
        # trusted subject/current context arrives, it cannot support a new
        # customer-visible transaction claim.
        historical = []
    case_selected = _case_selected_subjects(case)
    selected = dict(case_selected if selected_subjects is None else selected_subjects)
    pending = _case_pending(case)
    case_status = str(_field(case, "status") or "")
    pending_choices = progress.get("pending_choices")
    if not isinstance(pending_choices, list) or not pending_choices:
        pending_choices = pending.get("choices") if isinstance(pending.get("choices"), list) else []
    pending_choice = (
        progress.get("next_action") == "ASK_CHOICE"
        or pending.get("kind") == "customer_choice"
        or (case_status == "AWAITING_CUSTOMER" and pending.get("kind") == "customer_choice")
    ) and _selected_subject_id(selected) is None

    operation = _request_operation(requests_list)
    domain = _request_domain(requests_list)
    refund_sensitive = enforce_refund_boundary or any(
        str(item.get("domain") or "") == "refund" for item in requests_list
    )
    payment_sensitive = domain == "payment" and operation == "check_payment_status"
    raw_missing_facts = progress.get("missing_facts")
    missing_facts: list[str] = [str(item) for item in raw_missing_facts] if isinstance(raw_missing_facts, list) else []
    facts, fact_provenance, subject_id = _select_facts(
        result,
        current_contexts=current,
        historical_contexts=historical,
        legacy_current_facts=legacy_current_facts,
        legacy_historical_facts=legacy_historical_facts,
        selected_subjects=selected,
        pending_choice=pending_choice,
        missing_facts=missing_facts,
    )

    # 最高优先级：入口必须来自受控 fact，且完整替换 LLM 文案。
    if progress.get("resolution_type") == "SELF_SERVICE_HANDOFF":
        entry = _trusted_entry(facts.get("refund_entry"), subject_id)
        if entry is None:
            progress.pop("resolution_type", None)
            progress["goal_status"] = "blocked"
            progress["control_state"] = "BLOCKED"
            progress["next_action"] = "EXPLAIN_LIMITATION_OR_HANDOFF"
            progress["next_actor"] = "NONE"
            progress["reason"] = "capability_unavailable"
            unavailable = list(progress.get("unavailable_capabilities", []))
            if "generate_refund_entry" not in unavailable:
                unavailable.append("generate_refund_entry")
            progress["unavailable_capabilities"] = unavailable
            result.answer = "当前无法生成可用的官方退款入口。如需人工客服协助，请回复“转人工”。"
            result.response_control = {"mode": "FACT", "subject_id": subject_id}
            return
        result.answer = (
            "退款资格已核验通过。\n\n"
            "请在官方订单页面自行填写退款原因并确认提交。\n"
            "客服不会代为创建、提交或确认退款。\n\n"
            f"[前往我的订单申请退款]({entry})"
        )
        result.response_control = {
            "mode": "SELF_SERVICE_HANDOFF",
            "subject_id": subject_id,
            "fact_provenance": dict(fact_provenance),
        }
        return

    if progress.get("resolution_type") == "SELF_SERVICE_ORDER_CANCEL":
        if subject_id is None or facts.get("order_status") != "PENDING_PAYMENT":
            # A customer-facing cancellation handoff requires a current, trusted
            # pending-payment order.  Do not turn a stale/unknown order into a
            # navigation action.
            result.answer = "当前还无法确认这笔订单是否仍可取消，请先从订单页核验订单状态。"
            result.response_control = {"mode": "AWAITING_CUSTOMER", "subject_id": None}
            return
        _render_pending_payment_cancel_handoff(
            result,
            subject_id=subject_id,
            fact_provenance=fact_provenance,
        )
        return

    # 选择框是控制状态，不允许被任何 fact renderer 覆盖。
    if pending_choice and pending_choices:
        _render_choice(
            result,
            [item for item in pending_choices if isinstance(item, Mapping)],
            refund_request=any(
                str(item.get("domain") or "") == "refund" and str(item.get("operation") or "") == "request"
                for item in requests_list
            ),
        )
        return

    # Subject-bound contexts are authoritative.  If they contain more than one
    # order, or the Case selection points at an order absent from the current
    # verified context, never fall back to a flat transaction fact.  This is a
    # response boundary, not a routing decision: ask for the missing subject and
    # let the next turn execute a bound read.  It is deliberately evaluated
    # after the pending-choice branch so a choice frame always retains the
    # exact options that were shown to the customer.
    context_subject_ids = {
        str(item.get("subject_id"))
        for item in [*current, *historical]
        if isinstance(item, Mapping)
        and isinstance(item.get("subject_id"), str)
        and str(item.get("subject_id")).startswith("SO")
    }
    selected_id = _selected_subject_id(selected)
    subject_selection_conflict = bool(context_subject_ids) and (
        subject_id is None
        and (len(context_subject_ids) != 1 or (selected_id and selected_id not in context_subject_ids))
    )
    if subject_selection_conflict and operation in _SUBJECT_SENSITIVE_OPERATIONS:
        result.answer = "请先确认要查询的订单，我再为您核对这笔退款。"
        result.response_control = {
            "mode": "AWAITING_CUSTOMER",
            "subject_id": None,
            "reason": "subject_not_resolved",
        }
        return

    goal_status = str(progress.get("goal_status") or "")
    next_action = str(progress.get("next_action") or "")
    next_actor = str(progress.get("next_actor") or "")
    awaiting_customer = case_status == "AWAITING_CUSTOMER" or goal_status == "awaiting_customer"
    if awaiting_customer or next_action in {
        "ASK_CLARIFICATION",
        "ASK_CHOICE",
        "AWAITING_CONFIRMATION",
    }:
        if next_action == "ASK_CLARIFICATION":
            result.answer = "为了继续处理，请说明您要查询退款状态、申请退款，还是处理其他退款问题。"
            result.response_control = {"mode": "ASK_CLARIFICATION", "subject_id": None}
        elif next_action == "AWAITING_CONFIRMATION":
            result.answer = "请确认是否继续当前操作。"
            result.response_control = {"mode": "AWAITING_CUSTOMER", "subject_id": subject_id}
        else:
            result.answer = "请补充或确认需要处理的订单信息。"
            result.response_control = {"mode": "AWAITING_CUSTOMER", "subject_id": subject_id}
        return

    if goal_status == "resolved_with_limitation":
        if domain == "refund" and _render_limited_refund_facts(
            result,
            operation,
            facts,
            fact_provenance=fact_provenance,
            subject_id=subject_id,
        ):
            return
        if progress.get("unavailable_capabilities"):
            _render_unavailable(result, facts)
            return

    # Actual staff handoff and hard transaction blocks have higher precedence.
    # A capability gap that has an explicit safe-partial predicate was handled
    # above and must never be flattened back into a STAFF handoff.
    has_control_envelope = case_status in {"AWAITING_STAFF", "AWAITING_CUSTOMER"} or any(
        progress.get(key) not in (None, "") for key in ("control_state", "next_actor", "next_action", "reason")
    )
    blocked = (
        case_status == "AWAITING_STAFF"
        or next_actor in {"STAFF", "SYSTEM"}
        or goal_status in {"blocked", "unresolved"}
        or str(progress.get("control_state") or "") in {"BLOCKED", "NEED_FACT"}
    ) and has_control_envelope
    if blocked:
        if progress.get("unavailable_capabilities"):
            _render_unavailable(result, facts)
        else:
            _render_blocked_missing(result, facts, fact_provenance=fact_provenance)
        return

    if domain == "refund" and operation == "procedure":
        result.answer = (
            "请打开[我的订单](?page=orders)，选择对应的已付款订单；符合退款条件时页面会显示“申请退款”。"
            "填写退款原因后提交，之后按页面提示确认。"
        )
        result.response_control = {"mode": "FACT", "subject_id": None}
        return

    if domain == "refund" and operation == "request" and subject_id is None and not facts:
        # Refund reason is collected by the Orders self-service form after the
        # target order has been resolved.  It is not a chat workflow fact.
        result.answer = "请先选择或提供要处理的订单；退款原因会在订单页申请时填写。"
        result.response_control = {
            "mode": "AWAITING_CUSTOMER",
            "subject_id": None,
            "reason": "subject_not_resolved",
        }
        return

    if _render_refund_facts(
        result,
        operation,
        facts,
        fact_provenance=fact_provenance,
        subject_id=subject_id,
    ):
        return

    if domain == "order" and operation == "cancel":
        if _render_order_cancel_facts(
            result,
            facts,
            subject_id=subject_id,
            fact_provenance=fact_provenance,
        ):
            return

    if payment_sensitive:
        if _render_payment_facts(
            result,
            facts,
            fact_provenance=fact_provenance,
            subject_id=subject_id,
        ):
            return
        # A payment tool error is UNKNOWN, never an invitation for the model to
        # speculate about balance, bank, Huabei, network or provider behaviour.
        result.answer = (
            "当前暂时无法确认这笔订单的支付状态，也无法核验具体支付失败原因。请稍后从订单页重新打开支付或刷新支付状态。"
        )
        result.response_control = {"mode": "FACT", "subject_id": subject_id}
        return

    # 没有可用的 subject-bound/current fact 时，不能把普通 AgentLoop 的退款交易
    # 结论交给客户。这里必须完整替换答案，避免在 /chat 或 /chat/stream 中把
    # “某一笔退款已完成/处理中/金额……”等无主体事实泄露出去。
    if refund_sensitive:
        unverified_claim = _unverified_refund_claim(
            result.answer,
            facts,
            trusted_subject_id=subject_id,
        )
        if unverified_claim is not None:
            claim_type, _ = unverified_claim
            result.answer = (
                "我还没有核验到对应订单的退款信息，不能直接确认退款状态或到账情况。"
                "请提供或确认具体订单号，我再为您核对。"
            )
            result.response_control = {
                "mode": "AWAITING_CUSTOMER",
                "subject_id": subject_id,
                "reason": "unverified_refund_claim",
                "claim_type": claim_type,
            }
            return
    result.response_control = {
        "mode": "GENERIC",
        "subject_id": subject_id,
        "fact_provenance": dict(fact_provenance),
    }
