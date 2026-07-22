"""tests/test_loop.py — Agent Loop 单元测试"""

import pytest

from agent.loop import AgentLoop, LoopResult, StepResult
from agent.tools_registry import BaseTool, ToolRegistry, ToolResult
from core.llm_client import LLMResponse, TokenUsage, ToolCall


# =============================================================================
# Mock — 按顺序返回预设响应的假 LLM
# =============================================================================
class _SequentialMockLLM:
    """按调用顺序依次返回预设的 LLMResponse，超出则返回默认回答"""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = responses
        self._call_count = 0
        self.model = "mock"

    async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
        self._call_count += 1
        if self._call_count <= len(self._responses):
            return self._responses[self._call_count - 1]
        return LLMResponse(
            content="默认兜底回答",
            model="mock",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            finish_reason="stop",
        )


# =============================================================================
# 假工具
# =============================================================================
class _EchoTool(BaseTool):
    """回显参数的工具"""

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回显输入参数"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, **kwargs):
        return ToolResult(name=self.name, status="success", data={"echo": kwargs.get("text", "")})


class _FailingTool(BaseTool):
    """总是失败的工具 — 用于测试死循环检测"""

    @property
    def name(self) -> str:
        return "failing"

    @property
    def description(self) -> str:
        return "永远失败"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    def execute(self, **kwargs):
        return ToolResult(name=self.name, status="success", data={"result": "done"})


# =============================================================================
# 辅助函数
# =============================================================================
def _tool_call(name: str, id: str = "call_1", **kwargs) -> ToolCall:
    """快速构建 ToolCall"""
    return ToolCall(id=id, name=name, arguments=kwargs)


def _make_registry(*tools: BaseTool) -> ToolRegistry:
    r = ToolRegistry()
    for t in tools:
        r.register(t)
    return r


# =============================================================================
# LoopResult / StepResult 数据类
# =============================================================================
class TestLoopResult:
    def test_defaults(self):
        r = LoopResult()
        assert r.answer == ""
        assert r.steps == []
        assert r.total_steps == 0

    def test_full(self):
        steps = [StepResult(step=1, thought="thinking")]
        r = LoopResult(
            answer="答案", steps=steps, total_steps=1, total_tokens=100, total_latency_ms=500.0
        )
        assert r.answer == "答案"
        assert r.total_steps == 1
        assert r.total_tokens == 100
        assert r.total_latency_ms == 500.0


class TestStepResult:
    def test_defaults(self):
        s = StepResult(step=1)
        assert s.step == 1
        assert s.thought is None
        assert s.tool_calls is None

    def test_with_tool_call(self):
        tc = _tool_call("echo", text="hello")
        s = StepResult(
            step=2, thought="先查一下", tool_calls=[tc], observation="结果", latency_ms=100.0
        )
        assert s.tool_calls is not None
        assert s.tool_calls[0].name == "echo"
        assert s.observation == "结果"


# =============================================================================
# AgentLoop.run — 正常场景
# =============================================================================
class TestAgentLoopRun:
    @pytest.mark.asyncio
    async def test_no_tools_direct_answer(self):
        """LLM 不调工具，直接返回答案 → 1 步结束"""
        llm = _SequentialMockLLM(
            [
                LLMResponse(
                    content="您好，有什么可以帮您？",
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="stop",
                ),
            ]
        )
        loop = AgentLoop(llm=llm, registry=ToolRegistry())
        result = await loop.run("你好")

        assert result.answer == "您好，有什么可以帮您？"
        assert result.total_steps == 1
        assert len(result.steps) == 1
        assert result.steps[0].thought == "您好，有什么可以帮您？"

    @pytest.mark.asyncio
    async def test_single_tool_then_answer(self):
        """LLM 调一次工具 → 拿到 observation → 再答"""
        llm = _SequentialMockLLM(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("echo", text="hello world")],
                    model="mock",
                    usage=TokenUsage(total_tokens=50),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="已处理完毕",
                    model="mock",
                    usage=TokenUsage(total_tokens=30),
                    finish_reason="stop",
                ),
            ]
        )
        registry = _make_registry(_EchoTool())
        loop = AgentLoop(llm=llm, registry=registry)
        result = await loop.run("请回声 hello world")

        assert result.answer == "已处理完毕"
        assert result.total_steps == 2
        # 第一步有 tool_calls
        assert result.steps[0].tool_calls is not None
        assert result.steps[0].tool_calls[0].name == "echo"
        # observation 包含工具结果
        assert "hello world" in result.steps[0].observation

    @pytest.mark.asyncio
    async def test_context_injection(self):
        """有 context 时拼入 user message"""
        captured_messages: list = []

        class _CaptureLLM:
            model = "mock"

            async def chat(self, messages, *, tools=None, temperature=0.0, max_tokens=2048):
                captured_messages.extend(messages)
                return LLMResponse(
                    content="收到", model="mock", usage=TokenUsage(), finish_reason="stop"
                )

        loop = AgentLoop(llm=_CaptureLLM(), registry=ToolRegistry())
        await loop.run("推荐笔记本", context="参考: ThinkPad X1")

        user_msg = [m for m in captured_messages if m["role"] == "user"][0]
        assert "参考: ThinkPad X1" in user_msg["content"]
        assert "推荐笔记本" in user_msg["content"]


