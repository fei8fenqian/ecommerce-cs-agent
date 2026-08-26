"""迁移拓扑测试：生产主线与 Harness 实验分支必须明确隔离。"""

from pathlib import Path

VERSIONS = Path("alembic/versions")


def _load_revision(name: str) -> str:
    return (VERSIONS / name).read_text(encoding="utf-8")


def test_harness_revision_is_explicitly_experimental() -> None:
    source = _load_revision("d7a1c4e8b92f_add_v7_harness_persistence.py")

    assert 'branch_labels: Union[str, Sequence[str], None] = "experimental_harness"' in source


def test_production_merge_contains_only_business_heads() -> None:
    source = _load_revision("m8c5d2e9f701_merge_production_heads.py")

    assert '"a6b4c8d2e7f1"' in source
    assert '"b4e7c2d9f601"' in source
    assert '"d7a1c4e8b92f"' not in source
    assert "CREATE TABLE" not in source
    assert "ALTER TABLE" not in source
