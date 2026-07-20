# 3C 数码智能客服 Agent — 工程化迭代计划 V3

> **项目名称**：3c-cs-agent  
> **产品定位**：面向"极客数码"（中型 3C 电商）的 AI 客服中台  
> **核心原则**：不是 Demo，是按企业生产标准构建的 Agent 系统。架构可扩展，加工具只加子类不改核心。

---

## 当前进度（更新于 2026-07-19）

| Phase | 状态 | 关键产出 |
|-------|:---:|------|
| Phase 1: 爬虫 + 知识库 | ✅ | 笔记本 + 手机产品 / 5 篇知识库 / pgvector 入库 |
| Phase 2: 检索 + 评估 + 工程基础 | ✅ 80% | BM25/向量/RRF/Rerank 四方案消融 / 62 题评测集 / ruff+mypy/pre-commit/Makefile |
| Phase 3: Agent 引擎 | 🔜 50% | 异步 LLM 客户端 / ABC 工具注册中心 / ReAct Loop / RAG+LLM 端到端 / **缺：5 个工具 + 意图路由 + mock 数据** |
| Phase 4: 人工兜底 + 多轮对话 | ⬜ | 情绪检测 / 转人工工单 / 上下文记忆 / 指代消解 |
| Phase 5: 中台能力 + 管理后台 | ⬜ | /admin API / 模型网关 / Token 统计 / MCP 接入 |
| Phase 6: 部署 + CI/CD + 文档 | ⬜ | Docker Compose / GitHub Actions / README / 面试稿 |

---

## V3 相比 V2 的核心升级

| 维度 | V2 | V3 |
|------|----|----|
| 工具数量 | 2 个（stock / order） | **5 个，覆盖 5 种数据源模式**（见 3.5） |
| 工具注册 | 装饰器模式 | **ABC 抽象基类（预留 MCP 接入位）** |
| LLM 客户端 | Phase 3 占位 | **异步 + 指数退避重试 + Token 统计 + 结构化日志** |
| Agent 目标 | 跑通 ReAct | **展示企业级工具系统架构——加第 50 个工具只加一个子类** |
| 检索 | 三方案对比 | **四方案消融实验 + 62 题跨品类评测** |
| 产品品类 | 仅笔记本 | **笔记本 + 手机双品类** |

---

## 一、产品背景

### 1.1 企业画像

"极客数码"——中型 3C 电商，主营手机、笔记本、平板及配件。SKU 约 500 个，日均订单 2000+，客服团队 8 人。用户画像：25-35 岁男性为主，参数敏感，购买前会反复对比规格。

### 1.2 业务痛点

| 痛点 | 现状 | 影响 |
|------|------|------|
| 参数对比咨询多 | 用户反复问"这个和那个有什么区别"，客服手动查参数 | 人均 5-8 分钟/次 |
| 售后政策重复解答 | 70% 咨询是退货条件、保修范围、以旧换新规则 | 日均 150 条重复 |
| 库存/物流查询频繁 | 用户下单前必问"有货吗""多久到" | 手工查系统回复 |
| 复杂问题升级慢 | 投诉/大额退款没有自动工单生成 | 客服手动记录，易遗漏 |
| 多平台信息孤岛 | 商品数据在 DB、知识在 Wiki、订单在另一个系统 | 新人上手慢 |

### 1.3 核心指标

```
AI 自动解决率 ≥ 65%
客服人效提升 40%
首次响应 < 2s
月 Token 成本 ≤ ¥500（DeepSeek 为主力）
工具可扩展性：新增一个工具只需加一个 BaseTool 子类
```

---

## 二、功能全景

