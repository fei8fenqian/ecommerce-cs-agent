"""3C 客户支持聊天的确定性动作闸门。

意图路由器和 LLM 只能提出 ``ticket`` 建议；本模块结合当前原话与已发送的
受控引导，决定是否真的允许创建工单。它不访问数据库、不调用工具，因此可以
用真实口语样本稳定回归测试。
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CustomerSupportAction(StrEnum):
    """客户聊天允许执行的售后动作。"""

    CONTINUE = "CONTINUE"
    OFFER_REFUND_SELF_SERVICE = "OFFER_REFUND_SELF_SERVICE"
    SHOW_REFUND_PROGRESS = "SHOW_REFUND_PROGRESS"
    OFFER_WARRANTY_TROUBLESHOOTING = "OFFER_WARRANTY_TROUBLESHOOTING"
    ASK_FOR_CLARIFICATION = "ASK_FOR_CLARIFICATION"
    CREATE_TICKET = "CREATE_TICKET"


@dataclass(frozen=True)
class CustomerSupportDecision:
    """由确定性规则给出的动作许可，而不是模型直接执行的命令。"""

    action: CustomerSupportAction
    reason: str
    answer: str = ""


_REFUND_MARKERS = (
    "退款",
    "退货",
    "退钱",
    "退的钱",
    "想退",
    "要退",
    "退一下",
    "退掉",
    "退回去",
    "申请退",
    "不想要",
    "不要了",
    "还没退",
    "没退",
    "未退",
    "钱没到账",
)
_REFUND_PROGRESS_MARKERS = (
    "已经退了",
    "已退了",
    "退过了",
    "已退款",
    "退款了",
    "已退货",
    "退货了",
    "已经申请退款",
    "已申请退款",
    "申请过退款",
    "退货已经寄出",
    "退货已寄出",
    "退货寄出",
    "退货已经寄回",
    "退货已寄回",
    "退款进度",
    "退款状态",
    "到账了吗",
    "到账没",
    "哪里看退款",
    "怎么看退款",
    "查退款",
    "还没退款",
    "退款没到账",
    "退款未到账",
    "没到账",
    "未到账",
    "看不到退款",
    "没有退款",
    "咋还没退款",
    "怎么还没退款",
)
_REFUND_EXCEPTION_MARKERS = (
    "退款失败",
    "无法退款",
    "不能退款",
    "退不了",
    "申请失败",
    "操作失败",
    "重复扣款",
    "多扣",
    "金额不对",
    "不能申请退款",
    "无法申请退款",
    "退款申请不了",
    "支付异常",
    "订单异常",
    "页面报错",
    "退款页报错",
    "点退款没反应",
    "退款没反应",
    "少退",
    "退少",
    "退款少了",
    "退的钱少了",
)
_HUMAN_MARKERS = (
    "转人工",
    "转接人工",
    "人工客服",
    "真人客服",
    "找人工",
    "需要人工",
    "仍需人工",
    "还是要人工",
    "必须人工",
    "坚持人工",
)
_TICKET_REQUIRED_MARKERS = (
    *_HUMAN_MARKERS,
    "投诉",
    "纠纷",
    "争议",
    "赔偿",
    "改地址",
    "修改地址",
    "取消订单",
    "重复扣款",
    "支付失败",
    "付款失败",
    "订单显示没付",
    "地址填错",
)
_REFUND_GUIDANCE_LINK = "[前往我的订单申请退款](?page=orders)"
_WARRANTY_GUIDANCE_MARKER = "[设备故障排查与保修说明]"
_DEVICE_SYMPTOM_MARKERS = (
    "设备出问题",
    "设备故障",
    "设备坏了",
    "电脑出问题",
    "电脑坏了",
    "笔记本出问题",
    "手机出问题",
    "手机坏了",
    "开不了机",
    "无法开机",
    "开机不了",
    "黑屏",
    "死机",
    "蓝屏",
    "闪退",
    "充不上电",
    "充不进电",
    "不充电",
    "不通电",
    "无法充电",
    "不能用",
    "无法使用",
    "不工作",
    "故障",
    "坏了",
    "损坏",
    "进水",
    "摔坏",
    "漏液",
    "发热",
    "不灵敏",
    "有问题",
    "出现问题",
    "出问题",
    "异常",
    "不好用",
    "没反应",
    "开裂",
    "裂了",
    "漏水",
    "掉毛",
    "变形",
    "质量问题",
    "不正常",
    "无法识别",
    "失灵",
    "信号不好",
)
_DEVICE_REFERENCE_MARKERS = (
    "设备",
    "电脑",
    "笔记本",
    "手机",
    "平板",
    "手表",
    "台灯",
    "耳机",
    "路由器",
    "打印机",
    "相机",
    "电视",
    "冰箱",
    "空调",
    "轮胎",
    "电饭煲",
    "洗衣机",
    "键盘",
    "屏幕",
    "电池",
    "充电宝",
    "鼠标",
)
_STRONG_DEVICE_SYMPTOM_MARKERS = (
    "坏了",
    "故障",
    "不灵敏",
    "损坏",
    "开不了机",
    "无法开机",
    "黑屏",
    "死机",
    "充不上电",
    "不能用",
    "无法使用",
    "没反应",
    "发热",
    "进水",
    "出现问题",
    "质量问题",
    "无法识别",
    "失灵",
    "信号不好",
    "充不进电",
    "异响",
    "不正常",
    "出问题",
)
_REPAIR_REQUEST_MARKERS = (
    "报修",
    "申请维修",
    "需要维修",
    "送修",
    "寄修",
    "保修维修",
)
_DEVICE_DANGER_MARKERS = (
    "冒烟",
    "起火",
    "着火",
    "爆炸",
    "电池鼓包",
    "漏电",
    "烧焦",
)


def _normalized(value: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”‘’\"'（）()【】\[\]]+", "", value).lower()


def _has_marker(value: str, markers: tuple[str, ...]) -> bool:
    return any(marker in value for marker in markers)


def _refund_self_service_already_offered(history: list[dict[str, Any]] | None) -> bool:
    """只相信服务端保存的上一条助手引导，不从用户原话推断已完成确认。"""
    return any(
        message.get("role") == "assistant" and _REFUND_GUIDANCE_LINK in str(message.get("content") or "")
        for message in (history or [])[-8:]
    )


def refund_self_service_answer() -> str:
    return (
        "可以先在本人订单中发起退款申请：\n\n"
        f"{_REFUND_GUIDANCE_LINK}\n\n"
        "选择对应的已付款订单后点击“申请退款”，系统会核验订单、金额和退款资格。"
        "如果页面无法提交、订单或金额有异议，请告诉我具体情况；如仍希望人工处理，"
        "回复“需要人工”或“仍需人工”即可。"
    )


def refund_progress_answer() -> str:
    return (
        "如果你已经提交退款或退货申请，就不用重复创建工单了。\n\n"
        "可以在[我的订单查看退款进度](?page=orders)。如果退款长时间未到账、金额不对，"
        "或仍需要人工处理，再告诉我具体情况。"
    )


def warranty_troubleshooting_answer() -> str:
    return (
        f"{_WARRANTY_GUIDANCE_MARKER}\n\n"
        "先不要反复通电、拆机或继续使用。请告诉我设备类型/型号、具体症状、从什么时候开始，"
        "以及是否摔碰、进水或出现异常发热；如果方便，也可以提供订单号或购买手机号后四位，"
        "我先帮你核对保修范围。\n\n"
        "在没有冒烟、起火、电池鼓包或漏电等危险迹象时，可以先检查电源/充电器和指示灯，"
        "拔掉外接设备后长按电源键 10–15 秒，再正常开机。若仍无法恢复，回复“需要报修”；"
        "出现冒烟、起火、鼓包或漏电请立即断电并停止使用。"
    )


def clarification_answer() -> str:
    return (
        "为了避免误建售后工单，请补充一下你遇到的是哪一种情况：退款/退货、"
        "商品故障需要报修、支付或订单异常，还是投诉。\n\n"
        "如果是退款，请说明对应订单、退款页面是否报错，或金额是否有问题。"
    )


def _warranty_guidance_already_offered(history: list[dict[str, Any]] | None) -> bool:
    return any(
        message.get("role") == "assistant" and _WARRANTY_GUIDANCE_MARKER in str(message.get("content") or "")
        for message in (history or [])[-8:]
    )


def decide_customer_support_action(
    *,
    intent_target: str,
    role: str,
    query: str,
    history: list[dict[str, Any]] | None = None,
) -> CustomerSupportDecision:
    """将模型提出的工单意图收敛为允许执行的确定性动作。

    未被允许的 ``ticket`` 一律不会触发写操作：普通退款先走订单入口，模糊
    售后请求要求补充事实。退款类的明确人工诉求至少要先收到一次自助引导；客户
    在该引导后再次明确要求人工，才允许建单。
    """
    if role != "customer" or intent_target != "ticket":
        return CustomerSupportDecision(CustomerSupportAction.CONTINUE, "NOT_CUSTOMER_TICKET")

    normalized = _normalized(query)
    has_refund = _has_marker(normalized, _REFUND_MARKERS) or _has_marker(normalized, _REFUND_PROGRESS_MARKERS)
    has_refund_exception = _has_marker(normalized, _REFUND_EXCEPTION_MARKERS)
    has_human_request = _has_marker(normalized, _HUMAN_MARKERS)
    recent_customer_context = "".join(
        _normalized(str(message.get("content") or ""))
        for message in (history or [])[-8:]
        if message.get("role") == "user"
    )
    device_context = f"{normalized}{recent_customer_context}"
    has_device_symptom = _has_marker(device_context, _DEVICE_SYMPTOM_MARKERS) and (
        _has_marker(device_context, _DEVICE_REFERENCE_MARKERS)
        or _has_marker(normalized, _STRONG_DEVICE_SYMPTOM_MARKERS)
        or _has_marker(normalized, _REPAIR_REQUEST_MARKERS)
    )
    has_repair_request = _has_marker(normalized, _REPAIR_REQUEST_MARKERS)
    has_device_danger = _has_marker(normalized, _DEVICE_DANGER_MARKERS)
    has_device_unresolved = _has_marker(normalized, ("还是不行", "仍然不行", "依旧不行", "没解决", "解决不了"))

    if has_device_danger:
        return CustomerSupportDecision(CustomerSupportAction.CREATE_TICKET, "DEVICE_SAFETY_RISK")

    if _warranty_guidance_already_offered(history) and (
        has_human_request or has_repair_request or has_device_unresolved
    ):
        return CustomerSupportDecision(CustomerSupportAction.CREATE_TICKET, "DEVICE_REPAIR_CONFIRMED")

    if has_device_symptom or has_repair_request:
        return CustomerSupportDecision(
            CustomerSupportAction.OFFER_WARRANTY_TROUBLESHOOTING,
            "DEVICE_TROUBLESHOOTING_FIRST",
            warranty_troubleshooting_answer(),
        )

    if has_refund and _has_marker(normalized, _REFUND_PROGRESS_MARKERS) and not has_human_request:
        return CustomerSupportDecision(
            CustomerSupportAction.SHOW_REFUND_PROGRESS,
            "REFUND_PROGRESS_OR_STATUS_QUERY",
            refund_progress_answer(),
        )

    if has_refund and has_refund_exception:
        return CustomerSupportDecision(CustomerSupportAction.CREATE_TICKET, "REFUND_EXCEPTION")

    if has_refund and not has_refund_exception:
        if has_human_request and _refund_self_service_already_offered(history):
            return CustomerSupportDecision(CustomerSupportAction.CREATE_TICKET, "REFUND_HUMAN_CONFIRMED")
        return CustomerSupportDecision(
            CustomerSupportAction.OFFER_REFUND_SELF_SERVICE,
            "REFUND_SELF_SERVICE_FIRST",
            refund_self_service_answer(),
        )

    if _has_marker(normalized, _TICKET_REQUIRED_MARKERS):
        return CustomerSupportDecision(CustomerSupportAction.CREATE_TICKET, "EXPLICIT_OR_EXCEPTIONAL_REQUEST")

    return CustomerSupportDecision(
        CustomerSupportAction.ASK_FOR_CLARIFICATION,
        "INSUFFICIENT_FACTS_FOR_TICKET",
        clarification_answer(),
    )
