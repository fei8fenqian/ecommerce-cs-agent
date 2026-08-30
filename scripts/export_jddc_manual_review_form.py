"""导出与在线 trace 对齐的 JDDC 人工仲裁选择题文档。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ACTIONS = (
    ("1", "CONTINUE", "继续处理：普通咨询，直接回答或调用只读工具"),
    ("2", "OFFER_REFUND_SELF_SERVICE", "引导退款自助：首次退款/退货，给订单入口让用户自行申请"),
    ("3", "SHOW_REFUND_PROGRESS", "查询退款进度：已经申请/寄回/退款未到账，查询当前状态"),
    ("4", "OFFER_WARRANTY_TROUBLESHOOTING", "故障排查/保修引导：先安全排查并收集设备事实"),
    ("5", "ASK_FOR_CLARIFICATION", "澄清问题：信息不足或意图不明，先追问关键事实"),
    ("6", "CREATE_TICKET", "创建人工工单：明确人工/投诉/报修，或已确认售后异常"),
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def render_case(record: dict[str, Any], number: int) -> str:
    lines = [f"## {number}. {record['id']}", ""]
    history = record.get("history") or []
    if history:
        lines.append("**可见上下文**")
        lines.append("")
        for message in history:
            role = "用户" if message.get("role") == "user" else "客服"
            content = str(message.get("content") or "").replace("\n", " ")
            lines.append(f"> **{role}：** {content}")
        lines.append("")
    lines.extend([f"**当前用户：** {record.get('query', '')}", "", "**请选择一个动作：**", ""])
    for key, _action, description in ACTIONS:
        lines.append(f"- [ ] **{key}**：{description}")
    lines.extend(["", "**你的判定编号：** `____`", "", "**备注（可空）：**", "", "---", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    source_by_id = {record["id"]: record for record in load_jsonl(args.source)}
    trace = json.loads(args.trace.read_text(encoding="utf-8"))
    results = trace.get("results") or []
    selected: list[dict[str, Any]] = []
    for result in results[: args.limit]:
        record = source_by_id.get(result.get("id"))
        if record is None:
            raise SystemExit(f"trace 中的样本不在 source：{result.get('id')}")
        selected.append(record)

    header = [
        "# JDDC 3C 客服 Agent：30 条人工仲裁表",
        "",
        "> 这是盲审表。只根据用户原话和上下文判断，不参考机器初标、Agent 实际输出或预期标签。",
        "> 每题在“你的判定编号”填写 1–6；不确定时仍选择最合适的一项，并在备注说明。",
        "",
        "## 动作定义",
        "",
    ]
    for key, _action, description in ACTIONS:
        header.append(f"{key}. **{description}**。")
    header.extend(["", f"共 {len(selected)} 题。", ""])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "\n".join(header + [render_case(record, i) for i, record in enumerate(selected, 1)]), encoding="utf-8"
    )
    print(f"已生成 {args.output}，共 {len(selected)} 题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