| 模块 | 功能 | 优先级 | 依赖 |
|------|------|:---:|------|
| 知识问答 | 参数查询、选购指南、售后政策 | P0 | RAG + LLM |
| 商品对比 | 多产品参数自动对比 | P0 | RAG（多文档检索）+ LLM |
| 库存查询 | 查商品库存状态和价格 | P1 | Tool Calling → check_stock |
| 订单追踪 | 查订单物流状态 | P1 | Tool Calling → track_order |
| 人工转接 | 生成工单，升级给人工 | P1 | Tool Calling → create_ticket |
| 多轮对话 | session 内上下文记忆 + 指代消解 | P0 | Agent Loop + 会话管理 |
| 意图路由 | 自动分类 query，走不同处理链路 | P0 | intent_router |
| 管理后台 | 知识库管理、模型配置、Token 统计 | P2 | /admin API |

### 行为边界

```
❌ 不编造参数（必须从知识库/数据库取）
❌ 不承诺库存（必须调 Tool 查数据）
❌ 不报价（价格从数据库取，LLM 不自行定价）
❌ 不执行付款/退款（只提供信息，不操作资金）
❌ 不说"最好""绝对""第一"（广告法合规）
❌ 被骂不骂回去（情绪检测 → 转人工）
```

---

## 三、技术架构

### 3.1 架构图

```
                          用户（Web Chat / Swagger）
                                │
                                ▼
                      ┌──────────────────┐
                      │   FastAPI 网关     │
                      │   结构化日志/限流   │
                      └────────┬─────────┘
                                │
                      ┌─────────▼──────────┐
                      │     意图路由        │
                      │   LLM 分类 query    │
                      │  参数/对比/库存/     │
                      │  订单/售后/闲聊     │
                      └──┬────┬─────┬──────┘
                         │    │     │
            ┌────────────┼────┼─────┼────────────┐
            │            │    │     │            │
            ▼            ▼    ▼     ▼            ▼
      ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
      │ RAG 引擎 │ │ Agent    │ │ 对比引擎 │ │ 人工路由 │
      │ 参数查询 │ │ Loop     │ │ 多产品   │ │ 工单生成 │
      │ 政策查询 │ │          │ │ 参数对比 │ │ 升级转接 │
      │ 选购建议 │ │ ┌──────┐ │ │          │ │          │
      └────┬─────┘ │ │工具集 │ │ └────┬─────┘ └────┬─────┘
           │       │ ├──────┤ │      │            │
           │       │ │stock  │ │      │            │
           ▼       │ │order  │ │      ▼            ▼
      ┌─────────┐  │ │ticket │ │  ┌─────────────────────────┐
      │检索模块  │  │ │search │ │  │      数据层              │
      │BM25     │  │ │compare│ │  │ PostgreSQL + pgvector    │
      │向量     │  │ └──────┘ │  │ (知识库+商品+订单+工单)   │
      │RRF      │  └────┬─────┘  │ images/                  │
      │Rerank   │       │        └─────────────────────────┘
      └─────────┘       │
                        ▼
                  ┌───────────┐
                  │  模型层    │
                  │ DeepSeek  │
                  │ bge-large │
                  └───────────┘
```

### 3.2 技术栈

| 层 | 技术 | 说明 |
|---|------|------|
| 后端框架 | FastAPI + Python 3.12 | 异步 SSE，lifespan 管理资源 |
| LLM 客户端 | AsyncOpenAI（兼容所有 OpenAI 格式 API） | 指数退避重试 + Token 统计 + 结构化日志 |
| LLM 模型 | DeepSeek Chat | 性价比首选，也可换通义千问/GLM 等 |
| Embedding | bge-large-zh-v1.5 | 1024 维，离线可用 |
| 向量库 | pgvector (PostgreSQL) | 向量 + 元数据混合查询，统一存储 |
| 检索 | BM25 + 向量 RRF + Rerank | 四方案消融实验，最终选纯向量 93ms |
| Agent | 手写 ReAct → 后续 LangGraph | ABC 工具注册中心 + OpenAI function calling |
| 工具注册 | BaseTool(ABC) - 预留 MCP 接入 | 新增工具只加子类，不改 Registry 和 Loop |
| 爬虫 | Playwright | 过 JS Challenge |
| 部署 | Docker Compose | FastAPI + PostgreSQL 编排 |

### 3.3 模型分层

