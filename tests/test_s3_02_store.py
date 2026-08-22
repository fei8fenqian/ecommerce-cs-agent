"""S3-02 Store 单元测试：只使用 fake connection，不连接 PostgreSQL。"""

from datetime import datetime, timezone
from typing import cast
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from service.after_sale_types import (
    AfterSaleCommand,
    AfterSaleStatus,
    Currency,
    DomainErrorCode,
    ExternalEventId,
    ExternalRefundId,
    IdempotencyKey,
    LegacyOrderId,
    MerchantRefundRequestNo,
    NonNegativeInt,
    NormalizedExternalStatus,
    OutboxEventType,
    OutboxStatus,
    PaymentTransactionRef,
    PositiveCents,
    PositiveInt,
    RefundStatus,
    Source,
)
from store.after_sale_store import PsycopgAfterSaleRepository
from store.audit_store import PsycopgAuditRepository
from store.outbox_store import PsycopgOutboxRepository
from store.refund_store import PsycopgRefundRepository
from store.refund_store_types import (
    AuditMetadata,
    EvidenceUpdate,
    IdempotencyRecord,
    NewRefundOutboxEvent,
    OutboxDeliveryUpdate,
    RefundOutboxPayload,
    RefundStatusUpdate,
    SafeText,
    VerifiedCallbackUpdate,
)
from store.refund_unit_of_work import RefundUnitOfWorkFactory


class FakeCursor:
    def __init__(self, row=None):
        self._row = row

    async def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self, *rows):
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.rows = list(rows)
        self.commit_count = 0
        self.rollback_count = 0

    async def execute(self, query: str, params=()):
        self.calls.append((query, tuple(params)))
        return FakeCursor(self.rows.pop(0) if self.rows else None)

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        self.rollback_count += 1


def _after_sale_row():
    now = datetime.now(timezone.utc)
    return (
        uuid4(),
        "ORD-S3-TEST-1",
        101,
        None,
        "SUBMITTED",
        "CNY",
        "pay-ref-1",
        1000,
        1000,
        "WRONG_ITEM",
        "safe note",
        [{"object_ref": "evidence/1", "media_type": "image/png", "size_bytes": 100}],
        0,
        None,
        None,
        None,
        None,
        None,
        0,
        now,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        now,
        now,
        None,
    )


def _refund_row():
    now = datetime.now(timezone.utc)
    return (
        uuid4(),
        uuid4(),
        "pay-ref-1",
        "merchant-refund-1",
        "CNY",
        1000,
        "CREATED",
        0,
        None,
        None,
        None,
        None,
        False,
        now,
        None,
        None,
        None,
        None,
        None,
        now,
    )


def _outbox_row():
    now = datetime.now(timezone.utc)
    outbox_id = uuid4()
    refund_id = uuid4()
    return (
        outbox_id,
        refund_id,
        "REFUND_REQUEST_AUTHORIZED",
        "outbox-key-1",
        "PENDING",
        0,
        {
            "outbox_event_id": str(outbox_id),
            "refund_id": str(refund_id),
            "after_sale_request_id": str(uuid4()),
            "merchant_refund_request_no": "merchant-refund-1",
            "payment_transaction_ref": "pay-ref-1",
            "amount_cents": 1000,
            "currency": "CNY",
            "attempt": 1,
            "created_at": now.isoformat(),
        },
        now,
    )


@pytest.mark.asyncio
async def test_after_sale_get_for_update_uses_lock_and_parses_values():
    connection = FakeConnection(_after_sale_row())
    repository = PsycopgAfterSaleRepository(connection)
    request_id = connection.rows[0][0]

    record = await repository.get_for_update(request_id)

    assert record is not None
    assert record.order_id == LegacyOrderId("ORD-S3-TEST-1")
    assert record.status.value == "SUBMITTED"
    assert len(record.evidence_refs) == 1
    query, params = connection.calls[0]
    assert "FOR UPDATE" in query
    assert params == (request_id,)


