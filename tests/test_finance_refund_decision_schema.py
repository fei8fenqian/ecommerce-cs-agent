"""财务退款决策 migration 的静态结构门禁。"""

from pathlib import Path

MIGRATION = Path("alembic/versions/b4e7c2d9f601_add_finance_refund_decisions.py")


def test_finance_refund_migration_extends_only_checkout_refund_state() -> None:
    """migration 必须保留幂等、财务主体和状态约束。"""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "Revises: f7b2d6e1a904" in sql
    assert "finance_decision_idempotency_key VARCHAR(80) UNIQUE" in sql
    assert "finance_decided_by INTEGER REFERENCES public.users(id)" in sql
    assert "finance_decision_note VARCHAR(500) NOT NULL DEFAULT ''" in sql
    assert "PENDING_FINANCE_APPROVAL" in sql
    assert "REJECTED" in sql
    assert "CREATE INDEX idx_checkout_refunds_finance_queue" in sql
    assert "UPDATE public.orders" not in sql
    assert "FROM public.orders" not in sql