| 场景 | 模型 | 原因 |
|------|------|------|
| 参数查询/选购建议 | DeepSeek | 确定性高，便宜够用 |
| 意图分类 | DeepSeek | 轻量分类，一次 LLM 调用 |
| 商品对比/工具调用 | DeepSeek | Function calling 稳定 |
| 情绪检测 | DeepSeek | V1 够用，V2 可上更强模型兜底 |
| Embedding | bge-large 本地 | 不花 API 钱 |

### 3.4 数据分布

```
PostgreSQL + pgvector（统一数据层）：
  ├── knowledge_chunks     → 选购指南/售后政策 chunks + 向量（~80 条）
  ├── laptop_products      → 笔记本规格+向量+价格+status（~200 条）
  ├── phone_products       → 手机规格+向量+价格+status（~100 条）
  ├── orders               → 模拟订单数据（10-20 条）
  └── tickets              → 工单记录（Agent 转人工时写入）

静态文件：
  └── images/laptops/      → 产品主图，FileResponse 返回
```

### 3.5 工具矩阵（5 种数据源模式）

**设计理念**：不是凑工具数量。5 个工具覆盖 5 种不同的数据源和执行模式。面试官看到架构的可扩展性——新增第 6 个工具（比如发优惠券），只需新增一个 BaseTool 子类，不改 Registry 和 Loop。

| # | 工具名 | 数据来源 | 模式 | 面试展示点 |
|---|--------|----------|------|-----------|
| 1 | `search_product` | pgvector 向量检索 | **检索型** — 包一层 hybrid_search | RAG 也能作为 Tool 被 Agent 调用 |
| 2 | `check_stock` | PostgreSQL `laptop_products.status` | **DB 查询型** — SQL 查询 | 最典型的生产工具模式 |
| 3 | `track_order` | `data/mock/orders.json` | **外部 API 型** — 读文件模拟 API 调用 | 模拟微服务调用，可换 MCP |
| 4 | `create_ticket` | `data/mock/tickets.json`（写入） | **写操作型** — 生成工单 | 有副作用的工具，需要日志审计 |
| 5 | `compare_products` | 2 次 search_product + LLM | **组合型** — 调其他工具 + LLM 推理 | 展示工具间协作，不是孤立调用 |

**所有工具共享的共同行为**（ABC 的价值）：
- `to_openai_function()` — 统一生成 OpenAI function calling 格式
- 参数校验 — JSON Schema 定义，LLM 自动遵守
- 异常 → `ToolResult(error)` — 不需要每个子类写 try/except（Registry.execute 统一兜底）

### 3.6 MCP 扩展预留（Phase 5+）

当前 5 个工具是本地 Python 实现。当系统对接真实后端（ERP、WMS、支付网关），每个系统暴露 MCP Server，Agent 通过 `MCPTool(BaseTool)` 适配器接入，注册进同一个 Registry。Agent Loop 完全无感——它只管调 `registry.execute()`，不关心工具是本地的还是远程的。

---

## 四、迭代计划

### Phase 1：爬虫 + 知识库（Day 1-3）✅ 已完成

- 5 篇知识库 + 笔记本产品 + 手机产品
- pgvector 入库 + HNSW 索引
- 28 个标准字段归一化

### Phase 2：检索 + 评估 + 工程基础（Day 4-5）✅ 80%

**已完成：**
- `pyproject.toml` + `Makefile` + `.pre-commit-config.yaml`
- `src/config.py` — pydantic-settings
- `src/log_config.py` — JSON 结构化日志（改名避免 stdlib 冲突）
- `src/exceptions.py` — 异常继承体系
- `src/core/bm25.py` — BM25 关键词检索
- `src/core/rrf.py` — RRF 融合
- `src/core/rerank.py` — Cross-encoder 精排
- `src/core/retrieve.py` — 向量检索 + 混合检索
- `scripts/eval.py` — 四方案消融实验（62 题）
- `tests/test_rrf.py` — RRF 单元测试

**评估结果：**

