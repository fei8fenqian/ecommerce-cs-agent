"""add V7 harness persistence tables.

Revision ID: d7a1c4e8b92f
Revises: a7d1e8f4c902
Create Date: 2026-08-23 00:00:00.000000

This migration only creates the V7 Harness persistence schema.  It does not
modify legacy chat, order, S3 refund, payment, or other existing tables, and
it never inserts default tasks, runs, profiles, or organization data.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "d7a1c4e8b92f"
down_revision: Union[str, Sequence[str], None] = "a7d1e8f4c902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the isolated V7 Harness persistence schema.

    The four tables are intentionally separate from legacy chat and all
    e-commerce domain tables.  The application derives organization_id from
    trusted server configuration; this migration does not seed one.
    """
    op.execute(
        """
        CREATE TABLE public.agent_tasks (
            task_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            requester_subject_id integer NOT NULL,
            requester_authz_version varchar(128) NOT NULL,
            profile_id varchar(128) NOT NULL,
            profile_version integer NOT NULL,
            profile_hash char(64) NOT NULL,
            task_kind varchar(64) NOT NULL,
            input_schema_version varchar(32) NOT NULL,
            input_payload jsonb NOT NULL,
            scope_descriptor jsonb NOT NULL,
            scope_hash char(64) NOT NULL,
            task_constraints jsonb NOT NULL,
            idempotency_key varchar(256) NOT NULL,
            input_hash char(64) NOT NULL,
            created_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            retention_expires_at timestamptz NOT NULL,
            retention_hold boolean NOT NULL DEFAULT false,
            CONSTRAINT agent_tasks_pkey PRIMARY KEY (task_id),
            CONSTRAINT agent_tasks_requester_subject_fkey
                FOREIGN KEY (requester_subject_id) REFERENCES public.users(id)
                ON DELETE RESTRICT,
            CONSTRAINT agent_tasks_org_task_key
                UNIQUE (organization_id, task_id),
            CONSTRAINT agent_tasks_idempotency_key
                UNIQUE (organization_id, requester_subject_id, idempotency_key),
            CONSTRAINT agent_tasks_task_kind_check
                CHECK (task_kind = 'SUPPORT_KNOWLEDGE_ASSIST'),
            CONSTRAINT agent_tasks_profile_version_check
                CHECK (profile_version > 0),
            CONSTRAINT agent_tasks_profile_hash_check
                CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT agent_tasks_expiry_check
                CHECK (expires_at > created_at),
            CONSTRAINT agent_tasks_retention_check
                CHECK (retention_expires_at >= created_at),
            CONSTRAINT agent_tasks_input_payload_check
                CHECK (jsonb_typeof(input_payload) = 'object'),
            CONSTRAINT agent_tasks_scope_descriptor_check
                CHECK (jsonb_typeof(scope_descriptor) = 'object'),
            CONSTRAINT agent_tasks_constraints_check
                CHECK (jsonb_typeof(task_constraints) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_tasks_requester_created
        ON public.agent_tasks(organization_id, requester_subject_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_tasks_retention
        ON public.agent_tasks(organization_id, retention_expires_at)
        """
    )

    op.execute(
        """
        CREATE TABLE public.agent_runs (
            run_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            task_id uuid NOT NULL,
            run_no integer NOT NULL,
            state varchar(32) NOT NULL,
            version bigint NOT NULL DEFAULT 0,
            current_step_no integer NOT NULL DEFAULT 0,
            checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb,
            lease_owner varchar(128),
            lease_expires_at timestamptz,
            fencing_token bigint NOT NULL DEFAULT 0,
            authz_checked_at timestamptz,
            deadline_at timestamptz NOT NULL,
            max_model_calls integer NOT NULL,
            model_calls_used integer NOT NULL DEFAULT 0,
            max_tool_calls integer NOT NULL,
            tool_calls_used integer NOT NULL DEFAULT 0,
            execution_versions jsonb NOT NULL,
            terminal_reason varchar(64),
            result_ref varchar(256),
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            finished_at timestamptz,
            retention_expires_at timestamptz NOT NULL,
            retention_hold boolean NOT NULL DEFAULT false,
            CONSTRAINT agent_runs_pkey PRIMARY KEY (run_id),
            CONSTRAINT agent_runs_task_fkey
                FOREIGN KEY (organization_id, task_id)
                REFERENCES public.agent_tasks(organization_id, task_id)
                ON DELETE RESTRICT,
            CONSTRAINT agent_runs_org_run_key
                UNIQUE (organization_id, run_id),
            CONSTRAINT agent_runs_task_run_no_key
                UNIQUE (organization_id, task_id, run_no),
            CONSTRAINT agent_runs_state_check
                CHECK (state IN (
                    'RECEIVED', 'PLANNING', 'RUNNING', 'WAITING_APPROVAL',
                    'WAITING_DEPENDENCY', 'FAILED_RETRYABLE', 'SUCCEEDED',
                    'FAILED_FINAL', 'CANCELLED', 'EXPIRED'
                )),
            CONSTRAINT agent_runs_run_no_check CHECK (run_no >= 1),
            CONSTRAINT agent_runs_version_check CHECK (version >= 0),
            CONSTRAINT agent_runs_current_step_no_check CHECK (current_step_no >= 0),
            CONSTRAINT agent_runs_fencing_token_check CHECK (fencing_token >= 0),
            CONSTRAINT agent_runs_model_budget_check
                CHECK (max_model_calls > 0 AND model_calls_used BETWEEN 0 AND max_model_calls),
            CONSTRAINT agent_runs_tool_budget_check
                CHECK (max_tool_calls >= 0 AND tool_calls_used BETWEEN 0 AND max_tool_calls),
            CONSTRAINT agent_runs_deadline_check CHECK (deadline_at > created_at),
            CONSTRAINT agent_runs_lease_pair_check
                CHECK (
                    (lease_owner IS NULL AND lease_expires_at IS NULL)
                    OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                ),
            CONSTRAINT agent_runs_checkpoint_check
                CHECK (jsonb_typeof(checkpoint) = 'object'),
            CONSTRAINT agent_runs_execution_versions_check
                CHECK (jsonb_typeof(execution_versions) = 'object'),
            CONSTRAINT agent_runs_retention_check
                CHECK (retention_expires_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_runs_lease_candidates
        ON public.agent_runs(organization_id, state, lease_expires_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_runs_deadline
        ON public.agent_runs(organization_id, deadline_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_agent_runs_retention
        ON public.agent_runs(organization_id, retention_expires_at)
        """
    )

    op.execute(
        """
        CREATE TABLE public.run_steps (
            step_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            run_id uuid NOT NULL,
            step_no integer NOT NULL,
            step_version bigint NOT NULL DEFAULT 0,
            step_kind varchar(32) NOT NULL,
            state varchar(32) NOT NULL,
            step_idempotency_key varchar(256),
            capability_id varchar(128),
            capability_version varchar(64),
            input_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            result_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            result_ref varchar(256),
            error_class varchar(64),
            attempt_no integer NOT NULL DEFAULT 0,
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            retention_expires_at timestamptz NOT NULL,
            retention_hold boolean NOT NULL DEFAULT false,
            CONSTRAINT run_steps_pkey PRIMARY KEY (step_id),
            CONSTRAINT run_steps_run_fkey
                FOREIGN KEY (organization_id, run_id)
                REFERENCES public.agent_runs(organization_id, run_id)
                ON DELETE RESTRICT,
            CONSTRAINT run_steps_org_run_step_no_key
                UNIQUE (organization_id, run_id, step_no),
            CONSTRAINT run_steps_org_run_step_key
                UNIQUE (organization_id, run_id, step_id),
            CONSTRAINT run_steps_kind_check
                CHECK (step_kind IN (
                    'PLAN', 'TOOL_CALL', 'WORKFLOW_CALL', 'RUN_APPROVAL',
                    'ARTIFACT', 'FINALIZE'
                )),
            CONSTRAINT run_steps_state_check
                CHECK (state IN (
                    'PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED_RETRYABLE',
                    'FAILED_FINAL', 'WAITING_APPROVAL', 'CANCELLED'
                )),
            CONSTRAINT run_steps_step_no_check CHECK (step_no >= 0),
            CONSTRAINT run_steps_version_check CHECK (step_version >= 0),
            CONSTRAINT run_steps_attempt_no_check CHECK (attempt_no >= 0),
            CONSTRAINT run_steps_input_summary_check
                CHECK (jsonb_typeof(input_summary) = 'object'),
            CONSTRAINT run_steps_result_summary_check
                CHECK (jsonb_typeof(result_summary) = 'object'),
            CONSTRAINT run_steps_retention_check
                CHECK (retention_expires_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_run_steps_step_idempotency
        ON public.run_steps(organization_id, step_idempotency_key)
        WHERE step_idempotency_key IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_run_steps_state_updated
        ON public.run_steps(organization_id, run_id, state, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_run_steps_retention
        ON public.run_steps(organization_id, retention_expires_at)
        """
    )

    op.execute(
        """
        CREATE TABLE public.run_approval_requests (
            approval_id uuid NOT NULL,
            organization_id uuid NOT NULL,
            run_id uuid NOT NULL,
            step_id uuid NOT NULL,
            step_version bigint NOT NULL,
            decision varchar(16) NOT NULL,
            decision_version bigint NOT NULL DEFAULT 0,
            requested_scope jsonb NOT NULL,
            approver_policy jsonb NOT NULL,
            requested_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            decided_at timestamptz,
            decided_by_subject_id integer,
            decision_reason_code varchar(64),
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            retention_expires_at timestamptz NOT NULL,
            retention_hold boolean NOT NULL DEFAULT false,
            CONSTRAINT run_approval_requests_pkey PRIMARY KEY (approval_id),
            CONSTRAINT run_approval_requests_step_fkey
                FOREIGN KEY (organization_id, run_id, step_id)
                REFERENCES public.run_steps(organization_id, run_id, step_id)
                ON DELETE RESTRICT,
            CONSTRAINT run_approval_requests_decider_fkey
                FOREIGN KEY (decided_by_subject_id) REFERENCES public.users(id)
                ON DELETE RESTRICT,
            CONSTRAINT run_approval_requests_binding_key
                UNIQUE (organization_id, run_id, step_id, step_version),
            CONSTRAINT run_approval_requests_decision_check
                CHECK (decision IN ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')),
            CONSTRAINT run_approval_requests_version_check CHECK (decision_version >= 0),
            CONSTRAINT run_approval_requests_expiry_check CHECK (expires_at > requested_at),
            CONSTRAINT run_approval_requests_step_version_check CHECK (step_version >= 0),
            CONSTRAINT run_approval_requests_scope_check
                CHECK (jsonb_typeof(requested_scope) = 'object'),
            CONSTRAINT run_approval_requests_policy_check
                CHECK (jsonb_typeof(approver_policy) = 'object'),
            CONSTRAINT run_approval_requests_decision_fields_check
                CHECK (
                    (decision = 'PENDING'
                        AND decided_at IS NULL
                        AND decided_by_subject_id IS NULL)
                    OR (decision IN ('APPROVED', 'REJECTED')
                        AND decided_at IS NOT NULL
                        AND decided_by_subject_id IS NOT NULL)
                    OR (decision = 'REVOKED' AND decided_at IS NOT NULL)
                    OR (decision = 'EXPIRED'
                        AND decided_at IS NOT NULL
                        AND decided_by_subject_id IS NULL)
                ),
            CONSTRAINT run_approval_requests_retention_check
                CHECK (retention_expires_at >= created_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_run_approval_requests_pending_expiry
        ON public.run_approval_requests(organization_id, expires_at)
        WHERE decision = 'PENDING'
        """
    )
    op.execute(
        """
        CREATE INDEX idx_run_approval_requests_retention
        ON public.run_approval_requests(organization_id, retention_expires_at)
        """
    )


def downgrade() -> None:
    """Drop the V7 Harness schema in reverse dependency order.

    This is only suitable for an empty local or disposable test database.
    Production recovery must use a forward migration because dropping these
    tables destroys task, run, step, and approval history.
    """
    op.execute("DROP TABLE IF EXISTS public.run_approval_requests")
    op.execute("DROP TABLE IF EXISTS public.run_steps")
    op.execute("DROP TABLE IF EXISTS public.agent_runs")
    op.execute("DROP TABLE IF EXISTS public.agent_tasks")
