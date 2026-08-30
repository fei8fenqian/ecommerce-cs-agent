"""生成客户售后盲测输入集和独立人工判定集。

输入文件不包含 expected_action，避免开发时只盯着答案造题；判定文件按稳定 case_id
单独保存。表达模板取自公开电商客服语料中常见的短句、省略、催促、重复和口语现象，
不是复制公开数据原文。需要扩充时先增加用户原话模板，再独立填写 oracle 动作。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_PATH = ROOT / "evals" / "customer_support_cases.jsonl"
ORACLE_PATH = ROOT / "evals" / "customer_support_oracle.jsonl"


# 这些是脱敏后的自然表达模式，不是模型回答。每类故意混入口语、错省略、催促和
# 语气词；动作标签在另一份 oracle 中保存，运行评测时按 id 关联。
GROUPS: list[tuple[str, str, str, list[str], bool]] = [
    (
        "refund_self_service",
        "ticket",
        "OFFER_REFUND_SELF_SERVICE",
        [
            "我要申请退款",
            "我想退货",
            "不想要了能退吗",
            "最近买的那个我想退掉",
            "怎么把这单退了",
            "退钱的入口在哪",
            "能不能把刚拍的退了",
            "订单不要了想退",
            "想退最近那一单但不知道单号",
            "给我说下怎么退款",
            "我想退货，不知道去哪操作",
            "退款怎么申请",
            "我要把订单退掉",
            "这件不要了，想退款",
            "这个能退不",
            "我想取消后退款",
            "申请退货",
            "想退钱",
        ],
        False,
    ),
    (
        "refund_progress",
        "ticket",
        "SHOW_REFUND_PROGRESS",
        [
            "已经退了",
            "已申请退款",
            "我都申请过退款了",
            "退货已经寄出",
            "退款进度在哪看",
            "退款状态查一下",
            "退款到账了吗",
            "退款到账没",
            "哪里看退款",
            "我已经申请退货",
            "这个退款怎么查",
            "退货寄走了现在到哪一步",
        ],
        False,
    ),
    (
        "ticket_exception",
        "ticket",
        "CREATE_TICKET",
        [
            "退款没到账",
            "退款金额不对",
            "退款页面报错",
            "退款点了没反应",
            "看不到退款",
            "退款申请不了",
            "退款失败",
            "退款一直没回来",
            "退的钱少了",
            "支付宝重复扣款了",
            "付款失败但是钱扣了",
            "订单显示没付但我已经付款",
            "我要投诉你们",
            "这事你们得赔偿",
            "电脑坏了，帮我报修",
            "给我保修",
            "我要取消订单",
            "地址填错了要改",
            "我要申请维修",
            "转人工客服",
        ],
        False,
    ),
    (
        "ticket_after_guidance",
        "ticket",
        "CREATE_TICKET",
        [
            "仍需人工",
            "还是要人工",
            "退款页面我不想弄，转人工",
            "我就要真人处理",
        ],
        True,
    ),
    (
        "ambiguous_ticket",
        "ticket",
        "ASK_FOR_CLARIFICATION",
        [
            "这个事情你们看着办",
            "售后有点问题帮我弄下",
            "我有个事",
            "帮我看看怎么办",
            "订单这边不对劲",
            "商品有问题",
            "售后呢",
            "能不能处理一下",
            "有问题找谁",
            "我不满意",
            "这个咋整",
            "帮忙看一下呗",
        ],
        False,
    ),
    (
        "non_ticket_customer",
        "rag",
        "CONTINUE",
        [
            "笔记本怎么开机",
            "退货政策是什么",
            "保修期多久",
            "这个手机支持什么网络",
            "支付方式有哪些",
            "怎么查看物流",
            "商品参数发我看看",
            "有没有现货",
            "帮我推荐一台游戏本",
            "这款和上一款差在哪",
        ],
        False,
    ),
    (
        "staff_or_other_route",
        "agent",
        "CONTINUE",
        [
            "请查一下库存",
            "帮我看今天退款异常",
            "把本周售后汇总一下",
            "运营日报准备好了吗",
            "这个订单需要财务核对",
            "查一下支付状态",
            "给我当前待处理工单",
            "仓库还有多少台",
            "看一下商品销量",
            "把异常订单列出来",
        ],
        False,
    ),
]


def _variants(base: str) -> list[str]:
    """模拟真实客服短句的语气变化，保持核心事实不被模板改写。"""
    candidates = (
        base,
        f"请问{base}",
        f"那个，{base}",
        f"{base}啊",
        f"{base}呢",
        f"麻烦问下，{base}",
        f"喂，{base}",
        f"{base}，麻烦了",
        f"{base}，急用",
        f"{base}？",
    )
    return list(dict.fromkeys(candidates))


def main() -> int:
    inputs: list[dict[str, object]] = []
    oracle: list[dict[str, object]] = []
    seen_queries: set[tuple[str, str, str, bool]] = set()
    sequence = 0

    for group_name, proposed_target, expected_action, bases, guidance in GROUPS:
        for base in bases:
            for variant_no, query in enumerate(_variants(base), start=1):
                role = "agent" if group_name == "staff_or_other_route" else "customer"
                key = (query, proposed_target, role, guidance)
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                sequence += 1
                case_id = f"cs-{sequence:04d}-{group_name}-{variant_no:02d}"
                inputs.append(
                    {
                        "id": case_id,
                        "query": query,
                        "proposed_target": proposed_target,
                        "role": role,
                        "refund_guidance_was_shown": guidance,
                    }
                )
                oracle.append({"id": case_id, "expected_action": expected_action})

    INPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    INPUT_PATH.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in inputs),
        encoding="utf-8",
    )
    ORACLE_PATH.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in oracle),
        encoding="utf-8",
    )
    print(f"Generated {len(inputs)} blind inputs and {len(oracle)} oracle labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
