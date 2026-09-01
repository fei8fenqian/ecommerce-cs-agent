"""checkout 退款存储的真实 PostgreSQL 验证。

只能显式指向 ecommerce_agent_refund_test。测试创建完全合成的用户和交易，结束时
按依赖倒序删除，绝不读取或修改业务库及 legacy orders。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import psycopg
import pytest
import pytest_asyncio

from agent.engines.loop import LoopResult
from agent.engines.support_workflow import SupportWorkflowAgent
from agent.tools.query_refund_status import QueryRefundStatus
from agent.tools.track_order import TrackOrder
from agent.tools_registry import ToolContext, ToolRegistry
from config import settings
from infra.db_pool import close_pool, init_pool
from infra.unionpay_test import UNIONPAY_TIMEZONE, UnionPayQueryResult, UnionPayRefundResult
from service.checkout_refund_service import confirm_customer_refund
from store.checkout_refund_store import (
    create_customer_refund_request,
    get_customer_checkout_refund,
    mark_checkout_refund_succeeded,
    start_customer_refund_confirmation,
    start_finance_refund_approval,
)
from store.checkout_store import mark_fulfillment_shipped

TARGET_DATABASE = "ecommerce_agent_refund_test"
EXPECTED_REVISION = "c6f4a9e2b817"
pytestmark = pytest.mark.skipif(
    settings.pg_dbname != TARGET_DATABASE,
    reason=f"checkout refund integration requires {TARGET_DATABASE}",
)


class _WorkflowAnswerAgent:
    """只固定最终话术，工具和状态判断仍走真实 SupportWorkflow。"""

    async def run(self, query, *, context="", history=None, system_prompt_extra="", tool_context=None):
        return LoopResult(answer="退款正在处理中，当前没有可信的预计到账时间。", total_steps=0)


def _dsn() -> str:
    """构造仅供独立测试库使用的 psycopg 连接串。"""
    return (
        f"host={settings.pg_host} port={settings.pg_port} dbname={settings.pg_dbname} "
        f"user={settings.pg_user} password={settings.pg_password.get_secret_value()}"
    )


@dataclass(frozen=True)
class RefundIntegrationState:
    """一笔完全合成、未发货的已支付 checkout 订单。"""

    customer_user_id: int
    sales_order_id: UUID
    payment_transaction_id: UUID
    order_no: str


@pytest_asyncio.fixture
async def refund_state() -> AsyncIterator[RefundIntegrationState]:
    """写入并最终清理退款测试夹具，禁止测试目标漂移到业务库。"""
    if settings.pg_dbname != TARGET_DATABASE:
        pytest.fail(f"退款集成测试只允许连接 {TARGET_DATABASE!r}，当前为 {settings.pg_dbname!r}。")

    connection = await psycopg.AsyncConnection.connect(_dsn())
    user_id: int | None = None
    sales_order_id = uuid4()
    payment_transaction_id = uuid4()
    try:
        revision_cursor = await connection.execute("SELECT version_num FROM public.alembic_version")
        revision = await revision_cursor.fetchone()
        assert revision == (EXPECTED_REVISION,)

        suffix = uuid4().hex[:18]
        user_cursor = await connection.execute(
            """
            INSERT INTO public.users (username, password_hash, role)
            VALUES (%s, %s, 'customer')
            RETURNING id
            """,
            (f"refund-integration-{suffix}", "synthetic-only"),
        )
        user_row = await user_cursor.fetchone()
        assert user_row is not None
        user_id = int(user_row[0])
        order_no = f"SOREF{suffix.upper()}"
        await connection.execute(
            """
            INSERT INTO public.sales_orders (
                id, order_no, customer_user_id, status, total_amount_cents, currency
            ) VALUES (%s, %s, %s, 'PAID', %s, 'CNY')
            """,
            (sales_order_id, order_no, user_id, 529900),
        )
        await connection.execute(
            """
            INSERT INTO public.sales_order_items (
                sales_order_id, catalog_category, catalog_product_id, product_name,
                brand, unit_amount_cents, quantity
            ) VALUES (%s, 'laptops', 'synthetic-laptop', '合成退款测试笔记本', 'Geex', %s, 1)
            """,
            (sales_order_id, 529900),
        )
        await connection.execute(
            """
            INSERT INTO public.payment_transactions (
                id, sales_order_id, provider, merchant_payment_no, status, amount_cents,
                currency, succeeded_at
            ) VALUES (%s, %s, 'alipay_sandbox', %s, 'SUCCEEDED', %s, 'CNY', NOW())
            """,
            (payment_transaction_id, sales_order_id, f"PMSYN{suffix.upper()}", 529900),
        )
        await connection.execute(
            """
            INSERT INTO public.fulfillments (id, sales_order_id, status)
            VALUES (%s, %s, 'PENDING_FULFILLMENT')
            """,
            (uuid4(), sales_order_id),
        )
        await connection.commit()
        await init_pool(minconn=1, maxconn=3)
        yield RefundIntegrationState(user_id, sales_order_id, payment_transaction_id, order_no)
    finally:
        await close_pool()
        await connection.rollback()
        if user_id is not None:
            await connection.execute("DELETE FROM public.checkout_refunds WHERE sales_order_id = %s", (sales_order_id,))
            await connection.execute("DELETE FROM public.fulfillments WHERE sales_order_id = %s", (sales_order_id,))
            await connection.execute(
                "DELETE FROM public.payment_transactions WHERE sales_order_id = %s", (sales_order_id,)
            )
            await connection.execute(
                "DELETE FROM public.sales_order_items WHERE sales_order_id = %s", (sales_order_id,)
            )
            await connection.execute("DELETE FROM public.sales_orders WHERE id = %s", (sales_order_id,))
            await connection.execute("DELETE FROM public.users WHERE id = %s", (user_id,))
            await connection.commit()
        await connection.close()


@pytest.mark.asyncio
async def test_refund_request_confirmation_and_success_are_atomic_and_idempotent(
    refund_state: RefundIntegrationState,
) -> None:
    """真实表验证：一个订单只能有一笔退款，重放不会重复生成或再次取得提交权。"""
    created = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFSYN-0001",
        request_idempotency_key="request-synthetic-0001",
        reason="合成测试退款",
    )
    assert created is not None
    assert created.status == "PENDING_CONFIRMATION"
    assert created.amount_cents == 529900

    replay = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFSYN-should-not-exist",
        request_idempotency_key="request-synthetic-0001",
        reason="ignored on replay",
    )
    assert replay is not None
    assert replay.refund_id == created.refund_id
    assert replay.merchant_refund_no == "RFSYN-0001"

    lost_response_retry = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFSYN-should-not-exist-either",
        request_idempotency_key="request-synthetic-new-browser-key",
        reason="lost response retry",
    )
    assert lost_response_retry is not None
    assert lost_response_retry.refund_id == created.refund_id

    first_confirmation = await start_customer_refund_confirmation(
        customer_user_id=refund_state.customer_user_id,
        refund_id=created.refund_id,
        confirmation_idempotency_key="confirm-synthetic-0001",
    )
    assert first_confirmation is not None
    assert first_confirmation.should_submit_to_provider is True
    assert first_confirmation.refund.status == "PROCESSING"

    confirmation_replay = await start_customer_refund_confirmation(
        customer_user_id=refund_state.customer_user_id,
        refund_id=created.refund_id,
        confirmation_idempotency_key="confirm-synthetic-0001",
    )
    assert confirmation_replay is not None
    assert confirmation_replay.should_submit_to_provider is False

    succeeded = await mark_checkout_refund_succeeded(
        refund_id=created.refund_id,
        provider_refund_reference="sandbox-trade-synthetic-1",
    )
    assert succeeded is not None
    assert succeeded.status == "SUCCEEDED"
    fetched = await get_customer_checkout_refund(refund_state.customer_user_id, created.refund_id)
    assert fetched is not None
    assert fetched.status == "SUCCEEDED"

    connection = await psycopg.AsyncConnection.connect(_dsn())
    try:
        order_cursor = await connection.execute(
            "SELECT status FROM public.sales_orders WHERE id = %s", (refund_state.sales_order_id,)
        )
        refund_count_cursor = await connection.execute(
            "SELECT COUNT(*) FROM public.checkout_refunds WHERE sales_order_id = %s", (refund_state.sales_order_id,)
        )
        assert await order_cursor.fetchone() == ("REFUNDED",)
        assert await refund_count_cursor.fetchone() == (1,)
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_support_workflow_reads_real_processing_refund_and_resolves_without_eta(
    refund_state: RefundIntegrationState,
) -> None:
    """真实 checkout 数据经 ToolRegistry 和 Workflow 后，处理中状态可完成查询目标。"""
    created = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFSYN-WORKFLOW-0001",
        request_idempotency_key="request-synthetic-workflow-0001",
        reason="合成工作流测试",
    )
    assert created is not None
    started = await start_customer_refund_confirmation(
        customer_user_id=refund_state.customer_user_id,
        refund_id=created.refund_id,
        confirmation_idempotency_key="confirm-synthetic-workflow-0001",
    )
    assert started is not None
    assert started.refund.status == "PROCESSING"

    registry = ToolRegistry()
    registry.register(TrackOrder())
    registry.register(QueryRefundStatus())
    result = await SupportWorkflowAgent(_WorkflowAnswerAgent(), registry).run(
        "我申请的退款现在到哪了？",
        support_requests=[{"domain": "refund", "operation": "refund_status"}],
        tool_context=ToolContext(user_id=refund_state.customer_user_id, role="customer"),
    )

    assert result.workflow_progress["goal_status"] == "resolved"
    assert result.workflow_progress["control_state"] == "RESOLVED"
    assert result.workflow_progress["decision_facts"]["refund_status"] == "PROCESSING"
    assert result.workflow_progress["missing_facts"] == []
    assert "预计到账时间" in result.answer


@pytest.mark.asyncio
async def test_unionpay_refund_persists_processing_time_and_converges_real_postgres(
    refund_state: RefundIntegrationState,
) -> None:
    """真实 Store 状态机：仅银联 HTTP 被 mock，资金绑定仍由真实表验证。"""
    connection = await psycopg.AsyncConnection.connect(_dsn())
    try:
        await connection.execute(
            """
            UPDATE public.payment_transactions
            SET provider = 'unionpay_test', provider_trade_no = 'UP-ORIGINAL-QUERY-1',
                provider_txn_time = '20260901080000'
            WHERE id = %s
            """,
            (refund_state.payment_transaction_id,),
        )
        await connection.commit()
    finally:
        await connection.close()

    created = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFSTABLEUNIONPAY001",
        request_idempotency_key="unionpay-real-store-request-0001",
        reason="合成银联退款",
    )
    assert created is not None
    assert created.status == "PENDING_CONFIRMATION"
    assert created.processing_at is None

    client = AsyncMock()

    async def refund_transaction(**kwargs: object) -> UnionPayRefundResult:
        return UnionPayRefundResult(
            signature_verified=True,
            resp_code="00",
            order_id=str(kwargs["order_id"]),
            txn_time=str(kwargs["txn_time"]),
            txn_amt=str(kwargs["txn_amt"]),
            orig_qry_id=str(kwargs["orig_qry_id"]),
        )

    async def query_transaction(*, order_id: str, txn_time: str) -> UnionPayQueryResult:
        return UnionPayQueryResult(
            signature_verified=True,
            resp_code="00",
            orig_resp_code="00",
            query_id="UP-REFUND-QUERY-1",
            txn_amt="529900",
            order_id=order_id,
            txn_time=txn_time,
            orig_qry_id="UP-ORIGINAL-QUERY-1",
        )

    client.refund_transaction.side_effect = refund_transaction
    client.query_transaction.side_effect = query_transaction
    with patch("service.checkout_refund_service.UnionPayTestClient.from_settings", return_value=client):
        result = await confirm_customer_refund(
            customer_user_id=refund_state.customer_user_id,
            refund_id=created.refund_id,
            confirmation_idempotency_key="unionpay-real-store-confirm-0001",
        )

    assert result.status == "SUCCEEDED"
    persisted = await get_customer_checkout_refund(refund_state.customer_user_id, created.refund_id)
    assert persisted is not None and persisted.processing_at is not None
    expected_txn_time = (
        datetime.fromisoformat(persisted.processing_at).astimezone(UNIONPAY_TIMEZONE).strftime("%Y%m%d%H%M%S")
    )
    assert client.refund_transaction.await_args is not None
    assert client.refund_transaction.await_args.kwargs["txn_time"] == expected_txn_time
    assert client.refund_transaction.await_args.kwargs["orig_qry_id"] == "UP-ORIGINAL-QUERY-1"
    assert client.query_transaction.await_args is not None
    assert client.query_transaction.await_args.kwargs["txn_time"] == expected_txn_time

    connection = await psycopg.AsyncConnection.connect(_dsn())
    try:
        order_cursor = await connection.execute(
            "SELECT status FROM public.sales_orders WHERE id = %s", (refund_state.sales_order_id,)
        )
        refund_cursor = await connection.execute(
            "SELECT status, processing_at FROM public.checkout_refunds WHERE id = %s", (created.refund_id,)
        )
        assert await order_cursor.fetchone() == ("REFUNDED",)
        status_row = await refund_cursor.fetchone()
        assert status_row is not None and status_row[0] == "SUCCEEDED" and status_row[1] is not None
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_active_or_finished_refund_blocks_operator_shipping(
    refund_state: RefundIntegrationState,
) -> None:
    """列表过滤之外，实际发货写边界也必须拒绝退款中的/完成订单。"""
    created = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFRACEPENDING001",
        request_idempotency_key="race-pending-request-0001",
        reason="并发边界测试",
    )
    assert created is not None
    assert (
        await mark_fulfillment_shipped(
            order_no=refund_state.order_no, carrier="测试物流", tracking_number="RACE-PENDING"
        )
        is None
    )

    started = await start_customer_refund_confirmation(
        customer_user_id=refund_state.customer_user_id,
        refund_id=created.refund_id,
        confirmation_idempotency_key="race-processing-confirm-0001",
    )
    assert started is not None and started.refund.status == "PROCESSING"
    assert (
        await mark_fulfillment_shipped(
            order_no=refund_state.order_no, carrier="测试物流", tracking_number="RACE-PROCESSING"
        )
        is None
    )

    succeeded = await mark_checkout_refund_succeeded(refund_id=created.refund_id, provider_refund_reference="RACE-REF")
    assert succeeded is not None
    assert (
        await mark_fulfillment_shipped(
            order_no=refund_state.order_no, carrier="测试物流", tracking_number="RACE-FINISHED"
        )
        is None
    )


@pytest.mark.asyncio
async def test_finance_confirmation_locks_current_fulfillment_and_persists_submission_time(
    refund_state: RefundIntegrationState,
) -> None:
    """财务路径与客户确认使用同一订单/支付/履约提交边界。"""
    created = await create_customer_refund_request(
        refund_id=uuid4(),
        customer_user_id=refund_state.customer_user_id,
        order_no=refund_state.order_no,
        merchant_refund_no="RFFINANCETIME001",
        request_idempotency_key="finance-processing-time-request-0001",
        reason="高金额合成退款",
        status="PENDING_FINANCE_APPROVAL",
    )
    assert created is not None
    assert (
        await mark_fulfillment_shipped(
            order_no=refund_state.order_no, carrier="测试物流", tracking_number="RACE-FINANCE"
        )
        is None
    )
    started = await start_finance_refund_approval(
        finance_user_id=refund_state.customer_user_id,
        refund_id=created.refund_id,
        decision_idempotency_key="finance-processing-time-approve-0001",
        decision_note="合成审批",
    )
    assert started is not None and started.should_submit_to_provider
    assert started.refund.status == "PROCESSING"
    assert started.refund.processing_at is not None
