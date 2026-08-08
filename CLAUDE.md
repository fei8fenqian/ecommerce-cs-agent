# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

"极客数码" 3C 电商 AI 客服 Agent 系统。异步 Python，OpenAI 兼容 API，手写 ReAct Agent Loop。

Tech: Python 3.12 / FastAPI / pgvector / AsyncOpenAI (DeepSeek) / pydantic-settings / ruff + mypy + pytest

## Import rule (critical)

`pyproject.toml` has `where = ["src"]`, so imports are **flat** — no `src.` prefix:

```python
from config import settings          # not: from src.config
from core.llm_client import LLMClient
from agent.tools_registry import ToolRegistry
from exceptions import LLMError
from log_config import get_request_id
```

The `src/logging.py` was renamed to `src/log_config.py` because it shadowed Python stdlib `logging`.

## Interaction Mode（学习模式）

fei8 处于**学习阶段**，不是工作效率阶段。核心原则：

**AI 不该做的事：**
- ❌ 不给架构思路、实现方案、设计建议
- ❌ 不给函数签名 + docstring + hint 的指导格式
- ❌ 不主动指出"你可以这样写"
- ❌ 不帮规划下一步做什么

**AI 该做的事：**
- ✅ 回答具体的知识点问题（"XXX 是什么"、"YYY 的原理"）
- ✅ 解释概念，但不延伸到"所以在你的项目里可以这样用"
- ✅ 跑 lint / test / 运维命令（纯体力活）
- ✅ 代码审查（写完后来问才看）

**原因**：fei8 发现之前"AI 给思路 → 自己写代码"的模式有问题——写代码退化为体力劳动，失去了独立思考和架构推导能力。学习阶段必须自己挣扎，效率不重要。

## Memory

项目记忆存在 `memory/` 目录（路径：`/home/fei8/.claude/projects/-home-fei8-ai-projects-E-Commerce-Agent/memory/`）。只记以下三类：

**1. 里程碑** — 做过哪些重要的事，不记细节（代码 git 里都有）
**2. 用户习惯** — fei8 的编码偏好、常用工具、喜欢什么样的工作方式
**3. 用户画像** — 技能水平、当前学习目标、短板

**不记：** 代码实现细节、git 能追溯的事情、一次性的配置改动。

## Commands

```
make install       # pip install -e ".[dev]"
make lint          # ruff check + mypy
make format        # ruff format
make test          # pytest -v
make eval          # python scripts/eval.py
make ingest        # 灌知识库 + 产品数据进 pgvector
make clean         # 清理缓存
```

Run a single file: `python scripts/smoke_llm.py`

## Architecture

```
user query
  → FastAPI route (coming)
    → AgentLoop.run(query, context=retrieved_docs)
      → LLMClient.chat(messages, tools)      # async, retry 3x, 401/403 no-retry
      → ToolRegistry.execute(name, **kwargs)  # sync tool calls
      → loop until answer or max_steps=5
    → return answer
```

### Layers (bottom-up)

1. **`config.py`** — `Settings` (pydantic-settings), reads `.env`. Global singleton `settings`.
2. **`exceptions.py`** — Hierarchy: `BaseAppException` → `ConfigError | RetrievalError | LLMError | ToolExecutionError | AgentLoopError`. `LLMError.can_retry` is False for 401/403.
3. **`log_config.py`** — Structured JSON logging + `contextvars` request_id injection.
4. **`core/llm_client.py`** — `LLMClient` wraps `AsyncOpenAI`. Returns `LLMResponse` (content + tool_calls + TokenUsage + latency). `LLMClient.__init__` takes all params explicitly (not reading settings internally) for testability.
5. **`agent/tools_registry.py`** — `BaseTool` (ABC), `ToolResult`, `ToolRegistry`. Tools define `name/description/parameters/execute`. Registry generates OpenAI function-calling schemas.
6. **`agent/loop.py`** — ReAct loop: LLM response → if tool_calls, execute tools → feed observations back → repeat until answer or max_steps. Safety: detects same-tool loops (configurable threshold, default 3).

### Key design decisions

- **Async everywhere**: FastAPI event loop handles multiple users; Agent Loop is serial per-user but IO waits (`await llm.chat`) release the event loop for other users.
- **OpenAI function calling format**: DeepSeek returns structured `tool_calls` — no regex-parsing of "Thought/Action/Observation" text needed.
- **`_parse_response`** converts raw API JSON → `LLMResponse`: extracts `choices[0].message.content`, `choices[0].message.tool_calls`, `response.usage`, `response.model`, `choice.finish_reason`. Tool argument strings are `json.loads()`-ed into dicts.
- **Tool arguments flow**: LLM returns JSON string → `json.loads()` → dict → `**kwargs` spread into `tool.execute(**kwargs)`.

### Project phases

| Phase | Status |
|-------|:------:|
| 1: 爬虫 + 知识库 (304 products, 5 docs) | ✅ |
| 2: 检索 pipeline + 评估 + 工程基础 | 🔜 |
| 3: Agent 引擎 (ReAct loop, tool calling) | ⬜ in progress |
| 4: 人工兜底 + 多轮对话 | ⬜ |
| 5: 中台能力 + 管理后台 | ⬜ |
| 6: 部署 + CI/CD + 文档 | ⬜ |

### Data

- `data/knowledge/` — 5 knowledge base markdown files (after_sales, laptop_guide, phone_guide, payment, trade_in)
- Products in pgvector (localhost:5433), embedding: `BAAI/bge-large-zh-v1.5` (1024-dim)
