"""将已筛选的 JDDC 退款候选导出为人工 Intent 标注 Markdown。

这个脚本只复制原始会话、当前用户消息和空标注位，不调用 IntentRouter，也不生成
任何预测标签。人工填写完成后，再由单独的导入脚本转换成正式 Gold JSONL。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_records(path: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(records) != 100:
        raise ValueError(f"候选文件必须正好包含 100 条，实际为 {len(records)} 条")
    ids = [record.get("id") for record in records]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("候选 case id 必须存在且唯一")
    return records


def _quote_history(history: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for message in history:
        role = "用户" if message.get("role") == "user" else "客服"
        content = str(message.get("content") or "").replace("\n", " ").strip()
        lines.append(f"> **{role}：** {content}")
    return lines


def _case_block(record: dict[str, Any], ordinal: int) -> str:
    case_id = str(record["id"])
    history = record.get("history") or []
    lines = [f"## {ordinal:03d}. `{case_id}`", ""]
    lines.append(f"来源会话：`{record.get('session_id', '')}`；当前轮次：`{record.get('turn_index', '')}`")
    lines.append("")
    if history:
        lines.append("**可见上下文**")
        lines.append("")
        lines.extend(_quote_history(history))
        lines.append("")
    lines.extend(
        [
            f"**当前用户：** {record.get('query', '')}",
            "",
            "### 请填写（只改 TODO，不要修改上面的原文）",
            "",
            "```json",
            json.dumps(
                {
                    "case_id": case_id,
                    "speech_act": "TODO",
                    "expected_requests": [],
                    "primary_goal": None,
                    "expected_workflow": None,
                    "needs_clarification": False,
                    "notes": "TODO",
                },
                ensure_ascii=False,
                indent=2,
            ),
            "```",
            "",
            "---",
            "",
        ]
    )
    return "\n".join(lines)


def _header(count: int) -> str:
    return f"""# JDDC 退款领域 Intent Gold 人工标注包

本文件是 **待人工标注数据**，不是 Gold。共 `{count}` 条，全部从 JDDC 基线公开脱敏
`data/chat.txt` 的不同会话中筛选；本文件没有使用当前 Router 的预测结果，也不把 JDDC
原始 `is_transfer`、`is_repeat`、`waiter_send` 当成本项目标签。

## 标注目标

本轮只标注 Intent / Router，不标注 required facts、capabilities、工具顺序、业务事实或最终客服回复。
请根据“可见上下文 + 当前用户”判断用户这一轮到底在做什么。

## 允许的 `speech_act`

只能填写以下 6 个值：

- `ACTION_REQUEST`：当前明确要求系统/客服做一件事，例如“我要退款”“取消退款”。
- `INFORMATION_QUERY`：当前询问状态、金额、去向、到账时间、资格、流程或原因。
- `STATEMENT`：只陈述已经发生的事实，没有新的问题或行动要求，例如“我已经申请退款了”。
- `ACKNOWLEDGEMENT`：结束、致谢、确认理解，例如“好的，谢谢”。
- `FUTURE_INTENTION`：将来打算做，但当前没有要求系统执行，例如“再等两天不行我就退款”。
- `CLARIFICATION_NEEDED`：当前意图不足以安全确定，例如“我不小心申请了退款”。

`STATEMENT`、`ACKNOWLEDGEMENT`、`FUTURE_INTENTION` 和 `CLARIFICATION_NEEDED` 的
`expected_requests` 必须为空；不要因为出现“退款”二字就制造退款请求。

## `expected_requests` 填写规则

