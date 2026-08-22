"""S3-02 Store 的真实 PostgreSQL 集成测试。

运行前必须把 ``PG_DBNAME`` 指向 ``ecommerce_agent_s3_test``，并确认数据库已经
处于 ``a7d1e8f4c902``。本文件不使用项目 conftest，也不连接业务库。
所有测试数据使用唯一值，session fixture 结束时删除；测试过程中发生异常时，
未提交事务由连接回滚。
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio

from config import settings
from service.after_sale_types import (
    ActorType,
    AfterSaleCommand,
    AfterSaleStatus,
    AuditAction,
    Currency,
    DecisionReasonCode,
    DomainErrorCode,
    ExternalEventId,
    ExternalRefundId,
    IdempotencyKey,
    LegacyOrderId,
    MerchantRefundRequestNo,
    NonNegativeCents,
    NonNegativeInt,
    NormalizedExternalStatus,
    OutboxEventType,
    OutboxStatus,
    PaymentTransactionRef,
    PositiveCents,
    PositiveInt,
    ReasonCode,
    RefundStatus,
    ResourceType,
    SafeText,
    Source,
)
from store.after_sale_store import PsycopgAfterSaleRepository
from store.audit_store import PsycopgAuditRepository
from store.outbox_store import PsycopgOutboxRepository
from store.refund_store import PsycopgRefundRepository
from store.refund_store_types import (
    AfterSaleTransitionUpdate,
    AuditMetadata,
    NewAfterSaleRecord,
    NewAuditEvent,
    NewRefundOutboxEvent,
    NewRefundRecord,
    OutboxDeliveryUpdate,
    RefundOutboxPayload,
    RefundStatusUpdate,
    VerifiedCallbackUpdate,
)
from store.refund_unit_of_work import RefundUnitOfWork

EXPECTED_REVISION = "a7d1e8f4c902"
TARGET_DATABASE = "ecommerce_agent_s3_test"


def _dsn() -> str:
    return (
        f"host={settings.pg_host} "
        f"port={settings.pg_port} "
        f"dbname={settings.pg_dbname} "
        f"user={settings.pg_user} "
        f"password={settings.pg_password.get_secret_value()}"
    )


@dataclass
class IntegrationState:
    dsn: str
    customer_user_id: int
    agent_a_id: int
    agent_b_id: int
    revision: str
    before_order_count: int
    before_unmatched_count: int
    before_order_snapshot: tuple[tuple[str, int | None], ...]
    created_order_ids: set[str] = field(default_factory=set)
    created_after_sale_ids: set[UUID] = field(default_factory=set)


async def _fetchone(
    conn: psycopg.AsyncConnection,
    query: str,
    params: tuple[object, ...] = (),
) -> Sequence[object] | None:
    cursor = await conn.execute(query, params)
    return await cursor.fetchone()


async def _insert_user(conn: psycopg.AsyncConnection, username: str, role: str) -> int:
    row = await _fetchone(
        conn,
        """
        INSERT INTO public.users (username, password_hash, role)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (username, "integration-placeholder", role),
    )
    assert row is not None
    return cast(int, row[0])


async def _insert_order(conn: psycopg.AsyncConnection, state: IntegrationState) -> str:
    order_id = "S3I" + uuid4().hex[:17]
    await conn.execute(
        """
        INSERT INTO public.orders (order_id, customer_id, customer_user_id)
        VALUES (%s, %s, %s)
        """,
        (order_id, "S3C" + uuid4().hex[:7], state.customer_user_id),
    )
    state.created_order_ids.add(order_id)
    return order_id


