"""受控的客户订单 subject correction 检测与解析。

该模块只判断客户是否在明确纠正当前已绑定订单，并提取客户对新订单的描述。
它绝不选择订单号；订单候选和最终 subject 仍由 API 在当前客户的订单范围内确定。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from agent.support_subjects import match_subject_identity_choices

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubjectCorrection:
    """一个经过格式校验的 correction 判断，不携带服务端选择结果。"""

    is_correction: bool
    subject_description: str = ""


# This is only a cheap pre-gate for invoking the semantic detector.  It must
# cover common correction wording without becoming the transition decision.
# In particular, "前面那个不对" and "我弄错了" do not contain the older
# short markers as exact substrings.
_CORRECTION_MARKER = re.compile(
    r"(?:前面|刚才|上面|之前)[^。！？?!\n]{0,12}(?:不对|错了|有误)"
    r"|(?:我|前面|刚才)?(?:弄错|搞错|说错|选错)"
    r"|不是|指的是|其实是|应该是|改成|换成"
)

_SYSTEM_PROMPT = """你是电商客服中的订单指代纠正检测器，只返回 JSON。

当前会话已经绑定了一笔订单。判断客户当前消息是否明确表示“前面绑定的订单不对，客户要改查另一笔订单”。
只有同时满足以下条件才返回 is_subject_correction=true：
1. 明确否定、纠正或承认前一个订单/选择有误；
2. 指出了另一笔订单的商品、品牌、型号或订单号等身份描述。

不要只用金额、贵/便宜等比较信息描述订单；金额不是本阶段支持的身份匹配依据。

“那 Sony 那个退款怎么样？”、“再查一下戴尔那笔”这类新问题不一定是否定前一笔，返回 false。
不要选择订单号，不要猜订单，不要输出任何业务事实。
subject_description 只填写客户用来描述新订单的原文短语；无法确定时返回 false。

格式：{"is_subject_correction":true,"subject_description":"Sony 耳机"}
或：{"is_subject_correction":false,"subject_description":""}
"""


def looks_like_subject_correction_candidate(query: str) -> bool:
    """只作为是否调用语义检测器的廉价候选门，不直接决定业务转换。

    最终 correction 必须由语义检测器确认，且订单 ID 仍必须由服务端候选解析得到。
    """

    return bool(isinstance(query, str) and _CORRECTION_MARKER.search(query))


def parse_subject_correction(value: object) -> SubjectCorrection:
    """严格解析检测器输出；不接受模型直接指定的 target order id。"""

    if isinstance(value, SubjectCorrection):
        return value if _valid_description(value.subject_description) else SubjectCorrection(False)
    payload: object = value
    if isinstance(value, str):
        try:
            payload = json.loads(_strip_code_fence(value.strip()))
        except (TypeError, ValueError, json.JSONDecodeError):
            return SubjectCorrection(False)
    if not isinstance(payload, dict) or payload.get("is_subject_correction") is not True:
        return SubjectCorrection(False)
    description = payload.get("subject_description")
    if not _valid_description(description):
        return SubjectCorrection(False)
    return SubjectCorrection(True, str(description).strip()[:160])


async def detect_subject_correction(
    llm: Any,
    *,
    query: str,
    history: list[dict[str, Any]] | None = None,
    current_subject_id: str = "",
) -> SubjectCorrection:
    """用独立的轻量语义判断确认 correction，不参与 canonical Goal 路由。"""

    if llm is None or not looks_like_subject_correction_candidate(query):
        return SubjectCorrection(False)
    visible_history = [
        {
            "role": str(message.get("role") or ""),
            "content": str(message.get("content") or "")[:600],
        }
        for message in (history or [])[-6:]
        if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
    ]
    user_payload = {
        "current_bound_order": current_subject_id,
        "recent_dialogue": visible_history,
        "customer_message": query[:1000],
    }
    try:
        response = await llm.chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=120,
        )
    except Exception:
        # correction 不是主路由，检测器失败时回到原有客服路径，不能凭关键词切单。
        logger.warning("subject correction detector unavailable", extra={"error_type": "dependency_failure"})
        return SubjectCorrection(False)
    return parse_subject_correction(getattr(response, "content", response))


def resolve_subject_description(
    description: str,
    choices: object,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """按订单号/商品身份归类 correction 描述，不消费 pending 选择语法。

    ``subject_correction_description`` 尚未向客户展示候选，因此这里不能使用
    ``resolve_pending_subject_choice`` 的 ordinal、金额比较或 exclusion 推理。
    """

    matches = match_subject_identity_choices(description, choices)
    if len(matches) > 1:
        return "multiple", None, matches
    if len(matches) == 1:
        return "unique", matches[0], matches
    return "none", None, []


def _valid_description(value: object) -> bool:
    if not isinstance(value, str):
        return False
    description = value.strip()
    if not description or len(description) > 160:
        return False
    # A bare amount is not an identity description. Keep this contract even
    # when a detector override bypasses the cheap continuation gate.
    return re.fullmatch(r"[¥￥]?\s*\d+(?:\.\d+)?", description) is None


def _strip_code_fence(value: str) -> str:
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return value
