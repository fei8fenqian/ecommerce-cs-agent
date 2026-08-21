"""add S3 refund domain tables

Revision ID: a7d1e8f4c902
Revises: 6aa6adbb7084
Create Date: 2026-08-22 00:00:00.000000

This migration only adds the S3 domain tables.  It does not backfill legacy
orders, infer ownership, or change the existing order facts.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

revision: str = "a7d1e8f4c902"
down_revision: Union[str, Sequence[str], None] = "6aa6adbb7084"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the S3 refund domain schema without touching legacy data."""
    op.execute(
        """
        CREATE TABLE public.after_sale_requests (
            id uuid PRIMARY KEY,
            order_id varchar(20) NOT NULL,
            customer_user_id integer NOT NULL,
            assigned_agent_id integer,
            status varchar(40) NOT NULL,
            currency char(3) NOT NULL DEFAULT 'CNY',
            payment_transaction_ref varchar(128) NOT NULL,
            payment_amount_cents bigint NOT NULL,
            refund_amount_cents bigint NOT NULL,
            reason_code varchar(64) NOT NULL,
            customer_note text,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence_round smallint NOT NULL DEFAULT 0,
            evidence_due_at timestamptz,
            qualification_path varchar(16),
            qualification_result varchar(24),
            policy_version varchar(64),
            facts_snapshot jsonb,
            quote_expires_at timestamptz,
            customer_confirmed_at timestamptz,
            reviewed_by_agent_id integer,
            reviewed_at timestamptz,
            finance_decided_by integer,
            finance_decided_at timestamptz,
            decision_reason_code varchar(64),
            reapplication_of_id uuid,
            version bigint NOT NULL DEFAULT 0,
            submitted_at timestamptz NOT NULL,
            claimed_at timestamptz,
            claim_expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            closed_at timestamptz,
            CONSTRAINT after_sale_requests_order_fkey
                FOREIGN KEY (order_id) REFERENCES public.orders(order_id)
                ON DELETE RESTRICT,
            CONSTRAINT after_sale_requests_customer_user_fkey
                FOREIGN KEY (customer_user_id) REFERENCES public.users(id)
                ON DELETE RESTRICT,
            CONSTRAINT after_sale_requests_assigned_agent_fkey
                FOREIGN KEY (assigned_agent_id) REFERENCES public.users(id)
                ON DELETE SET NULL,
            CONSTRAINT after_sale_requests_reviewed_agent_fkey
                FOREIGN KEY (reviewed_by_agent_id) REFERENCES public.users(id)
                ON DELETE RESTRICT,
            CONSTRAINT after_sale_requests_finance_decider_fkey
                FOREIGN KEY (finance_decided_by) REFERENCES public.users(id)
                ON DELETE RESTRICT,
            CONSTRAINT after_sale_requests_reapplication_fkey
                FOREIGN KEY (reapplication_of_id)
                REFERENCES public.after_sale_requests(id)
                ON DELETE RESTRICT,
            CONSTRAINT after_sale_requests_status_check CHECK (
                status IN (
                    'SUBMITTED', 'EVIDENCE_PENDING', 'UNDER_REVIEW',
                    'PENDING_CUSTOMER_CONFIRMATION', 'PENDING_FINANCE_APPROVAL',
                    'REFUND_PROCESSING', 'REFUNDED', 'REJECTED', 'CANCELLED',
                    'EXPIRED', 'REFUND_EXCEPTION'
                )
            ),
            CONSTRAINT after_sale_requests_currency_check
                CHECK (currency = 'CNY'),
            CONSTRAINT after_sale_requests_payment_amount_check
                CHECK (payment_amount_cents >= 0),
            CONSTRAINT after_sale_requests_refund_amount_check
                CHECK (refund_amount_cents >= 0),
            CONSTRAINT after_sale_requests_full_refund_check
                CHECK (refund_amount_cents = payment_amount_cents),
            CONSTRAINT after_sale_requests_evidence_round_check
                CHECK (evidence_round BETWEEN 0 AND 2),
            CONSTRAINT after_sale_requests_qualification_path_check
                CHECK (qualification_path IS NULL OR qualification_path IN ('AUTO', 'FINANCE')),
            CONSTRAINT after_sale_requests_version_check
                CHECK (version >= 0)
        )
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX uq_after_sale_requests_active_order
        ON public.after_sale_requests(order_id)
        WHERE status IN (
            'SUBMITTED', 'EVIDENCE_PENDING', 'UNDER_REVIEW',
            'PENDING_CUSTOMER_CONFIRMATION', 'PENDING_FINANCE_APPROVAL',
            'REFUND_PROCESSING'
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_after_sale_requests_reapplication
        ON public.after_sale_requests(reapplication_of_id)
        WHERE reapplication_of_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_after_sale_requests_customer_created
        ON public.after_sale_requests(customer_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_after_sale_requests_status_created
        ON public.after_sale_requests(status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_after_sale_requests_agent_claim
        ON public.after_sale_requests(assigned_agent_id, status, claim_expires_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_after_sale_requests_payment_ref
        ON public.after_sale_requests(payment_transaction_ref)
        """
    )

    op.execute(
        """
        CREATE TABLE public.refunds (
            id uuid PRIMARY KEY,
            after_sale_request_id uuid NOT NULL UNIQUE,
            payment_transaction_ref varchar(128) NOT NULL,
            merchant_refund_request_no varchar(128) NOT NULL UNIQUE,
            currency char(3) NOT NULL DEFAULT 'CNY',
            amount_cents bigint NOT NULL,
            status varchar(32) NOT NULL,
            version bigint NOT NULL DEFAULT 0,
            external_refund_id varchar(128),
            last_external_event_id varchar(128),
            last_external_status varchar(64),
            failure_reason_code varchar(64),
            retryable boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            processing_at timestamptz,
            succeeded_at timestamptz,
            failed_at timestamptz,
            last_callback_at timestamptz,
            reconciliation_due_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT refunds_after_sale_request_fkey
                FOREIGN KEY (after_sale_request_id)
                REFERENCES public.after_sale_requests(id)
                ON DELETE RESTRICT,
            CONSTRAINT refunds_currency_check CHECK (currency = 'CNY'),
            CONSTRAINT refunds_amount_check CHECK (amount_cents > 0),
            CONSTRAINT refunds_status_check CHECK (
                status IN (
                    'CREATED', 'PROCESSING', 'SUCCEEDED', 'FAILED',
                    'RECONCILIATION_EXCEPTION'
                )
            ),
            CONSTRAINT refunds_version_check CHECK (version >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_refunds_external_refund_id
        ON public.refunds(external_refund_id)
        WHERE external_refund_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_refunds_status_created
        ON public.refunds(status, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_refunds_reconciliation_due
        ON public.refunds(reconciliation_due_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_refunds_last_callback
        ON public.refunds(last_callback_at)
        """
    )

    op.execute(
        """
        CREATE TABLE public.audit_events (
            id uuid PRIMARY KEY,
            resource_type varchar(40) NOT NULL,
            resource_id uuid NOT NULL,
            owner_user_id integer,
            actor_type varchar(24) NOT NULL,
            actor_user_id integer,
            action varchar(64) NOT NULL,
            from_status varchar(40),
            to_status varchar(40),
            expected_version bigint,
            new_version bigint,
            reason_code varchar(64),
            policy_version varchar(64),
            request_id varchar(128),
            trace_id varchar(128),
            span_id varchar(64),
            command_name varchar(128),
            idempotency_key varchar(256),
            request_hash char(64),
            result_resource_type varchar(40),
            result_resource_id uuid,
            result_version bigint,
            external_event_id varchar(128),
            merchant_refund_request_no varchar(128),
            external_refund_id varchar(128),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT audit_events_actor_type_check CHECK (
                actor_type IN (
                    'CUSTOMER', 'AGENT', 'FINANCE', 'OPERATOR', 'ADMIN',
                    'WORKER', 'PAYMENT_GATEWAY', 'SYSTEM'
                )
            ),
            CONSTRAINT audit_events_expected_version_check
                CHECK (expected_version IS NULL OR expected_version >= 0),
            CONSTRAINT audit_events_new_version_check
                CHECK (new_version IS NULL OR new_version >= 0),
            CONSTRAINT audit_events_result_version_check
                CHECK (result_version IS NULL OR result_version >= 0)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE public.audit_events
        ADD CONSTRAINT audit_events_owner_user_fkey
        FOREIGN KEY (owner_user_id) REFERENCES public.users(id)
        ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        ALTER TABLE public.audit_events
        ADD CONSTRAINT audit_events_actor_user_fkey
        FOREIGN KEY (actor_user_id) REFERENCES public.users(id)
        ON DELETE RESTRICT
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_audit_events_command_idempotency
        ON public.audit_events(actor_user_id, command_name, idempotency_key)
        WHERE actor_user_id IS NOT NULL
          AND command_name IS NOT NULL
          AND idempotency_key IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_audit_events_callback_dedup
        ON public.audit_events(
            external_event_id,
            merchant_refund_request_no,
            external_refund_id
        )
        WHERE external_event_id IS NOT NULL
          AND merchant_refund_request_no IS NOT NULL
          AND external_refund_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX idx_audit_events_resource
        ON public.audit_events(resource_type, resource_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_audit_events_owner_created
        ON public.audit_events(owner_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_audit_events_actor_created
        ON public.audit_events(actor_user_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_audit_events_request_id
        ON public.audit_events(request_id)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_audit_events_created_at
        ON public.audit_events(created_at)
        """
    )

    op.execute(
        """
        CREATE TABLE public.outbox_events (
            id uuid PRIMARY KEY,
            refund_id uuid NOT NULL,
            event_type varchar(64) NOT NULL,
            idempotency_key varchar(256) NOT NULL UNIQUE,
            payload jsonb NOT NULL,
            status varchar(20) NOT NULL DEFAULT 'PENDING',
            attempt_count integer NOT NULL DEFAULT 0,
            available_at timestamptz NOT NULL DEFAULT now(),
            locked_at timestamptz,
            locked_by varchar(128),
            last_error_code varchar(64),
            dead_lettered_at timestamptz,
            processed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT outbox_events_refund_fkey
                FOREIGN KEY (refund_id) REFERENCES public.refunds(id)
                ON DELETE RESTRICT,
            CONSTRAINT outbox_events_type_check
                CHECK (event_type = 'REFUND_REQUEST_AUTHORIZED'),
            CONSTRAINT outbox_events_status_check CHECK (
                status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'DEAD')
            ),
            CONSTRAINT outbox_events_attempt_count_check
                CHECK (attempt_count >= 0),
            CONSTRAINT outbox_events_refund_type_unique
                UNIQUE (refund_id, event_type)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX idx_outbox_events_claim
        ON public.outbox_events(status, available_at, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_outbox_events_dead
        ON public.outbox_events(status, updated_at)
        """
    )
    op.execute(
        """
        CREATE INDEX idx_outbox_events_dead_lettered_at
        ON public.outbox_events(dead_lettered_at)
        """
    )


def downgrade() -> None:
    """Drop only the S3 tables; production downgrade is not a rollback plan."""
    op.execute("DROP TABLE IF EXISTS public.outbox_events")
    op.execute("DROP TABLE IF EXISTS public.audit_events")
    op.execute("DROP TABLE IF EXISTS public.refunds")
    op.execute("DROP TABLE IF EXISTS public.after_sale_requests")
