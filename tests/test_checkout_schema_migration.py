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


def test_component_checkout_migration_only_extends_application_category_constraints() -> None:
    """配件可购买不应借机修改 legacy 订单或支付结构。"""
    sql = Path("alembic/versions/c3d7e1a9f5b2_allow_components_in_checkout.py").read_text(encoding="utf-8")

    assert "cart_items_category_check" in sql
    assert "sales_order_items_category_check" in sql
    assert "'components'" in sql
    assert "ALTER TABLE public.orders" not in sql


def test_cart_consumption_migration_only_adds_application_checkout_snapshots() -> None:
    """付款成功后的购物车消费必须有独立快照，升级时不清空客户购物车。"""
    sql = Path("alembic/versions/f7b2d6e1a904_add_checkout_cart_consumption.py").read_text(encoding="utf-8")

    assert "CREATE TABLE public.checkout_cart_lines" in sql
    assert "consumed_at TIMESTAMPTZ" in sql
    assert "REFERENCES public.sales_orders(id)" in sql
    assert "UPDATE public.cart_items" not in sql
    assert "DELETE FROM public.cart_items" not in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "UPDATE public.orders" not in sql
