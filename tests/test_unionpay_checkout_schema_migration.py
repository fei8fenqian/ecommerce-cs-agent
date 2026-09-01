"""UnionPay U1 migration contract checks; does not connect to PostgreSQL."""

from pathlib import Path

MIGRATION = Path("alembic/versions/c6f4a9e2b817_add_unionpay_checkout_provider.py")


def test_unionpay_migration_is_a_forward_extension_of_checkout_payments() -> None:
    """U1 extends application-owned payment rows without editing old migrations."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "c6f4a9e2b817"' in sql
    assert 'down_revision: Union[str, Sequence[str], None] = "a9e4c7d2f813"' in sql
    assert "ALTER TABLE public.payment_transactions" in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "CREATE TABLE" not in sql


def test_unionpay_provider_and_txn_time_constraints_are_explicit() -> None:
    """The database accepts the two existing providers plus UnionPay and validates txnTime."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "'alipay_sandbox', 'wechat_sandbox', 'unionpay_test'" in sql
    assert "ADD COLUMN provider_txn_time VARCHAR(14)" in sql
    assert "provider_txn_time IS NULL OR provider_txn_time ~ '^[0-9]{14}$'" in sql
    assert "DROP COLUMN provider_txn_time" in sql
    assert "'alipay_sandbox', 'wechat_sandbox'" in sql
