"""在 JDDC 脱敏真实客服会话上做 3C 客服安全扫描。

JDDC 基线仓库的 ``data/chat.txt`` 约含一万组真实客服会话，并保留用户/客服角色。
脚本筛出包含 3C 商品线索的会话，每个会话选一条最早的可行动用户消息，保留前置上下文，
再强制模拟路由器提出 ``ticket`` 建议。JDDC 原始标签不是本项目动作金标，因此只报告
安全不变量和主题分布，不伪装成动作准确率。

运行示例：
    PYTHONPATH=src .venv/bin/python scripts/run_jddc_customer_support_eval.py \
        --source /path/to/data/chat.txt
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from service.customer_support_policy import CustomerSupportAction, decide_customer_support_action

_THREE_C = re.compile(
    r"手机|电脑|笔记本|平板|手表|相机|耳机|充电器|充电宝|键盘|鼠标|显示器|路由器|硬盘|"
    r"内存|显卡|主板|电源|数据线|音箱|电视|投影仪|打印机|游戏机|无人机|摄像头|数码|"
    r"电子|苹果|华为|小米|联想|戴尔|惠普|三星|荣耀|魅族|vivo|oppo|一加|诺基亚|华硕|"
    r"雷蛇|罗技|索尼|佳能|尼康|Kindle",
    re.IGNORECASE,
)
_TOPICS: dict[str, re.Pattern[str]] = {
    "device_issue": re.compile(
        r"坏了|故障|不能用|无法识别|无法使用|开不了机|黑屏|死机|充不进|不充电|漏电|"
        r"蓝屏|发热|不灵敏|没反应|损坏|质量问题|不正常|出问题|失灵|花屏|闪退|卡顿|"
        r"信号不好|按键.*(失灵|没反应)|电池.*(坏|问题)",
    ),
    "after_sales_process": re.compile(r"保修|质保|维修|修理|报修|售后|检测|换货|换新|退换"),
    "refund_return": re.compile(r"退款|退货|退钱|退回|不要了|退掉|退换|换货|价保|保价"),
    "logistics_delivery": re.compile(r"物流|快递|发货|配送|到货|送到|运单|签收|没收到|几天|缺货"),
    "payment_order": re.compile(r"支付|付款|扣款|下单|订单|发票|开票|白条|银行卡|地址|改价|取消"),
    "product_pre_sales": re.compile(r"参数|型号|规格|兼容|区别|哪个好|推荐|价格|优惠|怎么用|使用|安装"),
    "complaint_human": re.compile(r"投诉|人工|真人|客服|生气|骗人|垃圾|差评|举报|赔偿|纠纷|争议"),
}
_EXPLICIT_SUPPORT = re.compile(
    r"人工|真人|客服|投诉|纠纷|争议|赔偿|报修|维修|售后|保修|检测|换货|换新|"
    r"冒烟|起火|着火|爆炸|鼓包|漏电|烧焦|坏了|故障|不能用|无法使用|开不了机|"
    r"没反应|损坏|不灵敏|退款失败|退款没到账|退款未到账|支付失败|付款失败|订单异常|"
    r"扣款|少退|退少|改地址|修改地址|地址填错|取消订单|申请取消|申请失败|不能申请退款|退不了|退款申请不了",
)
_DANGER = re.compile(r"冒烟|起火|着火|爆炸|电池鼓包|漏电|烧焦")
_NOISE = re.compile(r"^(谢谢|好的|嗯+|哦+|在吗[？?！!。．]*|没了|没有了|\?+|？+)$")


@dataclass(frozen=True)
class Turn:
    role: str
    content: str


@dataclass(frozen=True)
class SupportCase:
    session_id: str
    turn_index: int
    query: str
    history: tuple[Turn, ...]
    topics: tuple[str, ...]


def _read_sessions(path: Path) -> dict[str, list[Turn]]:
    sessions: dict[str, list[Turn]] = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        next(handle, None)
        for raw in handle:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) < 7:
                continue
            session_id, _user_id, waiter_send = parts[:3]
            content = parts[-1].strip()
            if content:
                sessions[session_id].append(Turn(role="assistant" if waiter_send == "1" else "user", content=content))
    return dict(sessions)


def _select_cases(
    sessions: dict[str, list[Turn]],
    *,
    seed: int,
    min_history_turns: int = 0,
) -> list[SupportCase]:
    """Select one actionable user turn per session with an optional context floor."""

    if min_history_turns < 0:
        raise ValueError("min_history_turns must be non-negative")
    cases: list[SupportCase] = []
    for session_id, turns in sessions.items():
        if not any(_THREE_C.search(turn.content) for turn in turns):
            continue
        candidates = [
            (index, turn)
            for index, turn in enumerate(turns)
            if index >= min_history_turns and turn.role == "user" and not _NOISE.fullmatch(turn.content.strip())
        ]
        if not candidates:
            continue
        index, selected = next(
            (item for item in candidates if any(pattern.search(item[1].content) for pattern in _TOPICS.values())),
            candidates[0],
        )
        topics = tuple(name for name, pattern in _TOPICS.items() if pattern.search(selected.content))
        cases.append(
            SupportCase(
                session_id=session_id,
                turn_index=index,
                query=selected.content,
                history=tuple(turns[:index]),
                topics=topics or ("other_3c",),
            )
        )
    random.Random(seed).shuffle(cases)
    return cases


def _scan(cases: list[SupportCase]) -> dict[str, object]:
    action_counts: Counter[str] = Counter()
    topic_counts: Counter[str] = Counter()
    violations: list[dict[str, object]] = []
    for case in cases:
        history = [{"role": turn.role, "content": turn.content} for turn in case.history]
        decision = decide_customer_support_action(
            intent_target="ticket",
            role="customer",
            query=case.query,
            history=history,
        )
        action = CustomerSupportAction(decision.action).value
        action_counts[action] += 1
        topic_counts.update(case.topics)
        context = "".join(turn.content for turn in case.history[-8:] if turn.role == "user")
        has_explicit_support = bool(_EXPLICIT_SUPPORT.search(f"{context}{case.query}"))
        has_danger = bool(_DANGER.search(case.query))
        violation: str | None = None
        if action == CustomerSupportAction.CREATE_TICKET.value and not has_explicit_support:
            violation = "CREATE_TICKET_WITHOUT_EXPLICIT_SUPPORT_SIGNAL"
        elif has_danger and action != CustomerSupportAction.CREATE_TICKET.value:
            violation = "DANGER_NOT_ESCALATED"
        elif "device_issue" in case.topics and action == CustomerSupportAction.ASK_FOR_CLARIFICATION.value:
            violation = "DEVICE_CASE_NOT_ROUTED_TO_TROUBLESHOOTING"
        if violation:
            violations.append(
                {
                    "id": f"jddc-{case.session_id}-{case.turn_index}",
                    "session_id": case.session_id,
                    "turn_index": case.turn_index,
                    "topics": list(case.topics),
                    "query": case.query,
                    "history": [{"role": turn.role, "content": turn.content} for turn in case.history[-8:]],
                    "action": action,
                    "reason": decision.reason,
                    "violation": violation,
                }
            )
    return {
        "evaluated": len(cases),
        "topic_counts": dict(sorted(topic_counts.items())),
        "actions": dict(sorted(action_counts.items())),
        "violations": violations,
        "violation_count": len(violations),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="JDDC data/chat.txt")
    parser.add_argument("--limit", type=int, default=0, help="最多评测多少个会话；0 表示全部（默认）")
    parser.add_argument("--seed", type=int, default=20260827, help="抽样种子")
    parser.add_argument(
        "--min-history-turns",
        type=int,
        default=0,
        help="只选择前面至少已有多少条对话消息的用户轮；0 表示不限制",
    )
    parser.add_argument("--json-out", type=Path, help="可选：把扫描结果写到本地文件")
    args = parser.parse_args()
    if args.limit < 0 or args.min_history_turns < 0:
        parser.error("limit and min-history-turns must be non-negative")
    if not args.source.is_file():
        parser.error(f"JDDC source does not exist: {args.source}")

    sessions = _read_sessions(args.source)
    all_cases = _select_cases(sessions, seed=args.seed, min_history_turns=args.min_history_turns)
    cases = all_cases
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        parser.error("JDDC source contains no 3C customer-support sessions")
    result = {
        "dataset": "JDDC baseline anonymized customer-service corpus",
        "evaluation_kind": "deterministic_gate_invariant_scan",
        "agent_execution": False,
        "llm_calls": 0,
        "source_labels_compared": False,
        "action_accuracy": None,
        "source": str(args.source),
        "seed": args.seed,
        "source_sessions": len(sessions),
        "three_c_sessions": len(all_cases),
        **_scan(cases),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered + "\n", encoding="utf-8")
    return 1 if result["violation_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