1. 当前句语义明确时，以当前句为准；历史只用于补全“这个/那笔/什么时候”等指代，不能覆盖当前明确目标。
2. 普通单目标只填一个对象，格式为 `{{"domain": "refund", "operation": "status"}}`。
3. 多目标最多填 3 个，按客户目标的业务依赖顺序排列；不要把一个目标拆成多个工具调用。
4. `primary_goal` 填规范化的 `domain.operation`；没有业务请求时填 `null`。
5. 退款子域常用 operation：
   - `status`：退款是否成功、当前进度、退款了吗；
   - `expected_arrival`：什么时候到账、多久收到；
   - `destination`：退到哪里、原路退回哪个支付渠道；
   - `request`：明确现在要申请退款；
   - `cancel`：撤销/取消已经申请的退款；
   - `amount`：退款金额、少退、部分退款；
   - `eligibility`：当前订单是否符合退款资格；
   - `processing_time`：审核、受理或处理需要多久；
   - `anomaly`：失败、被拒、反复处理或状态异常；
   - `procedure`：如何申请、退款流程；
   - `clarify`：出现退款但当前没有明确目标；
   - `delivery_after_refund`：退款后是否还要签收/配送等交叉问题。
6. 跨域目标使用：退货/拒收/回仓后问退款用 `return.refund_dependency`；价保退款进度用
   `price_protection.refund_status`；不要为了一个细节创造新 operation。

## `expected_workflow` 规则

它不是简单拼接出来的字段，必须按当前源码 `resolve_workflow()` 的真实映射填写：

| canonical goal | 当前 Workflow |
|---|---|
| `refund.status` | `refund.refund_status` |
| `refund.amount` | `refund.refund_detail` |
| `refund.expected_arrival` | `refund.expected_arrival` |
| `refund.processing_time` | `refund.processing_time` |
| `refund.anomaly` | `refund.anomaly` |
| `refund.destination` | `refund.destination` |
| `refund.eligibility` | `refund.eligibility` |
| `refund.request` | `refund.request` |
| `refund.cancel` | `refund.cancel` |
| `return.refund_dependency` | `return.refund_dependency` |
| `price_protection.refund_status` | `price_protection.refund_status` |

当前没有注册 Workflow 的目标（例如 `refund.procedure`、`refund.clarify`）填 `null`，不能为了让
Benchmark 看起来完整而自造 Workflow。没有业务请求时同样填 `null`。

## `needs_clarification`

- 明确表达状态/问题/行动：`false`；即使后台以后缺工具，也不因此改成澄清。
- 当前无法知道用户想查什么或做什么：`true`，同时 `speech_act=CLARIFICATION_NEEDED`、
  `expected_requests=[]`、`primary_goal=null`、`expected_workflow=null`。
- 缺少订单号不自动等于需要澄清；如果目标明确，仍标目标，订单选择是执行阶段问题。

## 自检清单

- 是否把“已经申请退款”误标成 `refund.request`？如果只是陈述，应为 `STATEMENT` 且无 request。
- 是否把“再等两天不行就退款”误标成当前申请？应为 `FUTURE_INTENTION` 且无 request。
- 是否把“退款退到哪里”标成 `status`？应为 `destination`。
- 是否把“拒收后什么时候退款”只标成 `refund.expected_arrival`？应为 `return.refund_dependency`。
- 是否把 `primary_goal` 和 `expected_workflow` 强行写成同一个值？先查上面的真实 alias。

## 提交流程

1. 逐条填写每个 JSON 块中的 `TODO`。
2. 不改 case id、历史和当前用户原文。
3. 保留合法 JSON：字符串用双引号，空目标用 `null`，无请求用 `[]`。
4. 完成后把这份 Markdown 返回，用后续脚本转换为正式 Gold；在此之前不要拆 dev/holdout，避免边标边切分。

来源：<https://github.com/SimonJYang/JDDC-Baseline-Seq2Seq>（公开基线仓库中的脱敏 `data/chat.txt`）。

"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="refund-candidates-100.jsonl")
    parser.add_argument("--output", type=Path, required=True, help="人工填写的 Markdown")
    args = parser.parse_args()
    records = _load_records(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        _header(len(records)) + "\n".join(_case_block(record, index) for index, record in enumerate(records, start=1)),
        encoding="utf-8",
    )
    print(f"Exported {len(records)} refund annotation cases: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