@pytest.mark.asyncio
async def test_after_sale_claim_is_atomic_and_has_version_guard():
    connection = FakeConnection()
    repository = PsycopgAfterSaleRepository(connection)
    request_id = uuid4()
    now = datetime.now(timezone.utc)

    assert (
        await repository.claim_if_available(
            request_id,
            202,
            NonNegativeInt(3),
            now,
            now,
        )
        is None
    )

    query, params = connection.calls[0]
    assert "assigned_agent_id IS NULL" in query
    assert "version = %s" in query
    assert request_id in params
    assert 3 in params


@pytest.mark.asyncio
async def test_after_sale_active_order_binds_order_id_as_string():
    connection = FakeConnection()
    repository = PsycopgAfterSaleRepository(connection)

    assert await repository.find_active_by_order(LegacyOrderId("ORD-S3-TEST-2")) is None

    _query, params = connection.calls[0]
    assert params[0] == "ORD-S3-TEST-2"
    assert type(params[0]) is str


@pytest.mark.asyncio
async def test_refund_repository_locks_by_after_sale_and_checks_callback_facts():
    connection = FakeConnection(None, None)
    repository = PsycopgRefundRepository(connection)
    after_sale_id = uuid4()

    assert await repository.get_for_update_by_after_sale(after_sale_id) is None
    query, params = connection.calls[0]
    assert "FOR UPDATE" in query
    assert params == (after_sale_id,)

    await repository.save_verified_callback(
        uuid4(),
        NonNegativeInt(1),
        update=VerifiedCallbackUpdate(
            external_event_id=ExternalEventId("event-1"),
            merchant_refund_request_no=MerchantRefundRequestNo("merchant-refund-1"),
            external_refund_id=ExternalRefundId("external-refund-1"),
            external_status=NormalizedExternalStatus("SUCCESS"),
            amount_cents=PositiveCents(1000),
            currency=Currency.CNY,
            callback_at=datetime.now(timezone.utc),
        ),
    )
    callback_query, callback_params = connection.calls[1]
    assert "amount_cents = %s" in callback_query
    assert "currency = %s" in callback_query
    assert "merchant_refund_request_no = %s" in callback_query
    assert 1000 in callback_params
    assert "merchant-refund-1" in callback_params
    assert "signature" not in callback_query.lower()


@pytest.mark.asyncio
async def test_refund_status_update_persists_state_timestamps():
    connection = FakeConnection(None)
    repository = PsycopgRefundRepository(connection)
    now = datetime.now(timezone.utc)

    await repository.update_status(
        uuid4(),
        NonNegativeInt(2),
        RefundStatusUpdate(
            target_status=RefundStatus.PROCESSING,
            updated_at=now,
            processing_at=now,
            succeeded_at=None,
            failed_at=None,
            reconciliation_due_at=now,
        ),
    )

    query, params = connection.calls[0]
    assert "processing_at = %s" in query
    assert "succeeded_at = %s" in query
    assert "failed_at = %s" in query
    assert "reconciliation_due_at = %s" in query
    assert params.count(now) == 3


@pytest.mark.asyncio
async def test_evidence_update_persists_customer_note():
    connection = FakeConnection(None)
    repository = PsycopgAfterSaleRepository(connection)
    now = datetime.now(timezone.utc)

    await repository.update_evidence(
        uuid4(),
        NonNegativeInt(0),
        EvidenceUpdate(
            evidence_refs=(),
            evidence_round=1,
            evidence_due_at=None,
            customer_note=SafeText("补充说明"),
            target_status=AfterSaleStatus.UNDER_REVIEW,
            updated_at=now,
        ),
    )

    query, params = connection.calls[0]
    assert "customer_note = %s" in query
    assert "补充说明" in params


@pytest.mark.asyncio
async def test_audit_repository_reads_command_idempotency_scope():
    audit_id = uuid4()
    connection = FakeConnection((audit_id, "hash-1", "AFTER_SALE_REQUEST", uuid4(), 2))
    repository = PsycopgAuditRepository(connection)

    record = await repository.find_command_idempotency(
        101,
        AfterSaleCommand.CANCEL,
        IdempotencyKey("idem-1"),
    )

    assert isinstance(record, IdempotencyRecord)
    assert record.request_hash == "hash-1"
    query, params = connection.calls[0]
    assert "actor_user_id = %s" in query
    assert "command_name = %s" in query
    assert "idempotency_key = %s" in query
    assert params == (101, "cancel_after_sale", "idem-1")


