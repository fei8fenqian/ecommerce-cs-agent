"""checkout 退款存储的真实 PostgreSQL 验证。

只能显式指向 ecommerce_agent_refund_test。测试创建完全合成的用户和交易，结束时
按依赖倒序删除，绝不读取或修改业务库及 legacy orders。
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
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
from store.checkout_refund_store import (
    create_customer_refund_request,
    get_customer_checkout_refund,
    mark_checkout_refund_succeeded,
    start_customer_refund_confirmation,
)

TARGET_DATABASE = "ecommerce_agent_refund_test"
EXPECTED_REVISION = "a9e4c7d2f813"
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
