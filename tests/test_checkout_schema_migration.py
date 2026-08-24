"""Static contract checks for the isolated checkout migration.

The migration is intentionally not applied by this test.  Applying it requires
an explicit command against a database ending in ``_test``.
"""

from pathlib import Path

MIGRATION = Path("alembic/versions/b9f3c2d6e714_add_checkout_order_and_payment_tables.py")


def test_checkout_migration_isolated_from_legacy_orders() -> None:
    """New payment facts must never reuse or rewrite historical mock orders."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE public.sales_orders" in sql
    assert "CREATE TABLE public.sales_order_items" in sql
    assert "CREATE TABLE public.payment_transactions" in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "UPDATE public.orders" not in sql


def test_checkout_migration_requires_core_transaction_constraints() -> None:
    """Verify CNY integer amounts, ownership, idempotency keys, and callback de-duplication."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "total_amount_cents BIGINT NOT NULL" in sql
    assert "amount_cents BIGINT NOT NULL" in sql
    assert "REFERENCES public.users(id)" in sql
    assert "merchant_payment_no VARCHAR(64) NOT NULL UNIQUE" in sql
    assert "provider_callback_id VARCHAR(128) UNIQUE" in sql
    assert "currency = 'CNY'" in sql