@pytest.mark.asyncio
async def test_outbox_repository_uses_whitelist_payload():
    connection = FakeConnection(_outbox_row())
    repository = PsycopgOutboxRepository(connection)
    payload = RefundOutboxPayload(
        outbox_event_id=uuid4(),
        refund_id=uuid4(),
        after_sale_request_id=uuid4(),
        merchant_refund_request_no=MerchantRefundRequestNo("merchant-refund-1"),
        payment_transaction_ref=PaymentTransactionRef("pay-ref-1"),
        amount_cents=PositiveCents(1000),
        currency=Currency.CNY,
        attempt=PositiveInt(1),
        created_at=datetime.now(timezone.utc),
    )
    event = NewRefundOutboxEvent(
        id=payload.outbox_event_id,
        refund_id=payload.refund_id,
        event_type=OutboxEventType.REFUND_REQUEST_AUTHORIZED,
        idempotency_key=IdempotencyKey("outbox-key-1"),
        payload=payload,
    )

    record = await repository.insert_refund_authorized(event)

    assert record.status is OutboxStatus.PENDING
    query, params = connection.calls[0]
    assert "INSERT INTO public.outbox_events" in query
    json_payload = cast(Jsonb, params[-1]).obj
    assert set(json_payload) == {
        "outbox_event_id",
        "refund_id",
        "after_sale_request_id",
        "merchant_refund_request_no",
        "payment_transaction_ref",
        "amount_cents",
        "currency",
        "attempt",
        "created_at",
    }
    assert "signature" not in json_payload


@pytest.mark.asyncio
async def test_outbox_delivery_update_returns_created_at():
    row = _outbox_row()
    connection = FakeConnection(row)
    repository = PsycopgOutboxRepository(connection)
    event_id = row[0]
    created_at = row[7]

    record = await repository.update_delivery_state(
        event_id,
        OutboxStatus.PENDING,
        update=OutboxDeliveryUpdate(
            status=OutboxStatus.PROCESSING,
            attempt_count=NonNegativeInt(1),
            updated_at=datetime.now(timezone.utc),
            last_error_code=DomainErrorCode.DEPENDENCY_UNAVAILABLE,
        ),
    )

    assert record is not None
    assert record.created_at == created_at
    query, params = connection.calls[0]
    assert "RETURNING" in query
    assert "payload, created_at" in query
    assert "DEPENDENCY_UNAVAILABLE" in params


def test_audit_metadata_is_whitelisted():
    metadata = AuditMetadata(
        source=Source.API,
        conflict_version=NonNegativeInt(2),
        first_audit_event_id=uuid4(),
    ).as_json()

    assert set(metadata) == {"source", "conflict_version", "first_audit_event_id"}
    assert "raw_payload" not in metadata
    assert "token" not in metadata


@pytest.mark.asyncio
async def test_unit_of_work_binds_one_connection_and_commits():
    connection = FakeConnection()
    released = []

    async def connection_factory():
        return connection

    async def connection_releaser(value):
        released.append(value)

    factory = RefundUnitOfWorkFactory(connection_factory, connection_releaser)
    async with factory.begin() as unit_of_work:
        assert unit_of_work.after_sales._connection is connection
        assert unit_of_work.refunds._connection is connection
        assert unit_of_work.audits._connection is connection
        assert unit_of_work.outbox._connection is connection

    assert connection.calls[0][0] == "BEGIN"
    assert connection.commit_count == 1
    assert connection.rollback_count == 0
    assert released == [connection]


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_on_unexpected_exception():
    connection = FakeConnection()

    async def connection_factory():
        return connection

    async def connection_releaser(value):
        return None

    factory = RefundUnitOfWorkFactory(connection_factory, connection_releaser)
    with pytest.raises(RuntimeError, match="boom"):
        async with factory.begin():
            raise RuntimeError("boom")

    assert connection.commit_count == 0
    assert connection.rollback_count == 1