| 方案 | Hit@1 | Hit@5 | MRR | 延迟 |
|------|:---:|:---:|:---:|:---:|
| A: BM25 | 61.3% | 83.9% | 0.708 | 410ms |
| B: 纯向量 | 85.5% | 96.8% | 0.902 | 93ms |
| C: RRF 混合 | 83.9% | 96.8% | 0.891 | 81ms |
| D: +Rerank | 95.2% | 96.8% | 0.957 | 3291ms |

**结论**：纯向量方案 B 为默认策略（93ms，性价比最优）。Hard 题启用 Rerank（85% Hit@1）。

**待补：**
- [ ] `tests/test_retrieve.py` — 检索单元测试
- [ ] `tests/test_tools_registry.py` — 工具注册单元测试
- [ ] FastAPI 入口 + lifespan（Phase 5 做）

### Phase 3：Agent 引擎（Day 6-9）🔜 当前

**目标**：用户问题 → 意图路由 → RAG 或 Agent Loop → 回答。5 个工具全部可调。

**3.1 Agent 基础架构（✅ 完成）**

- [x] `src/agent/tools_registry.py` — 工具注册中心（ABC 模式，预留 MCP）
- [x] `src/core/llm_client.py` — 异步 LLM 客户端（指数退避重试 + Token 统计）
- [x] `src/agent/loop.py` — ReAct 循环引擎（OpenAI function calling）
- [x] `scripts/smoke_rag.py` — RAG+LLM 端到端验证（5 场景全过）

**3.2 Mock 数据（Day 6）**

- [ ] `laptop_products` / `phone_products` 表加 `status` 字段（"在售"/"已下架"）
- [ ] `data/mock/orders.json` — 20 条模拟订单（含不同物流状态）
- [ ] `data/mock/tickets.json` — 空数组起步，create_ticket 写入

**3.3 5 个业务工具（Day 6-7）**

- [ ] `src/agent/tools/search_product.py` — SearchProduct(BaseTool)：检索型
- [ ] `src/agent/tools/check_stock.py` — CheckStock(BaseTool)：DB 查询型
- [ ] `src/agent/tools/track_order.py` — TrackOrder(BaseTool)：JSON/API 型
- [ ] `src/agent/tools/create_ticket.py` — CreateTicket(BaseTool)：写操作型
- [ ] `src/agent/tools/compare_products.py` — CompareProducts(BaseTool)：组合型

**3.4 意图路由（Day 7-8）**

- [ ] `src/core/intent_router.py` — IntentRouter
  - 轻量 LLM 调用做意图分类
  - 路由表：参数查询→RAG / 政策→RAG / 库存→Agent / 订单→Agent / 对比→CompareProducts / 投诉→CreateTicket
  - 输出：`Intent(target="rag", table="laptop_products", query=...)`

**3.5 端到端集成（Day 8-9）**

- [ ] `scripts/smoke_agent.py` — 全链路冒烟测试
  - 场景 1：精确参数查询 → RAG → 回答
  - 场景 2：库存查询 → Agent Loop → check_stock → 回答
  - 场景 3：订单追踪 → Agent Loop → track_order → 回答
  - 场景 4：商品对比 → Agent Loop → compare_products → 回答
  - 场景 5：投诉升级 → Agent Loop → create_ticket → 回答
  - 场景 6：多轮对话（指代消解）→ session 上下文 → 回答

**3.6 单元测试（跟着代码写）**

- [ ] `tests/test_tools_registry.py`
- [ ] `tests/test_intent_router.py`
- [ ] `tests/test_tools/` — 每个工具的功能测试

### Phase 4：人工兜底 + 多轮对话（Day 10-11）

- [ ] 情绪检测：负面词/连续否定/投诉词 → 触发 create_ticket
- [ ] 转人工逻辑：生成对话摘要 + 已尝试方案 → 写入工单
- [ ] `src/session.py` — 会话上下文管理（当前产品名、订单号、意图）
- [ ] 指代消解："这个有黑色的吗" → 指代上文提及的产品
- [ ] `tests/test_session.py`
- [ ] Conventional Commits 严格执行

### Phase 5：中台能力 + 管理后台（Day 12-13）

