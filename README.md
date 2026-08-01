# 极客数码 — 3C 电商 AI 客服 Agent

一个完整的 AI 客服 Agent 系统，专为"极客数码"3C 电商场景设计。支持 RAG 检索问答、ReAct Agent 工具调用、Plan-and-Execute 复杂任务规划三种模式，通过意图路由器自动分发。

## 功能

- **意图路由** — 轻量 LLM 调用自动分类用户问题，分发到 RAG / Agent / Plan-Execute / 工单
- **RAG 检索** — Hybrid 检索 (语义 + BM25) → RRF 融合 → bge-reranker-v2-m3 精排
- **Agent 工具调用** — 手写 ReAct Loop，支持库存查询、订单追踪、商品对比、创建工单
- **Plan-and-Execute** — 配机组装（兼容性检查 + 预算分配）+ 设备故障诊断
- **SSE 流式输出** — LangGraph astream，token 级别实时推送
- **多轮对话** — Redis Session 管理 + 指代消解 + tiktoken 按轮次截断
- **评估体系** — 130 题测试集 + L1 意图分类准确率 + L2 Plan 结构校验

## 架构

```
用户请求
  → FastAPI /chat 或 /chat/stream
    → IntentRouter 意图分类
      ├─ rag          → Hybrid 检索 → Rerank → LLM 生成
      ├─ agent        → ReAct Loop (思考→调工具→观察→回答)
      ├─ plan_execute → Planner → Executor → Judge → Replanner → Formatter
      └─ ticket       → 自动创建工单
    → SessionManager 保存对话历史 (Redis)
```

## 快速开始

### 环境要求

- Python 3.12+
- PostgreSQL + pgvector
- Redis

### 安装

```bash
git clone https://github.com/fei8fenqian/ecommerce-cs-agent.git
cd ecommerce-cs-agent
pip install -e ".[dev]"
cp .env.example .env  # 编辑 .env 填入 API Key 和数据库密码
```

### 初始化数据

```bash
# 启动 PostgreSQL + Redis
docker compose up -d  # 如果有 docker-compose.yml
# 或者手动启动

# 灌入知识库和产品数据
make ingest
```

### 启动

```bash
python src/main.py
# 或
uvicorn src.main:app --reload
```

### 运行测试

```bash
make test       # 单元测试
make eval       # Agent 管线评估
make lint       # 代码检查
```

## 项目结构

```
src/
├── agent/          # Agent 引擎
│   ├── loop.py         # ReAct Agent Loop
│   ├── plan_execute.py # Plan-and-Execute Agent (LangGraph)
│   ├── session.py      # Redis 会话管理 + 上下文截断
│   ├── tools_registry.py  # 工具注册中心
│   ├── sentiment.py    # 情绪识别
│   ├── mcp_tool.py     # MCP 工具适配器
│   └── tools/          # 工具实现
│       ├── search_product.py   # 产品/知识库搜索
│       ├── search_component.py # 配件搜索
│       ├── track_order.py      # 订单追踪
│       ├── create_ticket.py    # 创建工单
│       ├── check_stock.py      # 库存查询
│       └── compare_products.py # 商品对比
├── core/           # 核心能力
│   ├── llm_client.py    # LLM 客户端 (OpenAI 兼容)
│   ├── intent_router.py # 意图路由器
│   ├── retrieve.py      # Hybrid 检索
│   ├── rerank.py        # 精排
│   ├── rrf.py           # RRF 融合
│   └── bm25.py          # BM25 关键词检索
├── api/            # FastAPI 路由
│   ├── chat.py      # /chat + /chat/stream
│   └── middleware.py # 安全中间件
├── config.py       # 全局配置 (pydantic-settings)
├── exceptions.py   # 异常体系
├── log_config.py   # 结构化 JSON 日志
└── main.py         # 应用入口

data/
├── knowledge/      # 知识库 (售后/选购/支付/故障排查)
├── products/       # 产品数据 (笔记本/手机/配件)
├── mock/           # 模拟数据 (订单/工单)
└── test_questions.json  # 130 题测试集

tests/              # 单元测试
scripts/            # 运维脚本
├── ingest/         # 数据灌入 pgvector
├── eval_agent.py   # Agent 评估
├── smoke/          # 冒烟测试
└── smoke_plan_execute.py
```

## 技术栈

| 组件 | 选型 |
|------|------|
| Web 框架 | FastAPI |
| Agent 编排 | 手写 ReAct + LangGraph StateGraph |
| LLM | DeepSeek (OpenAI 兼容 API) |
| 向量数据库 | pgvector |
| Embedding | BAAI/bge-large-zh-v1.5 (1024-dim) |
| 精排 | BAAI/bge-reranker-v2-m3 |
| 会话存储 | Redis (async, TTL 自动过期) |
| Token 计数 | tiktoken (cl100k_base) |
| 代码质量 | ruff + mypy + pytest |

## License

MIT
