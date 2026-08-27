"""静态验证客户支持 Case migration 的业务边界。"""

from pathlib import Path  # noqa: I001


MIGRATION = Path("alembic/versions/a9e4c7d2f813_add_customer_support_cases.py")


def test_support_case_migration_creates_only_empty_workflow_tables() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE public.support_cases" in sql
    assert "CREATE TABLE public.support_case_events" in sql
    assert "REFERENCES public.sessions(id)" in sql
    assert "REFERENCES public.users(id)" in sql
    assert "ALTER TABLE public.sales_orders" not in sql
    assert "ALTER TABLE public.orders" not in sql
    assert "UPDATE public." not in sql
    assert "INSERT INTO public." not in sql


def test_support_case_migration_has_recovery_and_audit_guards() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "support_cases_one_open_case_per_session" in sql
    assert "request_stack JSONB NOT NULL" in sql
    assert "verified_facts JSONB NOT NULL" in sql
    assert "pending_command JSONB NOT NULL" in sql
    assert "version INTEGER NOT NULL DEFAULT 1" in sql
    assert "AWAITING_CUSTOMER" in sql
    assert "AWAITING_STAFF" in sql
    assert "CASE_CREATED" in sql
    assert "COMMAND_PROPOSED" in sql
    assert "COMMAND_COMPLETED" in sql
