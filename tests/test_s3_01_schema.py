"""S3-01 schema assertions.

These tests are read-only.  They must run only after an isolated ``_test``
database has been upgraded to the S3-01 head revision.

Before migration, the operator must record the following values and compare
them after migration; the migration must not alter either count:

    SELECT COUNT(*) FROM orders;
    SELECT COUNT(*) FROM orders WHERE customer_user_id IS NULL;
"""

import psycopg
import pytest

from config import settings

EXPECTED_REVISION = "a7d1e8f4c902"
EXPECTED_TABLES = {
    "after_sale_requests",
    "refunds",
    "audit_events",
    "outbox_events",
}


def _dsn() -> str:
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


async def _fetchall(query: str, params: tuple = ()) -> list[tuple]:
    conn = await psycopg.AsyncConnection.connect(_dsn())
    try:
        cursor = await conn.execute(query, params)
        return await cursor.fetchall()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_s3_01_revision_and_tables_exist() -> None:
    revision_rows = await _fetchall("SELECT version_num FROM alembic_version")
    assert revision_rows == [(EXPECTED_REVISION,)]

    rows = await _fetchall(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        ORDER BY table_name
        """,
        (list(EXPECTED_TABLES),),
    )
    assert {row[0] for row in rows} == EXPECTED_TABLES


@pytest.mark.asyncio
async def test_s3_01_columns_and_types() -> None:
    rows = await _fetchall(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        ORDER BY table_name, ordinal_position
        """,
        (list(EXPECTED_TABLES),),
    )
    columns = {(row[0], row[1], row[2], row[3]) for row in rows}

    assert ("after_sale_requests", "payment_amount_cents", "bigint", "NO") in columns
    assert ("after_sale_requests", "refund_amount_cents", "bigint", "NO") in columns
    assert ("after_sale_requests", "currency", "character", "NO") in columns
    assert ("refunds", "amount_cents", "bigint", "NO") in columns
    assert ("refunds", "merchant_refund_request_no", "character varying", "NO") in columns
    assert ("audit_events", "request_hash", "character", "YES") in columns
    assert ("outbox_events", "payload", "jsonb", "NO") in columns
    assert ("outbox_events", "available_at", "timestamp with time zone", "NO") in columns


@pytest.mark.asyncio
async def test_s3_01_constraints_and_foreign_keys() -> None:
    rows = await _fetchall(
        """
        SELECT conname
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
          AND conrelid IN (
              'public.after_sale_requests'::regclass,
              'public.refunds'::regclass,
              'public.audit_events'::regclass,
              'public.outbox_events'::regclass
          )
        """
    )
    constraints = {row[0] for row in rows}
    expected = {
        "after_sale_requests_order_fkey",
        "after_sale_requests_customer_user_fkey",
        "after_sale_requests_reapplication_fkey",
        "after_sale_requests_status_check",
        "after_sale_requests_currency_check",
        "after_sale_requests_full_refund_check",
        "refunds_after_sale_request_fkey",
        "refunds_merchant_refund_request_no_key",
        "refunds_status_check",
        "audit_events_owner_user_fkey",
        "audit_events_actor_user_fkey",
        "outbox_events_refund_fkey",
        "outbox_events_status_check",
        "outbox_events_refund_type_unique",
    }
    assert expected <= constraints


@pytest.mark.asyncio
async def test_s3_01_indexes_cover_idempotency_and_claim_queries() -> None:
    rows = await _fetchall(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = ANY(%s)
        """,
        (list(EXPECTED_TABLES),),
    )
    indexes = {row[0] for row in rows}
    expected = {
        "uq_after_sale_requests_active_order",
        "uq_after_sale_requests_reapplication",
        "idx_after_sale_requests_agent_claim",
        "uq_refunds_external_refund_id",
        "idx_refunds_last_callback",
        "uq_audit_events_command_idempotency",
        "uq_audit_events_callback_dedup",
        "idx_audit_events_created_at",
        "idx_outbox_events_claim",
        "idx_outbox_events_dead_lettered_at",
    }
    assert expected <= indexes


@pytest.mark.asyncio
async def test_s3_01_preserves_legacy_order_shape() -> None:
    rows = await _fetchall(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'orders'
        ORDER BY ordinal_position
        """
    )
    order_columns = {row[0] for row in rows}
    assert {
        "order_id",
        "customer_id",
        "customer_user_id",
        "status",
        "paid_amount",
        "payment_time",
    } <= order_columns
