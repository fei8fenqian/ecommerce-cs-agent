"""知识检索结果的受控上下文格式。"""

from typing import Any


def format_knowledge_context(docs: list[dict[str, Any]], *, max_docs: int = 5) -> str:
    """只把已检索到的知识内容标记为一般知识，不提升为当前业务事实。"""

    if not docs:
        return ""

    lines: list[str] = []
    for doc in docs[:max_docs]:
        title = str(doc.get("title") or "知识库").strip()
        source = str(doc.get("source") or "runtime_knowledge").strip()
        content = str(doc.get("content") or "").strip()[:600]
        if content:
            lines.append(f"[知识来源: {source} / {title}]\n{content}")
    return "\n\n---\n\n".join(lines)
