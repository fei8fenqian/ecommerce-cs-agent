"""静态验证 checkout 退款迁移，不执行 Alembic 或访问数据库。"""

from pathlib import Path

MIGRATION = Path("alembic/versions/e9c4b7d2a618_add_checkout_refunds.py")


def test_checkout_refund_migration_uses_new_checkout_facts_only() -> None:
    """退款必须关联新交易，绝不能复用或改写 legacy orders。"""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE public.checkout_refunds" in sql
    assert "REFERENCES public.sales_orders(id)" in sql
    assert "REFERENCES public.payment_transactions(id)" in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "UPDATE public.orders" not in sql


def test_checkout_refund_migration_has_full_refund_and_replay_guards() -> None:
    """退款必须为 CNY 整数分，并在订单和客户端重放两层去重。"""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "sales_order_id UUID NOT NULL UNIQUE" in sql
    assert "payment_transaction_id UUID NOT NULL UNIQUE" in sql
    assert "merchant_refund_no VARCHAR(64) NOT NULL UNIQUE" in sql
    assert "UNIQUE (customer_user_id, request_idempotency_key)" in sql
    assert "amount_cents BIGINT NOT NULL" in sql
    assert "currency = 'CNY'" in sql
    assert "'REFUND_PROCESSING', 'REFUNDED'" in sql
