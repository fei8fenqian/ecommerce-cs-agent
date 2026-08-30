"""Import the annotated JDDC refund Markdown pack into JSONL.

The refund pack intentionally keeps the original JDDC ``history`` and ``query``
from a JSONL template, while this script parses only the human-authored Gold
fields from Markdown.  It does not infer missing labels.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CASE_HEADING = re.compile(r"^##\s+(\d+)\.\s+(\S+)\s*$", re.MULTILINE)
_FIELD_PATTERNS = {
    "domain": r"^-\s*domain（.*?）：\s*(.*)$",
    "operation": r"^-\s*operation（.*?）：\s*(.*)$",
    "user_goal": r"^-\s*用户目标：\s*(.*)$",
    "relevant_entities": r"^-\s*相关实体（.*?）：\s*(.*)$",
    "required_facts": r"^-\s*解决此问题前必须确认的事实（.*?）：\s*(.*)$",
    "required_capabilities": r"^-\s*所需业务能力及顺序（.*?）：\s*(.*)$",
    "expected_case_status": r"^-\s*预期 Case 状态（.*?）：\s*(.*)$",
    "expected_final_outcome": r"^-\s*预期最终结果：\s*(.*)$",
    "escalation_required": r"^-\s*是否必须转人工（.*?）：\s*(.*)$",
}
_ALLOWED_CASE_STATUSES = {
    "ACTIVE",
    "AWAITING_CUSTOMER",
    "AWAITING_STAFF",
    "COMPLETED",
    "FAILED",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _clean(value: str) -> str:
    return value.strip().strip("`").strip()


def _field(body: str, key: str) -> str:
    match = re.search(_FIELD_PATTERNS[key], body, flags=re.MULTILINE)
    # Keep backticks for list-valued fields such as
    # ``fact_a``、``fact_b``; stripping the outer characters here would make
    # the separators look like the captured values.
    return match.group(1).strip() if match else ""


def _split_list(value: str) -> list[str]:
    value = value.strip()
    if not value or value.startswith("无"):
        return []
    code_items = re.findall(r"`([^`]+)`", value)
    if code_items:
        return [item.strip() for item in code_items if item.strip()]
    return [item.strip() for item in re.split(r"[、,，；;]", value) if item.strip()]


def _parse_capabilities(value: str) -> list[dict[str, Any]]:
    value = _clean(value)
    if not value or value.startswith("无"):
        return []
    steps = [step.strip() for step in re.split(r"\s*(?:→|->)\s*", value) if step.strip()]
    return [{"capability": step, "required": True, "order": order} for order, step in enumerate(steps, start=1)]


def _parse_paths(section: str) -> list[list[str]]:
    code_match = re.search(r"```(?:text)?\s*(.*?)```", section, flags=re.DOTALL)
    if not code_match:
        return []
    paths: list[list[str]] = []
    for raw_line in code_match.group(1).splitlines():
        line = re.sub(r"^\s*\d+[.)]\s*", "", raw_line).strip()
        if not line:
            continue
        steps = [step.strip() for step in re.split(r"\s*(?:→|->)\s*", line) if step.strip()]
        if steps:
            paths.append(steps)
    return paths


def _section(body: str, heading: str, next_heading: str) -> str:
    start_marker = f"### {heading}"
    end_marker = f"### {next_heading}"
    start = body.find(start_marker)
    if start < 0:
        return ""
    start += len(start_marker)
    end = body.find(end_marker, start)
    return body[start : end if end >= 0 else len(body)].strip()


def _parse_invalid_paths(section: str) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("-"):
            continue
        line = line[1:].strip()
        if not line:
            continue
        if line.startswith("路径："):
            line = line[len("路径：") :]
        elif line.startswith("路径:"):
            line = line[len("路径:") :]
        reason_match = re.search(r"[；;]\s*原因[：:]\s*", line)
        if reason_match:
            path = line[: reason_match.start()].strip()
            reason = line[reason_match.end() :].strip()
        else:
            path, reason = line, ""
        values.append({"path": path, "reason": reason})
    return values


def _parse_constraints(body: str) -> list[str]:
    inline = [_clean(match.group(1)) for match in re.finditer(r"^-\s*业务约束：\s*(.+)$", body, flags=re.MULTILINE)]
    if inline:
        return inline
    marker = re.search(r"^-\s*业务约束：\s*$", body, flags=re.MULTILINE)
    if not marker:
        return []
    section = body[marker.end() :]
    section = section[: section.find("### 合法执行路径")] if "### 合法执行路径" in section else section
    return [
        line.strip()[1:].strip()
        for line in section.splitlines()
        if line.strip().startswith("-") and line.strip()[1:].strip()
    ]


def _parse_notes(section: str) -> tuple[str, str]:
    response = re.search(r"^-\s*最终回复要求：\s*(.*)$", section, flags=re.MULTILINE)
    note = re.search(r"^-\s*标注备注：\s*(.*)$", section, flags=re.MULTILINE)
    if not response and not note:
        # 当前退款标注模板只有一个“最终回复要求 / 标注备注”区块，
        # 人工通常直接填写一个 bullet；兼容这种格式，不要求重复填写两遍。
        generic = [line[1:].strip() for line in section.splitlines() if line.strip().startswith("-")]
        return "\n".join(line for line in generic if line), ""
    return (
        _clean(response.group(1)) if response else "",
        _clean(note.group(1)) if note else "",
    )


def _parse_markdown(markdown: str) -> dict[str, dict[str, Any]]:
    matches = list(_CASE_HEADING.finditer(markdown))
    annotations: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        case_id = match.group(2)
        body = markdown[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(markdown)]
        values = {key: _field(body, key) for key in _FIELD_PATTERNS}
        notes_section = _section(body, "最终回复要求 / 标注备注", "")
        response_notes, annotator_note = _parse_notes(notes_section)
        invalid_section = _section(body, "不能接受的路径或承诺", "最终回复要求 / 标注备注")
        annotations[case_id] = {
            "domain": _clean(values["domain"]),
            "operation": _clean(values["operation"]),
            "user_goal": _clean(values["user_goal"]),
            "relevant_entities": _split_list(values["relevant_entities"]),
            "required_facts": [{"fact": fact, "required": True} for fact in _split_list(values["required_facts"])],
            "required_capabilities": _parse_capabilities(values["required_capabilities"]),
            "valid_solution_paths": _parse_paths(_section(body, "合法执行路径", "不能接受的路径或承诺")),
            "invalid_solution_paths": _parse_invalid_paths(invalid_section),
            "business_constraints": _parse_constraints(body),
            "expected_case_status": _clean(values["expected_case_status"]),
            "expected_final_outcome": _clean(values["expected_final_outcome"]),
            "escalation_required": _clean(values["escalation_required"]),
            "expected_response_notes": response_notes,
            "annotator_note": annotator_note,
        }
    return annotations


def _validate(records: list[dict[str, Any]], annotations: dict[str, dict[str, Any]]) -> None:
    source_ids = [str(record.get("id", "")) for record in records]
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("template 中存在重复 id")
    source_set = set(source_ids)
    annotation_set = set(annotations)
    if source_set != annotation_set:
        missing = sorted(source_set - annotation_set)
        extra = sorted(annotation_set - source_set)
        raise ValueError(f"案例 id 不一致：missing={missing[:3]} extra={extra[:3]}")

    incomplete: list[str] = []
    invalid_status: list[str] = []
    for case_id, annotation in annotations.items():
        required = (
            "domain",
            "operation",
            "user_goal",
            "expected_case_status",
            "expected_final_outcome",
            "escalation_required",
            "expected_response_notes",
        )
        if any(not annotation[field] for field in required):
            incomplete.append(case_id)
        if annotation["expected_case_status"] not in _ALLOWED_CASE_STATUSES:
            invalid_status.append(case_id)
    if incomplete:
        raise ValueError(f"有 {len(incomplete)} 条核心字段为空，例如 {incomplete[:3]}")
    if invalid_status:
        raise ValueError(f"有 {len(invalid_status)} 条 Case 状态不在允许集合中，例如 {invalid_status[:3]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--annotator", default="user")
    args = parser.parse_args()

    if not args.template.is_file() or not args.markdown.is_file():
        parser.error("template 和 markdown 必须存在")

    records = _load_jsonl(args.template)
    annotations = _parse_markdown(args.markdown.read_text(encoding="utf-8"))
    if len(annotations) != 100:
        raise ValueError(f"Markdown 解析出 {len(annotations)} 条，不是预期的 100 条")
    _validate(records, annotations)

    reviewed_at = datetime.now(timezone.utc).isoformat()
    for record in records:
        record["ground_truth"] = annotations[str(record["id"])]
        record["annotation_status"] = "reviewed"
        record["annotator"] = args.annotator
        record["reviewed_at"] = reviewed_at

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Imported {len(records)} annotated JDDC refund cases: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
