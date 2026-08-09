"""tests/test_tools_create_ticket.py — 创建工单工具测试"""

import pytest
import pytest_asyncio

from agent.tools.create_ticket import ALLOWED_URGENCY, CreateTicket
from agent.tools_registry import ToolResult
from core.ticket_store import get_ticket, init_table
from infra.db_pool import close_pool, get_connection, init_pool, put_connection


@pytest_asyncio.fixture
async def _setup():
    await init_pool(minconn=1, maxconn=2)
    await init_table()
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
# 工具元数据（sync，不需要 DB）
# =============================================================================
class TestCreateTicketMeta:
    def test_name(self):
        tool = CreateTicket()
        assert tool.name == "create_ticket"

    def test_description_contains_keywords(self):
        tool = CreateTicket()
        assert "工单" in tool.description

    def test_parameters_require_issue(self):
        tool = CreateTicket()
        assert "issue" in tool.parameters["required"]

    def test_urgency_enum_values(self):
        tool = CreateTicket()
        enum_vals = tool.parameters["properties"]["urgency"]["enum"]
        assert set(enum_vals) == set(ALLOWED_URGENCY)

    def test_to_openai_schema(self):
        tool = CreateTicket()
        schema = tool.to_openai_function()
        assert schema["function"]["name"] == "create_ticket"


# =============================================================================
# execute — 正常流程
# =============================================================================
class TestCreateTicketExecute:
    @pytest.mark.asyncio
    async def test_create_ticket_success(self, _setup):
        """创建工单成功 → 返回 ticket_id"""
        tool = CreateTicket()
        result = await tool.execute(
            issue="屏幕花屏，要求退货",
            customer_name="张三",
            phone="13800138000",
            urgency="high",
        )

        assert isinstance(result, ToolResult)
        assert result.is_success is True
        assert "ticket_id" in result.data
        assert result.data["ticket_id"].startswith("TK")
        assert result.data["status"] == "待处理"
        assert result.data["urgency"] == "high"

        ticket = await get_ticket(result.data["ticket_id"])
        assert ticket is not None
        assert ticket["issue"] == "屏幕花屏，要求退货"
        assert ticket["customer_name"] == "张三"

    @pytest.mark.asyncio
    async def test_create_ticket_minimal(self, _setup):
        """最少字段创建"""
        tool = CreateTicket()
        result = await tool.execute(issue="用户投诉")

        assert result.is_success is True
        assert result.data["urgency"] == "medium"
        assert result.data["ticket_id"].startswith("TK")

    @pytest.mark.asyncio
    async def test_create_ticket_empty_contact(self, _setup):
        """不提供联系方式 → 仍成功"""
        tool = CreateTicket()
        result = await tool.execute(issue="测试", customer_name="", phone="")
        assert result.is_success is True

    @pytest.mark.asyncio
    async def test_ticket_id_format(self, _setup):
        """ticket_id 格式：TK + 14位时间 + 3位随机数"""
        tool = CreateTicket()
        result = await tool.execute(issue="ID 格式测试")
        tid = result.data["ticket_id"]
        assert tid.startswith("TK")
        assert len(tid) == 2 + 14 + 3

    @pytest.mark.asyncio
    async def test_all_urgency_levels(self, _setup):
        """所有 urgency 级别都能正常创建"""
        tool = CreateTicket()
        for level in ALLOWED_URGENCY:
            result = await tool.execute(issue=f"测试-{level}", urgency=level)
            assert result.is_success is True
            assert result.data["urgency"] == level


# =============================================================================
# execute — 异常路径
# =============================================================================
class TestCreateTicketErrors:
    @pytest.mark.asyncio
    async def test_invalid_urgency_defaults_to_medium(self, _setup):
        """非法的 urgency 值 → 自动降级为 medium"""
        tool = CreateTicket()
        result = await tool.execute(issue="测试", urgency="super_urgent")
        assert result.is_success is True
        assert result.data["urgency"] == "medium"