def _new_after_sale(
    state: IntegrationState,
    order_id: str,
    request_id: UUID | None = None,
) -> NewAfterSaleRecord:
    return NewAfterSaleRecord(
        id=request_id or uuid4(),
        order_id=LegacyOrderId(order_id),
        customer_user_id=state.customer_user_id,
        status=AfterSaleStatus.SUBMITTED,
        currency=Currency.CNY,
        payment_transaction_ref=PaymentTransactionRef("pay-" + uuid4().hex),
        payment_amount_cents=NonNegativeCents(1000),
        refund_amount_cents=NonNegativeCents(1000),
        reason_code=ReasonCode("wrong_item"),
        customer_note=SafeText("synthetic integration note"),
        evidence_refs=(),
        qualification_path=None,
        qualification_result=None,
        policy_version=None,
        facts_snapshot=None,
        submitted_at=datetime.now(timezone.utc),
    )


async def _create_committed_after_sale(state: IntegrationState) -> UUID:
    conn = await psycopg.AsyncConnection.connect(state.dsn)
    try:
        order_id = await _insert_order(conn, state)
        record = _new_after_sale(state, order_id)
        repository = PsycopgAfterSaleRepository(conn)
        await repository.insert_submitted(record)
        state.created_after_sale_ids.add(record.id)
        await conn.commit()
        return record.id
    except BaseException:
        await conn.rollback()
        raise
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def integration_state() -> AsyncIterator[IntegrationState]:
    if settings.pg_dbname != TARGET_DATABASE:
        pytest.fail(f"集成测试只允许连接 {TARGET_DATABASE!r}，当前数据库为 {settings.pg_dbname!r}。")

    conn = await psycopg.AsyncConnection.connect(_dsn())
    state: IntegrationState | None = None
    user_ids: list[int] = []
    try:
        revision_row = await _fetchone(conn, "SELECT version_num FROM public.alembic_version")
        if revision_row is None or revision_row[0] != EXPECTED_REVISION:
            actual = revision_row[0] if revision_row else "<empty>"
            pytest.fail(f"测试库 revision 为 {actual!r}，预期为 {EXPECTED_REVISION!r}。")

        order_count_row = await _fetchone(conn, "SELECT COUNT(*) FROM public.orders")
        unmatched_count_row = await _fetchone(
            conn,
            "SELECT COUNT(*) FROM public.orders WHERE customer_user_id IS NULL",
        )
        order_rows_cursor = await conn.execute("SELECT order_id, customer_user_id FROM public.orders ORDER BY order_id")
        order_rows = await order_rows_cursor.fetchall()
        assert order_count_row is not None
        assert unmatched_count_row is not None

        customer_user_id = await _insert_user(conn, "s3i-customer-" + uuid4().hex, "customer")
        agent_a_id = await _insert_user(conn, "s3i-agent-a-" + uuid4().hex, "agent")
        agent_b_id = await _insert_user(conn, "s3i-agent-b-" + uuid4().hex, "agent")
        user_ids.extend((customer_user_id, agent_a_id, agent_b_id))
        await conn.commit()

        state = IntegrationState(
            dsn=_dsn(),
            customer_user_id=customer_user_id,
            agent_a_id=agent_a_id,
            agent_b_id=agent_b_id,
            revision=EXPECTED_REVISION,
            before_order_count=cast(int, order_count_row[0]),
            before_unmatched_count=cast(int, unmatched_count_row[0]),
            before_order_snapshot=tuple((str(row[0]), row[1]) for row in order_rows),
        )
        yield state
    finally:
        if state is not None:
            await conn.rollback()
            if state.created_after_sale_ids:
                after_sale_ids = list(state.created_after_sale_ids)
                refund_cursor = await conn.execute(
                    """
                    SELECT id FROM public.refunds
                    WHERE after_sale_request_id = ANY(%s)
                    """,
                    (after_sale_ids,),
                )
                refund_ids = [row[0] for row in await refund_cursor.fetchall()]
                if refund_ids:
                    await conn.execute(
                        "DELETE FROM public.outbox_events WHERE refund_id = ANY(%s)",
                        (refund_ids,),
                    )
                    await conn.execute(
                        """
                        DELETE FROM public.audit_events
                        WHERE resource_id = ANY(%s) OR result_resource_id = ANY(%s)
                        """,
                        (refund_ids + after_sale_ids, refund_ids + after_sale_ids),
                    )
                    await conn.execute(
                        "DELETE FROM public.refunds WHERE id = ANY(%s)",
                        (refund_ids,),
                    )
                await conn.execute(
                    "DELETE FROM public.audit_events WHERE resource_id = ANY(%s)",
                    (after_sale_ids,),
                )
                await conn.execute(
                    "DELETE FROM public.after_sale_requests WHERE id = ANY(%s)",
                    (after_sale_ids,),
                )
            if state.created_order_ids:
                await conn.execute(
                    "DELETE FROM public.orders WHERE order_id = ANY(%s)",
                    (list(state.created_order_ids),),
                )
            if user_ids:
                await conn.execute(
                    "DELETE FROM public.users WHERE id = ANY(%s)",
                    (user_ids,),
                )
            await conn.commit()

            final_count_row = await _fetchone(conn, "SELECT COUNT(*) FROM public.orders")
            final_unmatched_row = await _fetchone(
                conn,
                "SELECT COUNT(*) FROM public.orders WHERE customer_user_id IS NULL",
            )
            final_rows_cursor = await conn.execute(
                "SELECT order_id, customer_user_id FROM public.orders ORDER BY order_id"
            )
            final_rows = await final_rows_cursor.fetchall()
            assert final_count_row is not None
            assert final_unmatched_row is not None
            assert cast(int, final_count_row[0]) == state.before_order_count
            assert cast(int, final_unmatched_row[0]) == state.before_unmatched_count
            assert tuple((str(row[0]), row[1]) for row in final_rows) == state.before_order_snapshot
        await conn.close()