# =============================================================================
# AgentLoop.run — 边界 / 兜底
# =============================================================================
class TestAgentLoopEdgeCases:
    @pytest.mark.asyncio
    async def test_max_steps_fallback(self):
        """LLM 一直调工具不回答，max_steps 用完 → 触发兜底 prompt"""
        # 只返回 tool_calls，永远不会 content
        tool_responses = [
            LLMResponse(
                content=None,
                tool_calls=[_tool_call("echo", id=f"call_{i}", text=str(i))],
                model="mock",
                usage=TokenUsage(),
                finish_reason="tool_calls",
            )
            for i in range(3)  # max_steps=3
        ]
        llm = _SequentialMockLLM(tool_responses)
        registry = _make_registry(_EchoTool())
        loop = AgentLoop(llm=llm, registry=registry, max_steps=3)
        result = await loop.run("测试")

        # 用完 max_steps，触发兜底 prompt 拿到默认回答
        assert result.answer == "默认兜底回答"
        assert result.total_steps == 3

    @pytest.mark.asyncio
    async def test_fallback_prompt_on_no_answer(self):
        """max_steps 内调了工具但没来得及回答 → 最后一步是兜底 prompt"""
        llm = _SequentialMockLLM(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("echo", text="x")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                # 第二步 LLM 没调用工具也没给内容
                LLMResponse(
                    content=None,
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="stop",
                ),
            ]
        )
        registry = _make_registry(_EchoTool())
        loop = AgentLoop(llm=llm, registry=registry, max_steps=2)
        result = await loop.run("查询")

        assert result.answer == "默认兜底回答"
        # 2 步 tool + 1 次兜底 = 3 次 LLM 调用
        assert llm._call_count == 3


# =============================================================================
# AgentLoop.run — 死循环检测
# =============================================================================
class TestAgentLoopDeadloop:
    @pytest.mark.asyncio
    async def test_same_tool_five_times_raises(self):
        """同一工具连续 5 次 → AgentLoopError"""
        from exceptions import AgentLoopError

        tool_responses = [
            LLMResponse(
                content=None,
                tool_calls=[_tool_call("failing", id=f"call_{i}")],
                model="mock",
                usage=TokenUsage(),
                finish_reason="tool_calls",
            )
            for i in range(6)  # 够触发 5 次阈值
        ]
        llm = _SequentialMockLLM(tool_responses)
        registry = _make_registry(_FailingTool())
        loop = AgentLoop(llm=llm, registry=registry, max_steps=10)

        with pytest.raises(AgentLoopError, match="疑似死循环"):
            await loop.run("无限循环测试")

    @pytest.mark.asyncio
    async def test_different_tools_no_deadloop(self):
        """交替调用不同工具不应触发死循环检测"""
        llm = _SequentialMockLLM(
            [
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("echo", id="c1", text="a")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("failing", id="c2")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("echo", id="c3", text="b")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("failing", id="c4")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content=None,
                    tool_calls=[_tool_call("echo", id="c5", text="c")],
                    model="mock",
                    usage=TokenUsage(),
                    finish_reason="tool_calls",
                ),
                LLMResponse(
                    content="交替完成", model="mock", usage=TokenUsage(), finish_reason="stop"
                ),
            ]
        )
        registry = _make_registry(_EchoTool(), _FailingTool())
        loop = AgentLoop(llm=llm, registry=registry, max_steps=10)

        result = await loop.run("交替调用测试")
        assert result.answer == "交替完成"


# =============================================================================
# AgentLoop._assistant_message
# =============================================================================
class TestAssistantMessage:
    def test_builds_correct_format(self):
        loop = AgentLoop(llm=_SequentialMockLLM([]), registry=ToolRegistry())
        msg = loop._assistant_message(
            [
                ToolCall(id="abc", name="search", arguments={"q": "test"}),
            ]
        )

        assert msg["role"] == "assistant"
        assert msg["content"] is None
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["id"] == "abc"
        assert msg["tool_calls"][0]["type"] == "function"
        assert msg["tool_calls"][0]["function"]["name"] == "search"

    def test_multiple_tool_calls(self):
        loop = AgentLoop(llm=_SequentialMockLLM([]), registry=ToolRegistry())
        msg = loop._assistant_message(
            [
                ToolCall(id="c1", name="t1", arguments={}),
                ToolCall(id="c2", name="t2", arguments={}),
            ]
        )

        assert len(msg["tool_calls"]) == 2
        assert msg["tool_calls"][0]["id"] == "c1"
        assert msg["tool_calls"][1]["id"] == "c2"

    def test_arguments_serialized_as_json(self):
        import json

        loop = AgentLoop(llm=_SequentialMockLLM([]), registry=ToolRegistry())
        msg = loop._assistant_message(
            [
                ToolCall(id="x", name="echo", arguments={"text": "你好", "count": 3}),
            ]
        )

        args_str = msg["tool_calls"][0]["function"]["arguments"]
        parsed = json.loads(args_str)
        assert parsed["text"] == "你好"
        assert parsed["count"] == 3


# =============================================================================
# AgentLoop.__init__
# =============================================================================
class TestAgentLoopInit:
    def test_custom_system_prompt(self):
        llm = _SequentialMockLLM([])
        loop = AgentLoop(llm=llm, registry=ToolRegistry(), system_prompt="自定义 prompt")
        assert loop.system_prompt == "自定义 prompt"

    def test_default_system_prompt(self):
        llm = _SequentialMockLLM([])
        loop = AgentLoop(llm=llm, registry=ToolRegistry())
        assert "极客数码" in loop.system_prompt

    def test_custom_max_steps(self):
        llm = _SequentialMockLLM([])
        loop = AgentLoop(llm=llm, registry=ToolRegistry(), max_steps=8)
        assert loop.max_steps == 8
