"""tests/test_session.py — 多轮对话 Session 管理 + 指代消解单元测试"""

import uuid

import pytest
import pytest_asyncio

from agent.engines.loop import LoopResult, StepResult
from agent.llm.llm_client import ToolCall
from agent.llm.resolve import resolve_pronouns
from agent.llm.session import SessionContext, SessionManager
from infra.db_pool import close_pool, get_connection, init_pool, put_connection


# =============================================================================
# 辅助函数
# =============================================================================
def _make_loop_result(
    answer: str = "回答",
    steps: list[StepResult] | None = None,
    last_entities: dict[str, str] | None = None,
) -> LoopResult:
    return LoopResult(
        answer=answer,
        steps=steps or [],
        total_steps=len(steps) if steps else 0,
        total_tokens=100,
        total_latency_ms=500.0,
        last_entities=last_entities or {},
    )


def _make_step(
    step: int = 1,
    thought: str = "正在处理",
    tool_name: str = "echo",
    tool_args: dict | None = None,
    observation: str = "结果",
) -> StepResult:
    return StepResult(
        step=step,
        thought=thought,
        tool_calls=[ToolCall(id=f"call_{step}", name=tool_name, arguments=tool_args or {})],
        observation=observation,
        latency_ms=100.0,
    )


# =============================================================================
# resolve_pronouns — 指代消解纯函数
# =============================================================================
class TestResolvePronouns:
    def test_no_entities_returns_original(self):
        assert resolve_pronouns("你好世界", {}) == "你好世界"

    def test_empty_entities_dict_returns_original(self):
        assert resolve_pronouns("这个不错", {}) == "这个不错"

    def test_replace_ta(self):
        result = resolve_pronouns("它的价格是多少", {"product": "拯救者Y9000P"})
        assert result == "拯救者Y9000P的价格是多少"

    def test_replace_ta_male(self):
        result = resolve_pronouns("他有什么颜色", {"product": "iPhone 15"})
        assert result == "iPhone 15有什么颜色"

    def test_replace_zhege(self):
        result = resolve_pronouns("这个有黑色吗", {"product": "ThinkPad X1"})
        assert result == "ThinkPad X1有黑色吗"

    def test_replace_zhetai(self):
        result = resolve_pronouns("这台多重", {"product": "MacBook Pro"})
        assert result == "MacBook Pro多重"

    def test_replace_natai(self):
        result = resolve_pronouns("那台续航怎么样", {"product": "ROG枪神7"})
        assert result == "ROG枪神7续航怎么样"

    def test_replace_zhekuan(self):
        result = resolve_pronouns("这款适合打游戏吗", {"product": "拯救者Y7000"})
        assert result == "拯救者Y7000适合打游戏吗"

    def test_replace_gaishangpin(self):
        result = resolve_pronouns("该商品支持分期吗", {"product": "华为Mate 60"})
        assert result == "华为Mate 60支持分期吗"

    def test_replace_gaichanpin(self):
        result = resolve_pronouns("该产品的保修多久", {"product": "小米14"})
        assert result == "小米14的保修多久"

    def test_replace_zhedan(self):
        result = resolve_pronouns("这单到哪了", {"order": "ORD2026070100138"})
        assert result == "ORD2026070100138到哪了"

    def test_replace_nage_order(self):
        result = resolve_pronouns("那个订单能取消吗", {"order": "ORD001"})
        assert result == "ORD001能取消吗"

    def test_replace_gai_order(self):
        result = resolve_pronouns("该订单什么时候发货", {"order": "ORD002"})
        assert result == "ORD002什么时候发货"

    def test_pronoun_not_in_query_preserves_original(self):
        result = resolve_pronouns("今天天气不错", {"product": "拯救者"})
        assert result == "今天天气不错"

    def test_multiple_pronouns_in_same_query(self):
        result = resolve_pronouns("这个和那台哪个好", {"product": "拯救者Y9000P"})
        assert result == "拯救者Y9000P和拯救者Y9000P哪个好"

    def test_entity_key_missing_no_replace(self):
        result = resolve_pronouns("这个不错", {"order": "ORD001"})
        assert result == "这个不错"

    def test_entity_value_empty_no_replace(self):
        result = resolve_pronouns("这个不错", {"product": ""})
        assert result == "这个不错"

    def test_both_product_and_order(self):
        result = resolve_pronouns(
            "这个和该订单都帮我查一下",
            {"product": "拯救者", "order": "ORD003"},
        )
        assert result == "拯救者和ORD003都帮我查一下"