@pytest.mark.asyncio
async def test_s3_02_integration_revision_and_order_baseline(integration_state: IntegrationState) -> None:
    assert integration_state.revision == EXPECTED_REVISION
    assert integration_state.before_order_count >= 0
    assert integration_state.before_unmatched_count >= 0


@pytest.mark.asyncio
async def test_repositories_read_write_real_tables(integration_state: IntegrationState) -> None:
    conn = await psycopg.AsyncConnection.connect(integration_state.dsn)
    try:
        order_id = await _insert_order(conn, integration_state)
        after_sale = _new_after_sale(integration_state, order_id)
        after_sales = PsycopgAfterSaleRepository(conn)
        inserted = await after_sales.insert_submitted(after_sale)
        locked = await after_sales.get_for_update(after_sale.id)
        active = await after_sales.find_active_by_order(LegacyOrderId(order_id))
        assert inserted.id == after_sale.id
        assert locked is not None
        assert active is not None

        updated = await after_sales.update_status(
            after_sale.id,
            NonNegativeInt(0),
            AfterSaleTransitionUpdate(
                target_status=AfterSaleStatus.UNDER_REVIEW,
                updated_at=datetime.now(timezone.utc),
            ),
        )
        assert updated is not None
        assert updated.version == NonNegativeInt(1)
        assert (
            await after_sales.update_status(
                after_sale.id,
                NonNegativeInt(0),
                AfterSaleTransitionUpdate(
                    target_status=AfterSaleStatus.CANCELLED,
                    updated_at=datetime.now(timezone.utc),
                ),
            )
            is None
        )

        refunds = PsycopgRefundRepository(conn)
        refund = await refunds.insert_created(
            NewRefundRecord(
                id=uuid4(),
                after_sale_request_id=after_sale.id,
                payment_transaction_ref=after_sale.payment_transaction_ref,
                merchant_refund_request_no=MerchantRefundRequestNo("merchant-" + uuid4().hex),
                currency=Currency.CNY,
                amount_cents=PositiveCents(1000),
                status=RefundStatus.CREATED,
                created_at=datetime.now(timezone.utc),
            )
        )
        assert await refunds.get_for_update(refund.id) is not None
        assert await refunds.get_for_update_by_after_sale(after_sale.id) is not None
        processing_at = datetime.now(timezone.utc)
        processing = await refunds.update_status(
            refund.id,
            NonNegativeInt(0),
            RefundStatusUpdate(
                target_status=RefundStatus.PROCESSING,
                updated_at=processing_at,
                processing_at=processing_at,
            ),
        )
        assert processing is not None
        assert processing.processing_at == processing_at
        callback = await refunds.save_verified_callback(
            refund.id,
            NonNegativeInt(1),
            VerifiedCallbackUpdate(
                external_event_id=ExternalEventId("event-" + uuid4().hex),
                merchant_refund_request_no=refund.merchant_refund_request_no,
                external_refund_id=ExternalRefundId("external-" + uuid4().hex),
                external_status=NormalizedExternalStatus("SUCCESS"),
                amount_cents=PositiveCents(1000),
                currency=Currency.CNY,
                callback_at=datetime.now(timezone.utc),
            ),
        )
        assert callback is not None

        audits = PsycopgAuditRepository(conn)
        audit_id = uuid4()
        idempotency_key = IdempotencyKey("idem-" + uuid4().hex)
        audit = await audits.append(
            NewAuditEvent(
                id=audit_id,
                resource_type=ResourceType.REFUND,
                resource_id=refund.id,
                owner_user_id=integration_state.customer_user_id,
                actor_type=ActorType.CUSTOMER,
                actor_user_id=integration_state.customer_user_id,
                action=AuditAction("refund_created"),
                from_status=None,
                to_status=RefundStatus.CREATED,
                expected_version=NonNegativeInt(0),
                new_version=NonNegativeInt(0),
                reason_code=DecisionReasonCode("approved"),
                policy_version=None,
                request_id="req-" + uuid4().hex,
                trace_id="trace-" + uuid4().hex,
                span_id="span-" + uuid4().hex[:16],
                command_name=AfterSaleCommand.FINANCE_APPROVE,
                idempotency_key=idempotency_key,
                request_hash="a" * 64,
                result_resource_type=ResourceType.REFUND,
                result_resource_id=refund.id,
                result_version=NonNegativeInt(0),
                external_event_id=None,
                merchant_refund_request_no=None,
                external_refund_id=None,
                metadata=AuditMetadata(source=Source.API),
            )
        )
        assert audit.id == audit_id
        idempotency_record = await audits.find_command_idempotency(
            integration_state.customer_user_id,
            AfterSaleCommand.FINANCE_APPROVE,
            idempotency_key,
        )
        assert idempotency_record is not None
        assert idempotency_record.audit_event_id == audit_id

        outboxes = PsycopgOutboxRepository(conn)
        outbox_id = uuid4()
        outbox = await outboxes.insert_refund_authorized(
            NewRefundOutboxEvent(
                id=outbox_id,
                refund_id=refund.id,
                event_type=OutboxEventType.REFUND_REQUEST_AUTHORIZED,
                idempotency_key=IdempotencyKey("outbox-" + uuid4().hex),
                payload=RefundOutboxPayload(
                    outbox_event_id=outbox_id,
                    refund_id=refund.id,
                    after_sale_request_id=after_sale.id,
                    merchant_refund_request_no=refund.merchant_refund_request_no,
                    payment_transaction_ref=refund.payment_transaction_ref,
                    amount_cents=PositiveCents(1000),
                    currency=Currency.CNY,
                    attempt=PositiveInt(1),
                    created_at=datetime.now(timezone.utc),
                ),
            )
        )
        assert outbox.created_at is not None
        delivered = await outboxes.update_delivery_state(
            outbox.id,
            OutboxStatus.PENDING,
            OutboxDeliveryUpdate(
                status=OutboxStatus.PROCESSING,
                attempt_count=NonNegativeInt(1),
                updated_at=datetime.now(timezone.utc),
                last_error_code=DomainErrorCode.DEPENDENCY_UNAVAILABLE,
            ),
        )
        assert delivered is not None
        assert delivered.created_at == outbox.created_at
    finally:
        await conn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_select_for_update_blocks_second_transaction(integration_state: IntegrationState) -> None:
    after_sale_id = await _create_committed_after_sale(integration_state)
    first = await psycopg.AsyncConnection.connect(integration_state.dsn)
    second = await psycopg.AsyncConnection.connect(integration_state.dsn)
    try:
        assert await PsycopgAfterSaleRepository(first).get_for_update(after_sale_id) is not None
        task = asyncio.create_task(PsycopgAfterSaleRepository(second).get_for_update(after_sale_id))
        await asyncio.sleep(0.1)
        assert not task.done()
        await first.commit()
        assert await task is not None
        await second.rollback()
    finally:
        await first.rollback()
        await second.rollback()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_expected_version_conflict_does_not_update_real_row(
    integration_state: IntegrationState,
) -> None:
    after_sale_id = await _create_committed_after_sale(integration_state)
    conn = await psycopg.AsyncConnection.connect(integration_state.dsn)
    try:
        result = await PsycopgAfterSaleRepository(conn).update_status(
            after_sale_id,
            NonNegativeInt(99),
            AfterSaleTransitionUpdate(
                target_status=AfterSaleStatus.CANCELLED,
                updated_at=datetime.now(timezone.utc),
            ),
        )
        assert result is None
        row = await _fetchone(
            conn,
            "SELECT status, version FROM public.after_sale_requests WHERE id = %s",
            (after_sale_id,),
        )
        assert row == ("SUBMITTED", 0)
    finally:
        await conn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_two_agents_only_one_claim_succeeds(integration_state: IntegrationState) -> None:
    after_sale_id = await _create_committed_after_sale(integration_state)
    first = await psycopg.AsyncConnection.connect(integration_state.dsn)
    second = await psycopg.AsyncConnection.connect(integration_state.dsn)
    try:
        now = datetime.now(timezone.utc)
        winner = await PsycopgAfterSaleRepository(first).claim_if_available(
            after_sale_id,
            integration_state.agent_a_id,
            NonNegativeInt(0),
            now,
            now + timedelta(hours=24),
        )
        assert winner is not None

        loser_task = asyncio.create_task(
            PsycopgAfterSaleRepository(second).claim_if_available(
                after_sale_id,
                integration_state.agent_b_id,
                NonNegativeInt(0),
                now,
                now + timedelta(hours=24),
            )
        )
        await asyncio.sleep(0.1)
        assert not loser_task.done()
        await first.commit()
        loser = await loser_task
        await second.commit()
        assert loser is None
        assert winner.assigned_agent_id == integration_state.agent_a_id
    finally:
        await first.rollback()
        await second.rollback()
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_unit_of_work_commits_real_transaction(integration_state: IntegrationState) -> None:
    conn = await psycopg.AsyncConnection.connect(integration_state.dsn)
    after_sale_id = uuid4()
    try:
        order_id = await _insert_order(conn, integration_state)
        await conn.commit()
        integration_state.created_after_sale_ids.add(after_sale_id)
        async with RefundUnitOfWork(conn) as unit_of_work:
            inserted = await unit_of_work.after_sales.insert_submitted(
                _new_after_sale(integration_state, order_id, after_sale_id)
            )
        assert inserted.id == after_sale_id
        row = await _fetchone(
            conn,
            "SELECT id FROM public.after_sale_requests WHERE id = %s",
            (after_sale_id,),
        )
        assert row == (after_sale_id,)
    finally:
        await conn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_unit_of_work_rolls_back_real_transaction(integration_state: IntegrationState) -> None:
    conn = await psycopg.AsyncConnection.connect(integration_state.dsn)
    after_sale_id = uuid4()
    try:
        order_id = await _insert_order(conn, integration_state)
        await conn.commit()
        with pytest.raises(RuntimeError, match="rollback sentinel"):
            async with RefundUnitOfWork(conn) as unit_of_work:
                await unit_of_work.after_sales.insert_submitted(
                    _new_after_sale(integration_state, order_id, after_sale_id)
                )
                raise RuntimeError("rollback sentinel")
        row = await _fetchone(
            conn,
            "SELECT id FROM public.after_sale_requests WHERE id = %s",
            (after_sale_id,),
        )
        assert row is None
    finally:
        await conn.rollback()
        await conn.close()
