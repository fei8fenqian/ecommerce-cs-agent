"""把人工填写的 JDDC Ground Truth Markdown 导入结构化 v2 JSONL。"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_CASE_HEADING = re.compile(r"^##\s+\d+\.\s+(.+?)\s*$", re.MULTILINE)
_BULLET_FIELDS = {
    "用户目标": "user_goal.primary",
    "用户偏好的解决方式": "user_goal.preferred_resolution",
    "相关实体": "entities",
    "解决此问题前必须确认的事实": "required_facts",
    "所需能力及顺序": "expected_capabilities",
    "完成判据": "success_criteria",
    "completion type": "completion_type",
    "预期最终状态和说明": "expected_outcome",
    "转人工": "escalation",
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _clean(value: str) -> str:
    return value.strip().strip("`").strip()


def _split_items(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[、,，；;]", value) if item.strip()]


def _extract_bullet(body: str, prefix: str) -> str:
    pattern = re.compile(rf"^-\s*{re.escape(prefix)}[^：:]*[：:]\s*(.*)$", re.MULTILINE)
    match = pattern.search(body)
    return _clean(match.group(1)) if match else ""


def _extract_section(body: str, title: str, following_title: str | None) -> str:
    start = body.find(f"### {title}")
    if start < 0:
        return ""
    start += len(f"### {title}")
    end = body.find(f"### {following_title}", start) if following_title else len(body)
    if end < 0:
        end = len(body)
    return body[start:end].strip()


def _extract_code_path(section: str) -> list[str]:
    match = re.search(r"```(?:text)?\s*(.*?)```", section, flags=re.DOTALL)
    if not match:
        return []
    raw = match.group(1).strip()
    if not raw:
        return []
    return [step.strip() for step in re.split(r"\s*(?:→|->)\s*", raw) if step.strip()]


def _invalid_paths(section: str) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for raw_line in re.findall(r"^-\s+(.+)$", section, flags=re.MULTILINE):
        for item in _split_items(raw_line):
            if "：" in item:
                path, reason = item.split("：", 1)
            elif ":" in item:
                path, reason = item.split(":", 1)
            else:
                path, reason = item, ""
            values.append({"path": _clean(path), "reason": _clean(reason)})
    return values


def _notes(section: str) -> str:
    lines = [line[2:].strip() for line in section.splitlines() if line.startswith("- ")]
    return "\n".join(line for line in lines if line)


def _parse_escalation(value: str) -> dict[str, Any]:
    normalized = value.replace(" ", "")
    required: bool | None
    if normalized.startswith("必需") or normalized.startswith("需要"):
        required = True
    elif normalized.startswith("不必需") or normalized.startswith("不需要") or normalized.startswith("否"):
        required = False
    else:
        required = None
    allowed: list[str] = []
    forbidden: list[str] = []
    for segment in _split_items(value):
        if "允许" in segment or "可以" in segment or "可" in segment:
            allowed.append(segment)
        if "禁止" in segment or "不得" in segment:
            forbidden.append(segment)
    return {"required": required, "allowed_if": allowed, "forbidden_if": forbidden}


def _parse_outcome(value: str) -> dict[str, str]:
    if not value:
        return {"status": "", "description": ""}
    for separator in ("；", ";", "，", ","):
        if separator in value:
            status, description = value.split(separator, 1)
            return {"status": _clean(status), "description": _clean(description)}
    return {"status": _clean(value), "description": ""}


def _parse_markdown(markdown: str) -> dict[str, dict[str, Any]]:
    matches = list(_CASE_HEADING.finditer(markdown))
    parsed: dict[str, dict[str, Any]] = {}
    for index, match in enumerate(matches):
        case_id = match.group(1)
        body = markdown[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(markdown)]
        values = {key: _extract_bullet(body, key) for key in _BULLET_FIELDS}
        paths_section = _extract_section(body, "合法执行路径", "不能接受的路径或承诺")
        invalid_section = _extract_section(body, "不能接受的路径或承诺", "最终回复要求 / 备注")
        notes_section = _extract_section(body, "最终回复要求 / 备注", None)
        valid_path = _extract_code_path(paths_section)
        invalid_paths = _invalid_paths(invalid_section)
        parsed[case_id] = {
            "user_goal": {
                "primary": values["用户目标"],
                "preferred_resolution": values["用户偏好的解决方式"],
            },
            "entities": [{"type": item, "status": "relevant"} for item in _split_items(values["相关实体"])],
            "required_facts": [
                {"fact": item, "required": True} for item in _split_items(values["解决此问题前必须确认的事实"])
            ],
            "expected_capabilities": [
                {"capability": item, "required": True, "order": order}
                for order, item in enumerate(
                    [item.strip() for item in re.split(r"\s*(?:→|->)\s*", values["所需能力及顺序"]) if item.strip()],
                    start=1,
                )
            ],
            "valid_solution_paths": [valid_path] if valid_path else [],
            "invalid_solution_paths": invalid_paths,
            "business_constraints": [
                f"{item['path']}：{item['reason']}" if item["reason"] else item["path"] for item in invalid_paths
            ],
            "success_criteria": _split_items(values["完成判据"]),
            "completion_type": values["completion type"],
            "escalation": _parse_escalation(values["转人工"]),
            "expected_outcome": _parse_outcome(values["预期最终状态和说明"]),
            "expected_response_notes": _notes(notes_section),
        }
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True, help="未标注的 v2 JSONL 模板")
    parser.add_argument("--markdown", type=Path, required=True, help="人工填写后的 Markdown")
    parser.add_argument("--output", type=Path, required=True, help="新的金标 JSONL；不会覆盖输入")
    parser.add_argument("--annotator", default="user", help="标注者标识")
    args = parser.parse_args()
    if not args.template.is_file() or not args.markdown.is_file():
        parser.error("template 和 markdown 必须存在")

    annotations = _parse_markdown(args.markdown.read_text(encoding="utf-8"))
    records = _load_jsonl(args.template)
    source_ids = {str(record["id"]) for record in records}
    if source_ids != set(annotations):
        missing = source_ids - set(annotations)
        extra = set(annotations) - source_ids
        parser.error(f"案例 id 不一致：missing={len(missing)} extra={len(extra)}")

    reviewed_at = datetime.now(timezone.utc).isoformat()
    incomplete: list[str] = []
    for record in records:
        annotation = annotations[str(record["id"])]
        if not annotation["user_goal"]["primary"] or not annotation["completion_type"]:
            incomplete.append(str(record["id"]))
        record["ground_truth"] = annotation
        record["annotation_status"] = "reviewed"
        record["annotator"] = args.annotator
        record["reviewed_at"] = reviewed_at
    if incomplete:
        parser.error(f"有 {len(incomplete)} 条缺少用户目标或 completion type，未写输出")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"已导入 {len(records)} 条金标：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
