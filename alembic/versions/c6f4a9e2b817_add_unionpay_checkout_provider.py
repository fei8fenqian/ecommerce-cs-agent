"""add UnionPay checkout provider fields.

Revision ID: c6f4a9e2b817
Revises: a9e4c7d2f813
Create Date: 2026-08-31 15:00:00.000000

This migration only extends the existing application-owned payment transaction
record.  It does not backfill legacy payments or change any payment state.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "c6f4a9e2b817"
down_revision: Union[str, Sequence[str], None] = "a9e4c7d2f813"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow the frozen UnionPay test provider and retain its original txnTime."""
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        DROP CONSTRAINT payment_transactions_provider_check
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        ADD CONSTRAINT payment_transactions_provider_check
            CHECK (provider IN ('alipay_sandbox', 'wechat_sandbox', 'unionpay_test'))
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        ADD COLUMN provider_txn_time VARCHAR(14)
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        ADD CONSTRAINT payment_transactions_provider_txn_time_check
            CHECK (provider_txn_time IS NULL OR provider_txn_time ~ '^[0-9]{14}$')
        """
    )


def downgrade() -> None:
    """Remove only U1 fields; a downgrade is valid only before UnionPay rows exist."""
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        DROP CONSTRAINT payment_transactions_provider_txn_time_check
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        DROP COLUMN provider_txn_time
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        DROP CONSTRAINT payment_transactions_provider_check
        """
    )
    op.execute(
        """
        ALTER TABLE public.payment_transactions
        ADD CONSTRAINT payment_transactions_provider_check
            CHECK (provider IN ('alipay_sandbox', 'wechat_sandbox'))
        """
    )
