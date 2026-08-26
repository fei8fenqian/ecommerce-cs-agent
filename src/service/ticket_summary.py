"""工单在内部队列和飞书通知中共用的安全摘要。"""

import re

from log_config import redact_text

_ORDER_REFERENCE_PATTERN = re.compile(r"\b(?:ORD|SO|ORDER)[-_A-Z0-9]{3,}\b", re.IGNORECASE)


def build_safe_ticket_summary(value: str) -> str:
    """移除常见个人信息、订单引用并限制长度，供未认领队列展示。"""
    return _ORDER_REFERENCE_PATTERN.sub("[REDACTED]", redact_text(value)).strip().replace("\n", " ")[:240]
