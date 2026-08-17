"""tests/test_ticket_store.py — 工单数据访问层单元测试"""

import pytest
import pytest_asyncio

from infra.db_pool import close_pool, get_connection, init_pool, put_connection
from store.ticket_store import (
    create_ticket,
    get_ticket,
    init_ticket_table,
    list_tickets,
    update_ticket,
)

pytestmark = pytest.mark.asyncio


# ============================================================================
# 每个测试前后重置
# =============================================================================
@pytest_asyncio.fixture(autouse=True)
async def _setup():
    """每个测试前建表，测试后清空"""
    await init_pool(minconn=1, maxconn=2)
    await init_ticket_table()
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM tickets")
    await put_connection(conn)
    yield
    conn = await get_connection()
    await conn.set_autocommit(True)
    await conn.execute("DELETE FROM tickets")
    await put_connection(conn)
    await close_pool()


# =============================================================================
# init_ticket_table — 建表幂等
# =============================================================================
class TestInitTable:
    @pytest.mark.asyncio
    async def test_init_ticket_table_idempotent(self):
        """连续调用 init_ticket_table 不应报错"""
        await init_ticket_table()
        await init_ticket_table()

    @pytest.mark.asyncio
    async def test_table_exists_after_init(self):
        """init_ticket_table 后表存在且可写"""
        await create_ticket("TK-TEST-001", "测试工单", customer_name="小明")
        ticket = await get_ticket("TK-TEST-001")
        assert ticket is not None
        assert ticket["issue"] == "测试工单"


# =============================================================================
# create_ticket + get_ticket — 基本 CRUD
# =============================================================================
class TestCreateAndGet:
    @pytest.mark.asyncio
    async def test_create_and_get_roundtrip(self):
        """创建 → 查询，数据一致"""
        await create_ticket(
            ticket_id="TK-20240101-001",
            issue="屏幕有坏点，要求换货",
            customer_name="张三",
            phone="13800138000",
            urgency="high",
        )

        ticket = await get_ticket("TK-20240101-001")
        assert ticket is not None
        assert ticket["ticket_id"] == "TK-20240101-001"
        assert ticket["customer_name"] == "张三"
        assert ticket["phone"] == "13800138000"
        assert ticket["issue"] == "屏幕有坏点，要求换货"
        assert ticket["urgency"] == "high"
        assert ticket["status"] == "待处理"
        assert "created_at" in ticket

    @pytest.mark.asyncio
    async def test_create_minimal_fields(self):
        """最少字段创建（只有 ticket_id + issue）"""
        await create_ticket(ticket_id="TK-MINIMAL", issue="最小工单")
        ticket = await get_ticket("TK-MINIMAL")
        assert ticket is not None
        assert ticket["customer_name"] == ""
        assert ticket["phone"] == ""
        assert ticket["urgency"] == "medium"

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self):
        """查不存在的工单 → None"""
        ticket = await get_ticket("TK-NOT-EXISTS")
        assert ticket is None


# =============================================================================
# list_tickets — 列表查询
# =============================================================================
class TestListTickets:
    @pytest.mark.asyncio
    async def test_list_all(self):
        """不传 status → 返回所有工单，按时间倒序"""
        await create_ticket("TK-LIST-1", "工单1", urgency="low")
        await create_ticket("TK-LIST-2", "工单2", urgency="high")
        await create_ticket("TK-LIST-3", "工单3", urgency="medium")

        tickets = await list_tickets()
        assert len(tickets) == 3
        # 倒序：最后创建的排第一
        assert tickets[0]["ticket_id"] == "TK-LIST-3"

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self):
        """按 status 过滤"""
        await create_ticket("TK-ST1", "待处理工单")
        # 手动改状态
        await update_ticket("TK-ST1", status="已处理")
        await create_ticket("TK-ST2", "另一个待处理")

        pending = await list_tickets(status="待处理")
        processed = await list_tickets(status="已处理")

        assert len(pending) == 1
        assert pending[0]["ticket_id"] == "TK-ST2"
        assert len(processed) == 1
        assert processed[0]["ticket_id"] == "TK-ST1"

    @pytest.mark.asyncio
    async def test_list_empty(self):
        """空表 → 空列表"""
        tickets = await list_tickets()
        assert tickets == []

    @pytest.mark.asyncio
    async def test_list_returns_expected_fields(self):
        """返回的每条记录包含必要字段"""
        await create_ticket("TK-FIELDS", "字段测试", customer_name="李四", urgency="critical")
        tickets = await list_tickets()
        assert len(tickets) == 1
        t = tickets[0]
        assert "ticket_id" in t
        assert "customer_name" in t
        assert "urgency" in t
        assert "status" in t
        assert "created_at" in t