# =============================================================================
# SessionContext — 数据类
# =============================================================================
class TestSessionContext:
    def test_default_values(self):
        ctx = SessionContext(session_id="s1")
        assert ctx.session_id == "s1"
        assert ctx.messages == []
        assert ctx.last_entities == {}
        assert ctx.created_at == 0.0
        assert ctx.last_active == 0.0

    def test_custom_values(self):
        ctx = SessionContext(
            session_id="abc",
            messages=[{"role": "user", "content": "你好"}],
            last_entities={"product": "拯救者"},
            created_at=1000.0,
            last_active=2000.0,
        )
        assert ctx.session_id == "abc"
        assert len(ctx.messages) == 1
        assert ctx.last_entities["product"] == "拯救者"
        assert ctx.created_at == 1000.0
        assert ctx.last_active == 2000.0

    def test_messages_default_is_independent(self):
        a = SessionContext(session_id="a")
        b = SessionContext(session_id="b")
        a.messages.append({"role": "user", "content": "x"})
        assert b.messages == []

    def test_last_entities_default_is_independent(self):
        a = SessionContext(session_id="a")
        b = SessionContext(session_id="b")
        a.last_entities["product"] = "x"
        assert b.last_entities == {}


# =============================================================================
# SessionManager — PostgreSQL 后端
# =============================================================================
class TestSessionManager:
    """使用独立测试用户验证 PostgreSQL 会话的持久化和用户隔离。"""

    @pytest_asyncio.fixture(autouse=True)
    async def _db_pool(self):
        """在当前测试事件循环中初始化 PostgreSQL 连接池。"""
        await close_pool()
        await init_pool(minconn=1, maxconn=4)
        yield
        await close_pool()

    @pytest_asyncio.fixture
    async def owner_ids(self):
        conn = await get_connection()
        owner_ids: list[int] = []
        try:
            await conn.set_autocommit(True)
            for role in ("customer", "customer"):
                username = f"session-test-{uuid.uuid4().hex}"
                cur = await conn.execute(
                    """
                    insert into users (username, password_hash, role)
                    values (%s, %s, %s)
                    returning id
                    """,
                    (username, "test-hash", role),
                )
                row = await cur.fetchone()
                assert row is not None
                owner_ids.append(row[0])
            yield owner_ids
        finally:
            if owner_ids:
                await conn.execute(
                    "delete from sessions where owner_user_id = any(%s)",
                    (owner_ids,),
                )
                await conn.execute(
                    "delete from users where id = any(%s)",
                    (owner_ids,),
                )
            await put_connection(conn)

    @pytest_asyncio.fixture
    async def manager(self, owner_ids):
        yield SessionManager(), owner_ids[0], owner_ids[1]

    # -- get_or_create ---------------------------------------------------------
    @pytest.mark.asyncio
    async def test_get_or_create_new_session_without_id(self, manager):
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        assert ctx.session_id != ""
        assert len(ctx.session_id) == 36  # UUID4
        assert ctx.created_at > 0
        assert ctx.last_active > 0
        assert ctx.messages == []
        assert ctx.last_entities == {}

    @pytest.mark.asyncio
    async def test_get_or_create_new_session_with_id(self, manager):
        session_manager, owner_id, _ = manager
        created = await session_manager.get_or_create(None, owner_id)
        assert created is not None
        loaded = await session_manager.get_or_create(created.session_id, owner_id)
        assert loaded is not None
        assert loaded.session_id == created.session_id
        assert loaded.created_at == created.created_at

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing(self, manager):
        """同一个 session_id 返回的 SessionContext 内容相同"""
        session_manager, owner_id, _ = manager
        ctx1 = await session_manager.get_or_create(None, owner_id)
        assert ctx1 is not None
        ctx2 = await session_manager.get_or_create(ctx1.session_id, owner_id)
        assert ctx2 is not None
        assert ctx2.session_id == ctx1.session_id
        assert ctx2.created_at == ctx1.created_at  # 已存在的不会改

    @pytest.mark.asyncio
    async def test_get_or_create_updates_last_active(self, manager):
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        result = _make_loop_result(answer="更新活跃时间")
        await session_manager.add_turn(ctx.session_id, owner_id, "查询", result)
        updated = await session_manager.get(ctx.session_id, owner_id)
        assert updated is not None
        assert updated.last_active > ctx.last_active

    # -- add_turn --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_add_turn_with_tool_call(self, manager):
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        step = _make_step(
            step=1,
            thought="先查库存",
            tool_name="check_stock",
            tool_args={"product_name": "拯救者"},
            observation="库存5台",
        )
        result = _make_loop_result(
            answer="拯救者有货，5台",
            steps=[step],
            last_entities={"product": "拯救者Y9000P"},
        )

        await session_manager.add_turn(ctx.session_id, owner_id, "拯救者有货吗", result)

        # 重新从 PostgreSQL 加载，验证持久化
        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert len(ctx.messages) == 4

        assert ctx.messages[0]["role"] == "user"
        assert ctx.messages[0]["content"] == "拯救者有货吗"

        assert ctx.messages[1]["role"] == "assistant"
        assert ctx.messages[1]["content"] == "先查库存"
        assert ctx.messages[1]["tool_calls"] is not None

        assert ctx.messages[2]["role"] == "tool"
        assert ctx.messages[2]["content"] == "库存5台"

        assert ctx.messages[3]["role"] == "assistant"
        assert ctx.messages[3]["content"] == "拯救者有货，5台"

    @pytest.mark.asyncio
    async def test_add_turn_without_tool_calls(self, manager):
        """LLM 直接回答，没有调工具"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        result = _make_loop_result(answer="您好，有什么可以帮您？")

        await session_manager.add_turn(ctx.session_id, owner_id, "你好", result)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert len(ctx.messages) == 2
        assert ctx.messages[0]["role"] == "user"
        assert ctx.messages[0]["content"] == "你好"
        assert ctx.messages[1]["role"] == "assistant"
        assert ctx.messages[1]["content"] == "您好，有什么可以帮您？"

    @pytest.mark.asyncio
    async def test_add_turn_multiple_steps(self, manager):
        """多步工具调用"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        step1 = _make_step(step=1, tool_name="search_product", observation="找到了")
        step2 = _make_step(step=2, tool_name="check_stock", observation="库存3台")
        result = _make_loop_result(answer="有货，3台", steps=[step1, step2])

        await session_manager.add_turn(ctx.session_id, owner_id, "查库存", result)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert len(ctx.messages) == 6

    @pytest.mark.asyncio
    async def test_add_turn_missing_session_does_not_raise(self, manager):
        """session 不存在时不抛异常"""
        session_manager, owner_id, _ = manager
        result = _make_loop_result(answer="回答")
        await session_manager.add_turn(str(uuid.uuid4()), owner_id, "问题", result)

    @pytest.mark.asyncio
    async def test_add_turn_updates_last_entities(self, manager):
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        result = _make_loop_result(answer="回答", last_entities={"product": "拯救者Y9000P"})
        await session_manager.add_turn(ctx.session_id, owner_id, "查询", result)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert ctx.last_entities["product"] == "拯救者Y9000P"

    @pytest.mark.asyncio
    async def test_add_turn_merges_entities_across_turns(self, manager):
        """多轮累积 entity：product 和 order 都保留"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None

        step1 = _make_step(step=1, tool_name="search_product", observation="找到了")
        r1 = _make_loop_result(
            answer="拯救者Y9000P配置...",
            steps=[step1],
            last_entities={"product": "拯救者Y9000P"},
        )
        await session_manager.add_turn(ctx.session_id, owner_id, "拯救者配置", r1)

        r2 = _make_loop_result(
            answer="订单ORD001已发货",
            last_entities={"order": "ORD001"},
        )
        await session_manager.add_turn(ctx.session_id, owner_id, "ORD001到哪了", r2)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert ctx.last_entities["product"] == "拯救者Y9000P"
        assert ctx.last_entities["order"] == "ORD001"

    @pytest.mark.asyncio
    async def test_add_turn_overwrites_same_key_entity(self, manager):
        """同 key 的 entity 被新值覆盖"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None

        r1 = _make_loop_result(answer="a", last_entities={"product": "拯救者"})
        await session_manager.add_turn(ctx.session_id, owner_id, "q1", r1)

        r2 = _make_loop_result(answer="b", last_entities={"product": "ThinkPad"})
        await session_manager.add_turn(ctx.session_id, owner_id, "q2", r2)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert ctx.last_entities["product"] == "ThinkPad"

    @pytest.mark.asyncio
    async def test_add_turn_no_entities_does_not_clear_existing(self, manager):
        """新轮没有 entity 时，旧的保留"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        # 通过 add_turn 设置初始 entity
        r1 = _make_loop_result(answer="有货", last_entities={"product": "拯救者"})
        await session_manager.add_turn(ctx.session_id, owner_id, "查库存", r1)

        result = _make_loop_result(answer="不知道", last_entities={})
        await session_manager.add_turn(ctx.session_id, owner_id, "随便聊聊", result)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert ctx.last_entities["product"] == "拯救者"

    @pytest.mark.asyncio
    async def test_add_turn_updates_last_active(self, manager):
        session_manager, owner_id, _ = manager
        ctx1 = await session_manager.get_or_create(None, owner_id)
        assert ctx1 is not None
        old_active = ctx1.last_active
        result = _make_loop_result(answer="回答")
        await session_manager.add_turn(ctx1.session_id, owner_id, "查询", result)
        ctx2 = await session_manager.get(ctx1.session_id, owner_id)
        assert ctx2 is not None
        assert ctx2.last_active > old_active

    @pytest.mark.asyncio
    async def test_add_turn_step_no_tool_calls_is_skipped(self, manager):
        """steps 中某步没有 tool_calls 时不产生 assistant/tool 消息对"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        step = StepResult(step=1, thought="直接回答了", latency_ms=100.0)
        result = _make_loop_result(answer="最终答案", steps=[step])

        await session_manager.add_turn(ctx.session_id, owner_id, "问题", result)

        ctx = await session_manager.get(ctx.session_id, owner_id)
        assert ctx is not None
        assert len(ctx.messages) == 2

    # -- resolve ---------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_resolve_no_entities_returns_original(self, manager):
        """第一轮没有 entity，指代词不被替换"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        resolved = await session_manager.resolve("它的价格", ctx.session_id, owner_id)
        assert resolved == "它的价格"

    @pytest.mark.asyncio
    async def test_resolve_with_entity_replaces_pronoun(self, manager):
        """query 中的指代词被替换为实体名"""
        session_manager, owner_id, _ = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        # 通过 add_turn 写入 entity
        r1 = _make_loop_result(answer="配置...", last_entities={"product": "拯救者Y9000P"})
        await session_manager.add_turn(ctx.session_id, owner_id, "配置", r1)
        resolved = await session_manager.resolve("它的价格呢", ctx.session_id, owner_id)
        assert resolved == "拯救者Y9000P的价格呢"

    @pytest.mark.asyncio
    async def test_resolve_session_not_found(self, manager):
        """session 过期/不存在，返回原 query"""
        session_manager, owner_id, _ = manager
        resolved = await session_manager.resolve("这个有货吗", str(uuid.uuid4()), owner_id)
        assert resolved == "这个有货吗"

    @pytest.mark.asyncio
    async def test_resolve_session_id_none(self, manager):
        """新用户没传 session_id，返回原 query"""
        session_manager, owner_id, _ = manager
        resolved = await session_manager.resolve("这个有货吗", None, owner_id)
        assert resolved == "这个有货吗"

    # -- ownership, listing, deletion -----------------------------------------
    @pytest.mark.asyncio
    async def test_session_isolation_between_users(self, manager):
        session_manager, owner_id, other_owner_id = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        assert await session_manager.get(ctx.session_id, other_owner_id) is None
        assert await session_manager.delete(ctx.session_id, other_owner_id) is False

    @pytest.mark.asyncio
    async def test_list_sessions_only_returns_owner_sessions(self, manager):
        session_manager, owner_id, other_owner_id = manager
        owner_session = await session_manager.get_or_create(None, owner_id)
        other_session = await session_manager.get_or_create(None, other_owner_id)
        assert owner_session is not None
        assert other_session is not None

        sessions = await session_manager.list_sessions(owner_id)
        session_ids = {item["session_id"] for item in sessions}
        assert owner_session.session_id in session_ids
        assert other_session.session_id not in session_ids

    @pytest.mark.asyncio
    async def test_delete_session_only_for_owner(self, manager):
        session_manager, owner_id, other_owner_id = manager
        ctx = await session_manager.get_or_create(None, owner_id)
        assert ctx is not None
        assert await session_manager.delete(ctx.session_id, other_owner_id) is False
        assert await session_manager.delete(ctx.session_id, owner_id) is True
        assert await session_manager.get(ctx.session_id, owner_id) is None
