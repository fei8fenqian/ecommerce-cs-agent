"""校验人工完成的 JDDC 退款 Intent 标注并导出正式 Gold JSONL。

本脚本只读取人工填写的字段和 source candidates，不调用 IntentRouter，也不会用模型预测
补全或修改标注。``expected_workflow`` 通过当前 Control Plane 的 resolve_workflow 校验。
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from agent.goal_taxonomy import is_canonical_goal, workflow_key_for_goal
from agent.support_control import resolve_workflow

ALLOWED_SPEECH_ACTS = {
    "ACTION_REQUEST",
    "INFORMATION_QUERY",
    "STATEMENT",
    "ACKNOWLEDGEMENT",
    "FUTURE_INTENTION",
    "CLARIFICATION_NEEDED",
}
NON_ACTIONABLE_SPEECH_ACTS = {
    "STATEMENT",
    "ACKNOWLEDGEMENT",
    "FUTURE_INTENTION",
    "CLARIFICATION_NEEDED",
}
JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _load_candidates(path: Path) -> dict[str, dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        case_id = record.get("id")
        if not isinstance(case_id, str) or not case_id or case_id in candidates:
            raise ValueError(f"候选文件存在缺失或重复 id: {case_id!r}")
        candidates[case_id] = record
    if len(candidates) != 100:
        raise ValueError(f"候选文件必须包含 100 条，实际为 {len(candidates)} 条")
    return candidates


def _load_annotations(path: Path) -> list[dict[str, Any]]:
    blocks = JSON_BLOCK_RE.findall(path.read_text(encoding="utf-8"))
    if len(blocks) != 100:
        raise ValueError(f"标注 Markdown 必须包含 100 个 JSON 标注块，实际为 {len(blocks)} 个")
    annotations: list[dict[str, Any]] = []
    for index, block in enumerate(blocks, start=1):
        try:
            record = json.loads(block)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {index} 个标注块不是合法 JSON: {exc}") from exc
        annotations.append(record)
    return annotations


def _validate_request(request: Any, *, case_id: str, index: int) -> dict[str, str]:
    if not isinstance(request, dict):
        raise ValueError(f"{case_id}: expected_requests[{index}] 必须是对象")
    domain = request.get("domain")
    operation = request.get("operation")
    if not isinstance(domain, str) or not domain.strip() or not isinstance(operation, str) or not operation.strip():
        raise ValueError(f"{case_id}: expected_requests[{index}] 必须包含 domain 和 operation")
    domain = domain.strip()
    operation = operation.strip()
    if not is_canonical_goal(domain, operation):
        raise ValueError(f"{case_id}: expected_requests[{index}] 不是 canonical Goal: {domain}.{operation}")
    return {"domain": domain, "operation": operation}


def _validate_annotation(annotation: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    case_id = annotation.get("case_id")
    if not isinstance(case_id, str) or case_id not in candidates:
        raise ValueError(f"标注中的 case_id 不在候选集中: {case_id!r}")

    speech_act = annotation.get("speech_act")
    if speech_act not in ALLOWED_SPEECH_ACTS:
        raise ValueError(f"{case_id}: 非法 speech_act: {speech_act!r}")

    raw_requests = annotation.get("expected_requests")
    if not isinstance(raw_requests, list) or len(raw_requests) > 3:
        raise ValueError(f"{case_id}: expected_requests 必须是最多 3 条的数组")
    requests = [_validate_request(item, case_id=case_id, index=index) for index, item in enumerate(raw_requests)]

    if speech_act in NON_ACTIONABLE_SPEECH_ACTS and requests:
        raise ValueError(f"{case_id}: {speech_act} 不允许生成 expected_requests")
    if speech_act == "CLARIFICATION_NEEDED" and annotation.get("needs_clarification") is not True:
        raise ValueError(f"{case_id}: CLARIFICATION_NEEDED 必须 needs_clarification=true")
    if speech_act != "CLARIFICATION_NEEDED" and annotation.get("needs_clarification") is not False:
        raise ValueError(f"{case_id}: 非 CLARIFICATION_NEEDED 必须 needs_clarification=false")

    primary_goal = annotation.get("primary_goal")
    expected_workflow = annotation.get("expected_workflow")
    if requests:
        expected_primary_goal = f"{requests[0]['domain']}.{requests[0]['operation']}"
        if primary_goal != expected_primary_goal:
            raise ValueError(f"{case_id}: primary_goal 应为 {expected_primary_goal!r}，实际为 {primary_goal!r}")
        taxonomy_workflow = workflow_key_for_goal(requests[0]["domain"], requests[0]["operation"])
        workflow = resolve_workflow(requests[0])
        actual_workflow = workflow.key if workflow is not None else None
        if actual_workflow != taxonomy_workflow:
            raise ValueError(
                f"{case_id}: Goal taxonomy Workflow 为 {taxonomy_workflow!r}，"
                f"但 Control Plane resolve_workflow 为 {actual_workflow!r}"
            )
        if expected_workflow != actual_workflow:
            raise ValueError(f"{case_id}: expected_workflow 应为 {actual_workflow!r}，实际为 {expected_workflow!r}")
    elif primary_goal is not None or expected_workflow is not None:
        raise ValueError(f"{case_id}: 无 expected_requests 时 primary_goal 和 expected_workflow 必须为 null")

    notes = annotation.get("notes")
    if not isinstance(notes, str) or not notes.strip() or "TODO" in notes:
        raise ValueError(f"{case_id}: notes 不能为空且不能保留 TODO")

    candidate = candidates[case_id]
    return {
        "case_id": case_id,
        "dataset": candidate.get("dataset", "JDDC"),
        "session_id": candidate["session_id"],
        "turn_index": candidate["turn_index"],
        "history": candidate.get("history", []),
        "query": candidate["query"],
        "speech_act": speech_act,
        "expected_requests": requests,
        "primary_goal": primary_goal,
        "expected_workflow": expected_workflow,
        "needs_clarification": annotation["needs_clarification"],
        "notes": notes.strip(),
        "annotation_status": "human_reviewed",
    }


def import_gold(annotation_path: Path, candidates_path: Path) -> list[dict[str, Any]]:
    candidates = _load_candidates(candidates_path)
    annotations = _load_annotations(annotation_path)
    result = [_validate_annotation(annotation, candidates) for annotation in annotations]
    ids = [record["case_id"] for record in result]
    if len(set(ids)) != 100:
        raise ValueError("标注 case_id 必须唯一且覆盖 100 条")
    if set(ids) != set(candidates):
        raise ValueError("标注 case_id 没有完整覆盖候选集")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotation", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    records = import_gold(args.annotation, args.candidates)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"Validated {len(records)} human-reviewed refund Intent Gold cases")
    print("speech_act=" + json.dumps(Counter(record["speech_act"] for record in records), ensure_ascii=False))
    print("primary_goal=" + json.dumps(Counter(record["primary_goal"] for record in records), ensure_ascii=False))
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