- [ ] `src/main.py` — FastAPI 入口 + lifespan 资源管理
- [ ] `src/middleware.py` — 请求计时 + CORS + request_id 注入
- [ ] `/chat` / `/chat/stream` — 对话接口（SSE 流式）
- [ ] `/admin` API：
  - 知识库管理（CRUD）
  - 模型列表 + Token 统计
  - 工具调用统计
- [ ] 模型网关：按任务选模型
- [ ] `src/core/model_gateway.py` — 模型路由 + Token 记账
- [ ] MCP 接入（如时间允许）：`MCPTool(BaseTool)` 适配器 + 一个 MCP Server 示例
- [ ] 商品图片端点：`GET /product/{id}/image`

### Phase 6：部署 + CI/CD + 文档（Day 14-15）

- [ ] `.github/workflows/ci.yml` — push 自动 ruff + pytest + eval（5 题抽查）
- [ ] `.github/workflows/eval-nightly.yml` — 凌晨完整评估 + 历史存档
- [ ] Docker Compose — FastAPI + PostgreSQL 编排
- [ ] Dockerfile — 多阶段构建
- [ ] 健康检查：PG + Embedding + LLM 可达性
- [ ] `CONTRIBUTING.md` + `PULL_REQUEST_TEMPLATE.md` + `CHANGELOG.md`
- [ ] README：架构图 + CI badge + 评估数据 + 快速开始
- [ ] 面试讲述结构（30s / 2min / 踩坑）

---

## 五、企业级实践

### 5.1 资源生命周期 → FastAPI Lifespan

```
❌ Demo：_model = SentenceTransformer() 模块顶层
✅ 企业：FastAPI lifespan 管加载，Depends() 注入

面试必问："你的模型怎么加载和管理的？"
```

### 5.2 结构化日志

```
每行 JSON：request_id / latency_ms / model / tokens / finish_reason
出问题时一个 request_id 串起所有日志
```

### 5.3 异步不阻塞

```
CPU 密集（Embedding）→ asyncio.to_thread()
IO 密集（LLM API）→ AsyncOpenAI + await
FastAPI 单线程异步，多用户并发不互相阻塞
```

### 5.4 异常继承体系

```
BaseAppException
├── RetrievalError（可恢复/不可恢复）
├── LLMError（can_retry / retry_count）
├── ToolExecutionError（tool_name + original_error）
├── AgentLoopError（step_count + reason）
└── ConfigError
全局 exception handler 统一响应格式
```

### 5.5 工具可扩展性（面试核心亮点）

```
新增一个工具：
  1. 写一个 BaseTool 子类（30 行）
  2. registry.register(MyTool())
  3. 完成。不改 Registry、Loop、Router。

工具注册中心是最简单的设计模式面试题：
  "如果让你设计一个可扩展的 Agent 工具系统，你怎么做？"
  → Registry Pattern + ABC + MCP-ready
```

### 5.6 健康检查

```
/health → PG 连通性 + Embedding 模型状态 + LLM API 可达性
返回 {"status": "healthy/degraded", "components": {...}}
```

---

## 六、关键决策记录

### 6.1 为什么选 3C 数码

- 参数密集，RAG 检索精度要求高 → 面试能讲检索优化
- 对比类问题是天然 Agent 多步推理场景
- 公开数据（中关村在线），不需要编造
- 自己就是 3C 用户，理解用户行为

### 6.2 为什么先手写再上 LangGraph

- 手写理解 ReAct 每一步：状态管理、工具调度、错误恢复
- 踩完坑才能理解 LangGraph 的 StateGraph 设计动机
- 面试能讲"手写 vs 框架"的对比和选择理由

### 6.3 为什么用 pgvector 不是 ChromaDB

- 向量 + 元数据一次 SQL：`WHERE brand='联想' ORDER BY vec <=> query`
- 统一存储引擎：PostgreSQL 替代 ChromaDB + JSON 文件
- ACID 事务、主从复制、备份恢复都是成熟方案

### 6.4 为什么工具注册用 ABC 不是装饰器

