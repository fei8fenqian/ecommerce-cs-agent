"""V7-01A Harness schema assertions.

These tests are read-only, but they require an isolated database that has
already been upgraded to revision ``d7a1c4e8b92f``.  They must not run against
the legacy business database.
"""

import psycopg
import pytest

from config import settings

EXPECTED_REVISION = "d7a1c4e8b92f"
TARGET_DATABASE = "ecommerce_agent_v7_test"
pytestmark = pytest.mark.skipif(
    settings.pg_dbname != TARGET_DATABASE,
    reason=f"V7-01A schema tests require {TARGET_DATABASE}",
)
EXPECTED_TABLES = {
    "agent_tasks",
    "agent_runs",
    "run_steps",
    "run_approval_requests",
}


def _dsn() -> str:
    """Build the PostgreSQL DSN for the explicitly selected test database.

    Returns:
        DSN assembled from Settings.  The caller must set PG_DBNAME to an
        isolated test database before running these assertions.
    """
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


async def _fetchall(query: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    """Execute one read-only schema query and close its connection.

    Args:
        query: Parameterized PostgreSQL catalog query.
        params: Bound query values.

    Returns:
        Rows returned by PostgreSQL.
    """
    connection = await psycopg.AsyncConnection.connect(_dsn())
    try:
        cursor = await connection.execute(query, params)
        return await cursor.fetchall()
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_v7_01a_revision_and_tables_exist() -> None:
    """Verify that only the expected Harness schema was created."""
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
async def test_v7_01a_columns_cover_organization_profile_lease_and_retention() -> None:
    """Verify required organization, Profile, lease, and retention columns."""
    rows = await _fetchall(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        """,
        (list(EXPECTED_TABLES),),
    )
    columns = {(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows}

    for table_name in EXPECTED_TABLES:
        assert (table_name, "organization_id", "uuid", "NO") in columns
        assert (table_name, "retention_expires_at", "timestamp with time zone", "NO") in columns
        assert (table_name, "retention_hold", "boolean", "NO") in columns

    assert ("agent_tasks", "profile_hash", "character", "NO") in columns
    assert ("agent_runs", "run_no", "integer", "NO") in columns
    assert ("agent_runs", "lease_expires_at", "timestamp with time zone", "YES") in columns
    assert ("agent_runs", "fencing_token", "bigint", "NO") in columns
    assert ("run_steps", "step_idempotency_key", "character varying", "YES") in columns


@pytest.mark.asyncio
async def test_v7_01a_constraints_include_composite_boundaries_and_checks() -> None:
    """Verify composite foreign keys, uniqueness, and state safety checks."""
    rows = await _fetchall(
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE connamespace = 'public'::regnamespace
          AND conrelid = ANY(%s::regclass[])
        """,
        (list(f"public.{table_name}" for table_name in EXPECTED_TABLES),),
    )
    constraints = {str(name): str(definition) for name, definition in rows}

    expected_names = {
        "agent_tasks_org_task_key",
        "agent_tasks_idempotency_key",
        "agent_tasks_task_kind_check",
        "agent_tasks_profile_hash_check",
        "agent_runs_task_fkey",
        "agent_runs_org_run_key",
        "agent_runs_task_run_no_key",
        "agent_runs_run_no_check",
        "agent_runs_state_check",
        "agent_runs_lease_pair_check",
        "run_steps_run_fkey",
        "run_steps_org_run_step_no_key",
        "run_steps_org_run_step_key",
        "run_steps_state_check",
        "run_approval_requests_step_fkey",
        "run_approval_requests_binding_key",
        "run_approval_requests_decision_check",
        "run_approval_requests_decision_fields_check",
    }
    assert expected_names <= constraints.keys()
    assert "FOREIGN KEY (organization_id, task_id)" in constraints["agent_runs_task_fkey"]
    assert "FOREIGN KEY (organization_id, run_id)" in constraints["run_steps_run_fkey"]
    assert "FOREIGN KEY (organization_id, run_id, step_id)" in constraints["run_approval_requests_step_fkey"]
    assert "[0-9a-f]{64}" in constraints["agent_tasks_profile_hash_check"]
    assert "run_no >= 1" in constraints["agent_runs_run_no_check"]
    assert "PENDING" in constraints["run_approval_requests_decision_fields_check"]
    assert "decided_by_subject_id IS NULL" in constraints["run_approval_requests_decision_fields_check"]


@pytest.mark.asyncio
async def test_v7_01a_indexes_cover_lease_idempotency_and_retention() -> None:
    """Verify indexes needed by lease, recovery, and retention operations."""
    rows = await _fetchall(
        """
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
          AND tablename = ANY(%s)
        """,
        (list(EXPECTED_TABLES),),
    )
    indexes = {str(row[0]) for row in rows}
    expected = {
        "idx_agent_tasks_requester_created",
        "idx_agent_tasks_retention",
        "idx_agent_runs_lease_candidates",
        "idx_agent_runs_deadline",
        "idx_agent_runs_retention",
        "uq_run_steps_step_idempotency",
        "idx_run_steps_state_updated",
        "idx_run_steps_retention",
        "idx_run_approval_requests_pending_expiry",
        "idx_run_approval_requests_retention",
    }
    assert expected <= indexes


@pytest.mark.asyncio
async def test_v7_01a_does_not_change_legacy_order_shape() -> None:
    """Verify legacy orders retain their existing columns after the upgrade."""
    rows = await _fetchall(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'orders'
        """
    )
    order_columns = {str(row[0]) for row in rows}
    assert {
        "order_id",
        "customer_id",
        "customer_user_id",
        "status",
        "paid_amount",
        "payment_time",
    } <= order_columns
