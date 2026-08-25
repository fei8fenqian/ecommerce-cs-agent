"""Static checks for the autonomous human-escalation migration.

The migration is intentionally not applied by this test.  Applying it requires
explicit approval and an isolated database ending in ``_test``.
"""

from pathlib import Path

MIGRATION = Path("alembic/versions/a6b4c8d2e7f1_add_ticket_human_escalations.py")


def test_escalation_migration_isolated_from_business_facts() -> None:
    """The queue must reference tickets without changing orders or payments."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE public.ticket_human_escalations" in sql
    assert "REFERENCES public.tickets(ticket_id)" in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "UPDATE public.orders" not in sql
    assert "CREATE TABLE public.agent_" not in sql


def test_escalation_migration_has_dedup_delivery_and_reason_guards() -> None:
    """Generation, status, retry, and reason constraints are database-backed."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "UNIQUE (ticket_id, escalation_generation)" in sql
    assert "ticket_human_escalations_delivery_queue" in sql
    assert "ticket_human_escalations_reason_check" in sql
    assert "EXPLICIT_HUMAN_REQUEST" in sql
    assert "RETRY_WAIT" in sql
    assert "DLQ" in sql
    assert "last_error_code VARCHAR(64)" in sql
    assert "dead_lettered_at TIMESTAMPTZ" in sql
    assert "updated_at TIMESTAMPTZ" in sql