- 5 个工具覆盖 5 种数据源模式 → 共享 `to_openai_function()` / 参数校验 / 异常兜底
- ABC 模式天然对 MCP 友好：`MCPTool(BaseTool)` 一个适配器搞定
- 面试展示"面向接口编程"的工程思维
- 之前的 agent-playground 项目用的就是 ABC，这次延续并强化

### 6.5 为什么不做 Multi-Agent

- 单 Agent 多 Tool 覆盖当前场景
- 用户量小，Multi-Agent 协调开销 > 并行收益
- 架构预留模块扩展位，需要时再拆分

### 6.6 为什么一个人也要做 CI/CD + PR Template + pre-commit

- GitHub 仓库面貌是面试官判断团队经验的直觉依据
- 成本极低：Makefile 10 行 + pre-commit 15 行 + CI yaml 30 行
- 本质是给自己做 CI——pre-commit 在 commit 之前拦住你

### 6.7 为什么预留 MCP 但不现在做

- 当前 5 个工具本地实现足够，MCP 解决的是多系统集成问题
- `BaseTool` 的接口和 MCP Tool schema 天然对齐，接入成本低
- 面试能讲"架构预留 MCP 接入能力，关注行业标准"
- Phase 5 时间允许的话做一个 MCP Server 示例

### 6.8 评估结果：为什么选纯向量而不是 Rerank

- 纯向量 Hit@1 85.5% / 93ms；Rerank 95.2% / 3291ms
- 35 倍延迟换 10 个百分点，客服场景不划算
- 策略：默认纯向量，Hard 题按需 Rerank（后续可加意图判断）

---

## 七、风险与降级

| 风险 | 影响 | 降级方案 |
|------|------|---------|
| pgvector 挂了 | 检索不可用 | 降级 JSON 关键词搜索 |
| DeepSeek API 挂了 | 无法生成回答 | 返回检索文档原文 |
| Agent 死循环 | Token 暴涨 | max_steps=5 + 同 Tool 连续 3 次检测 |
| 工具执行失败 | 回答不完整 | ToolResult(error) → LLM 看到错误后自行降级 |
| Prompt 注入 | 模型越权 | 输入长度限制 + 输出校验 |
| 图片下载失败 | 产品图缺失 | 占位图路径，不阻塞主流程 |

---

## 八、成功指标

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| 知识库 Hit@1 | ≥ 85% | eval.py 62 题 |
| 知识库 MRR | ≥ 0.85 | eval.py |
| 意图分类准确率 | ≥ 90% | intent_router 测试集 |
| 工具调用成功率 | ≥ 95% | Tool 日志 |
| Token/次 | ≤ 3000 | 结构化日志 |
| 响应延迟 | ≤ 5s 非流式 | timing 日志 |
| 图片覆盖率 | ≥ 90% | products 表检查 |
| 测试覆盖率 | ≥ 70% | pytest --cov |

---

## 九、目录结构