# =============================================================================
# update_ticket — 工单更新
# =============================================================================
class TestUpdateTicket:
    @pytest.mark.asyncio
    async def test_update_status(self):
        """更新工单状态"""
        await create_ticket("TK-UPD-1", "测试更新")

        result = await update_ticket("TK-UPD-1", status="处理中")
        assert result is True

        ticket = await get_ticket("TK-UPD-1")
        assert ticket["status"] == "处理中"

    @pytest.mark.asyncio
    async def test_update_urgency(self):
        """更新紧急程度"""
        await create_ticket("TK-UPD-2", "紧急工单", urgency="low")

        result = await update_ticket("TK-UPD-2", urgency="critical")
        assert result is True

        ticket = await get_ticket("TK-UPD-2")
        assert ticket["urgency"] == "critical"

    @pytest.mark.asyncio
    async def test_update_multiple_fields(self):
        """同时更新多个字段"""
        await create_ticket("TK-UPD-3", "多字段更新", urgency="medium")

        result = await update_ticket("TK-UPD-3", status="已完成", urgency="low")
        assert result is True

        ticket = await get_ticket("TK-UPD-3")
        assert ticket["status"] == "已完成"
        assert ticket["urgency"] == "low"

    @pytest.mark.asyncio
    async def test_update_nonexistent_returns_false(self):
        """更新不存在的工单 → False"""
        result = await update_ticket("TK-NOT-EXISTS", status="已处理")
        assert result is False

    @pytest.mark.asyncio
    async def test_update_no_changes(self):
        """没有传任何有效字段 → True（幂等）"""
        await create_ticket("TK-NOCHG", "不变")

        result = await update_ticket("TK-NOCHG")
        assert result is True

    @pytest.mark.asyncio
    async def test_update_disallowed_fields_ignored(self):
        """不允许更新的字段会被忽略（白名单机制）"""
        await create_ticket("TK-IGNORE", "测试白名单")

        # 尝试更新 issue（不在白名单中），应被忽略
        result = await update_ticket("TK-IGNORE", issue="被黑了吗", status="处理中")
        assert result is True

        # issue 没变，status 变了
        ticket = await get_ticket("TK-IGNORE")
        assert ticket["issue"] == "测试白名单"
        assert ticket["status"] == "处理中"

    @pytest.mark.asyncio
    async def test_update_none_values_ignored(self):
        """传 None 的字段应该被跳过"""
        await create_ticket("TK-NONE", "None 测试", urgency="high")

        result = await update_ticket("TK-NONE", status=None, urgency=None)
        assert result is True

        ticket = await get_ticket("TK-NONE")
        assert ticket["urgency"] == "high"  # 没变


# =============================================================================
# get_ticket — 返回字段完整性
# =============================================================================
class TestGetTicketFields:
    @pytest.mark.asyncio
    async def test_all_fields_present(self):
        """get_ticket 返回所有必要字段"""
        await create_ticket(
            ticket_id="TK-FULL",
            issue="屏幕花屏",
            customer_name="王五",
            phone="13900139000",
            urgency="critical",
        )

        ticket = await get_ticket("TK-FULL")
        assert set(ticket.keys()) == {
            "ticket_id",
            "customer_user_id",
            "customer_name",
            "phone",
            "issue",
            "urgency",
            "status",
            "created_at",
        }

    @pytest.mark.asyncio
    async def test_created_at_is_isoformat(self):
        """created_at 应该是 ISO 格式字符串"""
        await create_ticket("TK-DATE", "日期测试")
        ticket = await get_ticket("TK-DATE")
        assert ticket is not None
        assert "T" in ticket["created_at"]  # ISO 格式含 T
