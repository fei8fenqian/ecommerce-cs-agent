"""运行时知识来源白名单。

``data/knowledge`` 中的 Markdown 可以作为编辑中的候选资料存在；只有本模块从
``runtime_manifest.txt`` 读取到的来源才允许进入生产 RAG。
"""

from pathlib import Path

RUNTIME_MANIFEST = "runtime_manifest.txt"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_KNOWLEDGE_DIR = _PROJECT_ROOT / "data" / "knowledge"


def load_runtime_knowledge_sources(knowledge_dir: Path | None = None) -> frozenset[str]:
    """加载并校验运行时 RAG 来源白名单，错误时 fail closed。"""

    directory = knowledge_dir or DEFAULT_KNOWLEDGE_DIR
    manifest = directory / RUNTIME_MANIFEST
    if not manifest.is_file():
        raise RuntimeError(f"缺少运行时知识清单: {manifest}")

    names = [
        line.strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not names:
        raise RuntimeError("运行时知识清单不能为空")
    if len(names) != len(set(names)):
        raise RuntimeError("运行时知识清单不能包含重复文件")

    for name in names:
        candidate = Path(name)
        if candidate.name != name or candidate.suffix != ".md":
            raise RuntimeError(f"运行时知识清单只允许 data/knowledge 根目录的 .md 文件: {name}")
        if not (directory / candidate).is_file():
            raise RuntimeError(f"运行时知识清单引用了不存在的文件: {name}")

    return frozenset(names)


def runtime_markdown_paths(knowledge_dir: Path | None = None) -> list[Path]:
    """返回 manifest 指定的运行时 Markdown 路径，顺序与清单一致。"""

    directory = knowledge_dir or DEFAULT_KNOWLEDGE_DIR
    sources = load_runtime_knowledge_sources(directory)
    names = [
        line.strip()
        for line in (directory / RUNTIME_MANIFEST).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    # ``sources`` 负责校验；这里保留清单顺序，使导入输出稳定且便于审计。
    assert set(names) == sources
    return [directory / name for name in names]