```
3c-cs-agent/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml
│   │   └── eval-nightly.yml
│   └── PULL_REQUEST_TEMPLATE.md
├── src/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 入口 + lifespan（Phase 5）
│   ├── config.py                  # pydantic-settings
│   ├── log_config.py              # JSON 结构化日志
│   ├── middleware.py              # 请求计时 + CORS（Phase 5）
│   ├── exceptions.py              # 异常继承体系
│   ├── session.py                 # 多轮会话管理（Phase 4）
│   ├── api/
│   │   ├── __init__.py
│   │   ├── chat.py                # /chat /chat/stream（Phase 5）
│   │   └── admin.py               # /admin/*（Phase 5）
│   ├── core/
│   │   ├── __init__.py
│   │   ├── retrieve.py            # 多库检索引擎
│   │   ├── bm25.py                # BM25 关键词检索
│   │   ├── rrf.py                 # RRF 融合
│   │   ├── rerank.py              # Cross-encoder 精排
│   │   ├── llm_client.py          # 异步 LLM 客户端
│   │   ├── intent_router.py       # 意图分类+路由（Phase 3）
│   │   ├── model_gateway.py       # 模型路由+Token统计（Phase 5）
│   │   ├── generate.py            # LLM 生成（Phase 3）
│   │   └── ingest.py              # 文档摄入（Phase 5）
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── loop.py                # ReAct 循环引擎
│   │   ├── tools_registry.py      # ABC 工具注册中心
│   │   ├── human_loop.py          # 人工审批/转接（Phase 4）
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── search_product.py  # 检索型工具
│   │       ├── check_stock.py     # DB 查询型工具
│   │       ├── track_order.py     # API/JSON 型工具
│   │       ├── create_ticket.py   # 写操作型工具
│   │       └── compare_products.py # 组合型工具
│   └── modules/                    # 业务模块扩展位
│       ├── __init__.py
│       ├── customer_service/
│       ├── product_compare/
│       └── content_gen/
├── tests/
│   ├── __init__.py
│   ├── test_rrf.py
│   ├── test_tools_registry.py
│   ├── test_intent_router.py
│   ├── test_retrieve.py
│   ├── test_bm25.py
│   └── test_loop.py
├── data/
│   ├── knowledge/                  # 5 篇知识库源文档
│   ├── products/                   # 商品数据（爬虫产出）
│   │   ├── raw/
│   │   └── normalized/
│   ├── images/                     # 产品图片
│   │   └── laptops/
│   ├── mock/                       # 模拟数据
│   │   ├── orders.json
│   │   └── tickets.json
│   └── test_questions.json         # 62 题评测集
├── scripts/
│   ├── crawl_test.py
│   ├── clean_products.py
│   ├── ingest_knowledge.py
│   ├── ingest_pgvector.py
│   ├── verify_retrieval.py
│   ├── eval.py
│   ├── smoke_llm.py
│   ├── smoke_rag.py
│   └── smoke_agent.py              # 全链路冒烟（Phase 3 收尾）
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── pyproject.toml
├── Makefile
├── CONTRIBUTING.md
├── CHANGELOG.md
├── README.md
├── PLAN_V2.md                     # 上版本计划（留档）
└── PLAN_V3.md                     # 本文件
```

---

## 十、面试讲述结构

### 30 秒：这是什么

> "我做了一个面向中型 3C 电商的 AI 客服中台。用真实爬取的笔记本和手机数据搭建了 pgvector RAG + ReAct Agent。5 个工具覆盖 5 种数据源模式，工具注册中心用 ABC 抽象基类预留了 MCP 接入位。整个项目按企业标准维护——异步架构、结构化日志、异常继承体系、CI/CD、Conventional Commits。"

### 2 分钟：怎么做的

> "数据层：Playwright 爬中关村在线，pgvector 统一存储向量和元数据，四方案消融实验验证检索效果。Agent 引擎：手写 ReAct Loop + OpenAI function calling，工具注册用 ABC 模式——5 个工具覆盖检索、DB 查询、API 调用、写操作、组合调用五种模式。新增工具只需写一个子类。LLM 客户端异步架构，指数退避重试，结构化日志记录每次调用的 Token 消耗和延迟。意图路由自动分类用户 query，决定走 RAG 还是 Agent Loop。"

### 核心技术亮点

> "一是用 pgvector 做统一数据层的架构决策。二是工具系统的 ABC 设计——面向接口编程，预留 MCP 接入，新增工具零改动核心代码。三是手写 ReAct 理解 Agent 状态管理的本质。四是四方案消融实验让检索效果有数据支撑。五是整个项目的工程化程度——CI/CD、pre-commit、结构化日志、异常体系、Conventional Commits，虽然是 solo project，但项目面貌是团队级的。"

### 踩过的坑

> "爬虫过 JS Challenge。pgvector 向量传参格式。logging.py 和标准库重名。Agent 死循环——LLM 反复调同一个工具，加了连续 3 次同名检测。工具注册从装饰器改 ABC——开始觉得装饰器简单，后来工具多了发现需要共享行为（参数校验、异常兜底、to_openai_function），ABC 的面向接口编程才是对的。"
