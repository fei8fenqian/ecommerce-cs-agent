"""Customer-safe presentation DTOs and projection helpers.

The execution stack owns business truth.  This module only projects the small
subset of already trusted control/fact data that a customer UI may render.  It
must never parse an answer, Markdown, or a model-generated URL.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field

from agent.decision_context import context_facts_for_subject


class _CustomerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CustomerDisplayField(_CustomerModel):
    label: str
    value: str


class CustomerSubjectSummary(_CustomerModel):
    subject_type: Literal["order"] = "order"
    subject_id: str
    title: str
    subtitle: str | None = None


class SubjectChoiceInteraction(_CustomerModel):
    """The only structured customer interaction supported by Contract V1."""

    type: Literal["subject_choice"] = "subject_choice"
    subject_type: Literal["order"] = "order"
    subject_id: str = Field(min_length=1, max_length=128)


class CustomerOrderTarget(_CustomerModel):
    order_id: str
    focus: Literal["details", "refund", "cancel"] | None = None


class CustomerTicketTarget(_CustomerModel):
    ticket_id: str


class CustomerNavigateOrderAction(_CustomerModel):
    type: Literal["navigate"] = "navigate"
    id: str
    label: str
    destination: Literal["orders"] = "orders"
    target: CustomerOrderTarget


class CustomerNavigateTicketAction(_CustomerModel):
    type: Literal["navigate"] = "navigate"
    id: str
    label: str
    destination: Literal["tickets"] = "tickets"
    target: CustomerTicketTarget


class CustomerSubjectChoiceAction(_CustomerModel):
    type: Literal["interaction"] = "interaction"
    id: str
    label: str
    interaction: SubjectChoiceInteraction


# ``type`` is intentionally not used as a discriminator here because both
# navigation destinations use ``type="navigate"``.  Pydantic still validates
# the typed destination/target union and rejects arbitrary params dictionaries.
CustomerAction: TypeAlias = Union[
    CustomerNavigateOrderAction,
    CustomerNavigateTicketAction,
    CustomerSubjectChoiceAction,
]


class ChoiceOption(_CustomerModel):
    id: str
    subject: CustomerSubjectSummary
    meta: list[CustomerDisplayField] = Field(default_factory=list)
    action: CustomerSubjectChoiceAction


class ChoicePresentation(_CustomerModel):
    version: Literal[1] = 1
    kind: Literal["choice"] = "choice"
    title: str
    description: str | None = None
    options: list[ChoiceOption]


class StatusPresentation(_CustomerModel):
    version: Literal[1] = 1
    kind: Literal["status"] = "status"
    title: str
    subject: CustomerSubjectSummary
    status: str
    details: list[CustomerDisplayField] = Field(default_factory=list)


class ActionPresentation(_CustomerModel):
    version: Literal[1] = 1
    kind: Literal["action"] = "action"
    title: str
    description: str | None = None
    actions: list[CustomerAction]


class HandoffPresentation(_CustomerModel):
    version: Literal[1] = 1
    kind: Literal["handoff"] = "handoff"
    title: str
    description: str
    actions: list[CustomerAction] = Field(default_factory=list)


CustomerPresentation: TypeAlias = Annotated[
    Union[ChoicePresentation, StatusPresentation, ActionPresentation, HandoffPresentation],
    Field(discriminator="kind"),
]


_TRUSTED_REFUND_ENTRY = re.compile(r"\?page=orders&refund_order=SO[A-Z0-9_-]+")
_REFUND_STATUS_LABELS = {
    "PENDING_CONFIRMATION": "等待确认",
    "PENDING_MERCHANT_REVIEW": "等待商家核验",
    "PROCESSING": "处理中",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "NOT_FOUND": "未找到退款记录",
}


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _subject_id(value: object) -> str | None:
    if isinstance(value, str) and value.startswith("SO"):
        return value
    return None


def _trusted_entry(value: object, subject_id: str | None) -> str | None:
    if not isinstance(value, str) or _TRUSTED_REFUND_ENTRY.fullmatch(value) is None:
        return None
    if subject_id is None or f"refund_order={subject_id}" not in value:
        return None
    return value


def _context_facts(result: object, subject_id: str | None) -> dict[str, Any]:
    """Return only known projected facts for one trusted subject."""
    if subject_id is None:
        return {}
    contexts = _field(result, "decision_contexts", [])
    if not isinstance(contexts, Sequence) or isinstance(contexts, (str, bytes)):
        return {}

    # Presentation 是当前客户可见的状态投影，不是历史事实浏览器。只消费
    # ``current`` context；这样 current shipping + historical eligibility 不会被
    # 合并成一张看似实时的资格卡，同时同一事实的 current 版本天然优先。
    current_facts, provenance = context_facts_for_subject(
        contexts,
        subject_id,
        provenance="current",
    )
    if provenance != "current":
        return {}
    return {
        key: value
        for key, value in current_facts.items()
        if key
        in {
            "refund_status",
            "refund_amount",
            "refund_eligibility",
            "refund_entry",
            "shipping_status",
            "order_status",
        }
    }


def _choice_items(result: object, case: object | None) -> list[Mapping[str, Any]]:
    # Once persisted, the Case snapshot is the exact frame that was shown to the
    # customer.  It takes precedence over a regenerated workflow result so a
    # refresh/resume cannot silently reorder ordinal choices.
    pending = _field(case, "pending", {})
    case_choices = _field(pending, "choices", []) if isinstance(pending, Mapping) else []
    if isinstance(case_choices, list) and case_choices:
        return [item for item in case_choices if isinstance(item, Mapping)][:3]
    progress = _field(result, "workflow_progress", {})
    choices = _field(progress, "pending_choices", [])
    return [item for item in choices if isinstance(item, Mapping)][:3]


def _choice_presentation(result: object, case: object | None) -> dict[str, Any] | None:
    choices = _choice_items(result, case)
    if not choices:
        return None
    options: list[ChoiceOption] = []
    for index, choice in enumerate(choices, start=1):
        order_id = _subject_id(choice.get("order_id"))
        if order_id is None:
            continue
        product_name = str(choice.get("product_name") or "订单").strip() or "订单"
        amount = choice.get("amount_cents")
        meta: list[CustomerDisplayField] = []
        if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
            meta.append(CustomerDisplayField(label="金额", value=f"¥{amount / 100:.2f}"))
        action = CustomerSubjectChoiceAction(
            id=f"choose-order-{index}",
            label="选择这笔",
            interaction=SubjectChoiceInteraction(subject_id=order_id),
        )
        options.append(
            ChoiceOption(
                id=f"order-{order_id}",
                subject=CustomerSubjectSummary(
                    subject_id=order_id,
                    title=product_name,
                    subtitle=order_id,
                ),
                meta=meta,
                action=action,
            )
        )
    if not options:
        return None
    return ChoicePresentation(
        title="请选择要查询的订单",
        description="我找到多笔相关订单，请选择其中一笔继续。",
        options=options,
    ).model_dump(mode="json", exclude_none=True)


def _status_presentation(
    result: object,
    requests: Sequence[Mapping[str, Any]],
    subject_id: str,
) -> dict[str, Any] | None:
    facts = _context_facts(result, subject_id)
    status = facts.get("refund_status")
    eligibility = facts.get("refund_eligibility")
    operation = str(requests[0].get("operation") or "") if len(requests) == 1 else ""
    if operation == "eligibility" and isinstance(eligibility, bool):
        status_text = "符合退款资格" if eligibility else "暂不符合退款资格"
        details: list[CustomerDisplayField] = []
        if facts.get("shipping_status") == "NOT_SHIPPED":
            details.append(CustomerDisplayField(label="订单状态", value="尚未发货"))
        return StatusPresentation(
            title="退款资格",
            subject=CustomerSubjectSummary(subject_id=subject_id, title="当前订单", subtitle=subject_id),
            status=status_text,
            details=details,
        ).model_dump(mode="json", exclude_none=True)
    if not isinstance(status, str) or status not in _REFUND_STATUS_LABELS:
        return None
    details = []
    amount = facts.get("refund_amount")
    if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
        details.append(CustomerDisplayField(label="退款金额", value=f"¥{amount / 100:.2f}"))
    return StatusPresentation(
        title="退款状态",
        subject=CustomerSubjectSummary(subject_id=subject_id, title="当前订单", subtitle=subject_id),
        status=_REFUND_STATUS_LABELS[status],
        details=details,
    ).model_dump(mode="json", exclude_none=True)


def build_customer_presentation(
    result: object,
    requests: Sequence[Mapping[str, Any]] | None = None,
    *,
    case: object | None = None,
    ticket_id: str | None = None,
) -> dict[str, Any] | None:
    """Project trusted workflow output into the minimal customer UI contract."""
    control = _field(result, "response_control", {})
    if not isinstance(control, Mapping):
        control = {}
    mode = str(control.get("mode") or "")
    progress = _field(result, "workflow_progress", {})
    if not isinstance(progress, Mapping):
        progress = {}
    case_status = str(_field(case, "status") or "")
    pending = _field(case, "pending", {})
    pending_kind = str(_field(pending, "kind") or "") if isinstance(pending, Mapping) else ""

    if mode == "ASK_CHOICE" or progress.get("next_action") == "ASK_CHOICE" or pending_kind == "customer_choice":
        choice = _choice_presentation(result, case)
        if choice is not None:
            return choice

    subject_id = _subject_id(control.get("subject_id"))
    selected = _field(case, "selected_subjects", {})
    if subject_id is None:
        subject_id = _subject_id(_field(selected, "order_id"))
    request_list = [item for item in (requests or []) if isinstance(item, Mapping)]
    facts = _context_facts(result, subject_id)

    if mode == "SELF_SERVICE_HANDOFF" and subject_id is not None:
        entry = _trusted_entry(facts.get("refund_entry"), subject_id)
        if entry is not None:
            return ActionPresentation(
                title="请在订单中继续申请退款",
                description="退款资格已核验通过，请在官方订单页面自行填写原因并确认提交。",
                actions=[
                    CustomerNavigateOrderAction(
                        id="open-refund-order",
                        label="前往订单处理",
                        target=CustomerOrderTarget(order_id=subject_id, focus="refund"),
                    )
                ],
            ).model_dump(mode="json", exclude_none=True)

    if (
        mode == "SELF_SERVICE_ORDER_CANCEL"
        and subject_id is not None
        and facts.get("order_status") == "PENDING_PAYMENT"
    ):
        return ActionPresentation(
            title="可在订单中取消待支付订单",
            description="订单尚未完成支付；提交取消时系统会再次核验支付渠道交易状态。",
            actions=[
                CustomerNavigateOrderAction(
                    id="open-cancel-order",
                    label="前往订单取消",
                    target=CustomerOrderTarget(order_id=subject_id, focus="cancel"),
                )
            ],
        ).model_dump(mode="json", exclude_none=True)

    if mode == "STAFF_HANDOFF" or case_status == "AWAITING_STAFF":
        actions: list[CustomerAction] = []
        if isinstance(ticket_id, str) and ticket_id.strip():
            actions.append(
                CustomerNavigateTicketAction(
                    id="open-support-ticket",
                    label="查看服务进度",
                    target=CustomerTicketTarget(ticket_id=ticket_id),
                )
            )
        if actions:
            return HandoffPresentation(
                title="已创建人工客服工单",
                description="人工客服已接手这项问题，你可以查看服务进度。",
                actions=actions,
            ).model_dump(mode="json", exclude_none=True)
        return HandoffPresentation(
            title="当前自动流程暂时无法继续处理",
            description="如需人工客服协助，请回复“转人工”。",
            actions=[],
        ).model_dump(mode="json", exclude_none=True)

    if mode == "FACT" and subject_id is not None:
        status = _status_presentation(result, request_list, subject_id)
        if status is not None:
            return status
    return None
