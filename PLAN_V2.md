# 3C 数码智能客服 Agent — 工程化迭代计划

> **项目名称**：3c-cs-agent（独立新建项目）  
> **产品定位**：面向 3C 数码电商的 AI 客服中台  
> **核心原则**：不是 Demo，是按企业生产标准构建的 Agent 系统。从真实数据出发，撞到真实瓶颈，做出真实效果。

---

## 前置项目参考

本项目参考 [rag-api](../rag-api/) 的工程实践：

| 可复用 | 说明 |
|------|------|
| `config.py` 结构 | pydantic-settings + 环境变量管理 |
| `logging.py` | JSON 结构化日志 + request_id + timing |
| `middleware.py` | 请求计时 + CORS |
| `session.py` | 会话管理模型（扩展上下文状态） |
| `bm25.py` + `rrf.py` | BM25 关键词检索 + RRF 融合 |
| `eval.py` 框架 | 评估脚本模板，换测试题和 expected_source |
| `Dockerfile` + `docker-compose.yml` | 容器化模板 |
| `.env` 管理 | API Key + 路径配置 |

| 不可复用（需重写） | 原因 |
|------|------|
| `retrieve.py` | V1 单库通用检索 → V2 多知识库路由 + 商品对比检索 |
| `generate.py` | V1 简单 RAG prompt → V2 多意图 + 模型网关 + Agent Loop |
| `routes.py` | V1 `/chat` `/ingest` → V2 多意图路由 + `/admin` + 图片服务 |
| `ingest.py` | V1 通用文档摄入 → V2 商品规格文本生成 + 图片下载 |

---

## 当前进度

| Phase | 状态 | 产出 |
|-------|:---:|------|
| Phase 1: 爬虫 + 知识库 | ✅ | 6 品牌 304 产品 / 5 篇知识库 / pgvector 入库 |
| Phase 2: 检索 + 评估 + 工程基础 | 🔜 | src 骨架 / ruff+mypy+pytest / BM25+向量+Rerank / 25 题 |
| Phase 3: Agent 引擎 | ⬜ | 手写 ReAct / Tool Calling / 意图路由 / 单元测试 |
| Phase 4: 人工兜底 + 多轮 | ⬜ | 情绪检测 / 转人工 / 上下文管理 / conventional commits |
| Phase 5: 中台能力 + 管理后台 | ⬜ | /admin API / 多库管理 / 模型网关 / Token 统计 |
| Phase 6: 部署 + CI/CD + 文档 | ⬜ | Docker / GitHub Actions / PR template / README / 面试稿 |

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
| 多平台信息孤岛 | 商品数据在系统、知识在 Wiki、客服多窗口切换 | 新人上手慢 |
| 3C 商品生命周期短 | 新款上市 → 旧款降价 → 参数对比维度变化快 | 文档更新滞后 |

### 1.3 目标用户

- **终端用户**：在"极客数码"购物的消费者（25-35 岁，参数敏感型）
- **内部用户**：客服团队（处理 AI 转接的复杂工单）

### 1.4 核心目标

```
V1.0 上线后：
  AI 自动解决率 ≥ 65%
  客服人效提升 40%
  首次响应 < 2s（当前人工平均 45s）
  月 Token 成本 ≤ ¥500（DeepSeek 为主力）
```

---

## 二、产品功能

### 2.1 功能全景

| 模块 | 功能 | 优先级 | 说明 |
|------|------|:---:|------|
| 知识问答 | 参数查询、选购指南、售后政策 | P0 | 核心 RAG |
| 商品对比 | 多款产品参数自动对比 + 图片展示 | P0 | 3C 特有高频场景 |
| 库存查询 | 查商品库存和价格 | P1 | Tool Calling #1 |
| 订单查询 | 查订单物流状态 | P1 | Tool Calling #2 |
| 人工转接 | 复杂问题/投诉/大额退款升级 | P1 | Human-in-the-loop |
| 多轮对话 | 同一 session 内记住上下文 + 指代消解 | P0 | 基础体验 |
| 管理后台 | 知识库管理、模型配置、Token 统计 | P2 | 中台感 |

### 2.2 行为边界

```
❌ 不编造参数（必须从知识库/数据库取，不可靠 LLM 记忆）
❌ 不承诺库存（必须调 Tool 查实时数据）
❌ 不报价（价格从数据库取，LLM 不负责）
❌ 不执行付款/退款（只提供信息，不操作资金）
❌ 不说"最好""绝对""第一"（广告法合规）
❌ 被骂不骂回去（情绪检测 → 转人工）
```

### 2.3 典型对话

**场景 1：参数查询（RAG）**
```
用户："ThinkPad X1 Carbon 有什么接口？"
Agent："X1 Carbon Gen 13 有 2×雷雳4、2×USB-A、HDMI 2.1、耳麦接口。
       其中雷雳4 支持 40Gbps 传输和外接显示器。"
       [附带产品图]
来源：商品规格库 → ThinkPad X1 Carbon Gen 13
```

**场景 2：商品对比（RAG + 多文档检索）**
```
用户："MacBook Air 15 和 X1 Carbon 怎么选？"
Agent："对比两款：
       | 维度 | MacBook Air 15 | X1 Carbon Gen 13 |
       | 重量 | 1.5kg | 0.98kg |
       | 续航 | 18h | 15h |
       | 接口 | 仅2×雷雳4 | 2×雷雳4+2×USB-A+HDMI |
       经常出差+用iPhone → MacBook Air
       需要接投影仪/U盘 → X1 Carbon"
       [附带两张产品对比图]
```

**场景 3：库存查询（Tool Calling）**
```
用户："X1 Carbon 32GB 版有货吗？"
Agent：[调用 check_stock] → "广州仓现货 5 台，¥12999，今天下单明天到。"
```

**场景 4：人工转接**
```
用户："前天买的本还没到！！！再不处理投诉了！！！"
Agent：[情绪检测 → 负面+投诉] →
      "非常抱歉！已为您转接人工客服，优先处理，请稍等。"
      [后台生成转接工单：对话摘要 + 订单号]
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
                    │  参数查询/对比/库存  │
                    │  /订单/售后/闲聊    │
                    └──┬────┬─────┬──────┘
                       │    │     │
          ┌────────────┼────┼─────┼────────────┐
          │            │    │     │            │
          ▼            ▼    ▼     ▼            ▼
    ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ RAG 引擎 │ │ 对比引擎 │ │ Agent    │ │ 人工路由 │
    │ 参数查询 │ │ 多文档   │ │ 库存/物流│ │ 升级/转接│
    │ 选购指南 │ │ 检索     │ │ 查询     │ │          │
    └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
         │            │            │            │
         ▼            ▼            ▼            ▼
    ┌──────────────────────────────────────────────────┐
    │                    数据层                         │
    │  PostgreSQL + pgvector(知识库+商品+订单+库存) │ images/ │
    └──────────────────────────────────────────────────┘
                              │
                    ┌─────────▼─────────┐
                    │      模型层        │
                    │  DeepSeek(对话)    │
                    │  bge-large(嵌入)   │
                    └───────────────────┘
```

### 3.2 技术栈

| 层 | 技术 | 说明 |
|---|------|------|
| 后端框架 | FastAPI + Python 3.12 | 异步 SSE，企业级结构 |
| LLM | DeepSeek Chat API | OpenAI 兼容接口，性价比 |
| Embedding | bge-large-zh-v1.5 本地 GPU | 1024 维，离线可用 |
| 向量库 | pgvector (PostgreSQL) | 混合查询，生产级可靠性 |
| 检索 | BM25 + 向量 MMR + RRF | 三方案对比评估 |
| 爬虫 | Playwright | 过 JS Challenge |
| Agent | 手写 ReAct → 后续 LangGraph | 先理解原理再上框架 |
| 部署 | Docker Compose | FastAPI + PostgreSQL 编排 |
| 图片 | 本地 FileResponse | 原型规模够用，生产迁 OSS |

### 3.3 扩展方向：MCP 工具接入（Phase 5+ 预留）

[MCP（Model Context Protocol）](https://modelcontextprotocol.io/) 是 Anthropic 发布的标准化工具接入协议。当前项目手写的 `ToolRegistry` 管理 Python 函数，MCP 让它能调用外部进程提供的工具。

**价值**：
- 公司内部如果有独立的订单系统/ERP，可以暴露 MCP Server，Agent 直接调
- 社区已有的 MCP Server（数据库查询、文件操作、Slack 通知等）可以直接接入，不用重复开发
- 面试时展示"架构预留 MCP 接入能力"说明关注行业标准

**对现有代码的影响**：
- `ToolSpec` 的 `{name, description, parameters}` 和 MCP 的 `Tool` schema 天然兼容
- 接入时只需写一个 `MCPToolAdapter` 把 MCP 工具包装成 `ToolSpec`，注册进同一个 `ToolRegistry`
- Agent Loop 完全无感——它只管调 `registry.execute()`，不关心工具是本地的还是远程的

**时间点**：Phase 5（中台能力）之前不做，但架构预留适配位。

---

### 3.4 模型分层

| 场景 | 模型 | 原因 |
|------|------|------|
| 参数查询/选购建议 | DeepSeek | 确定性高，便宜够用 |
| 商品对比/推荐 | DeepSeek | 推理能力够 |
| 情绪检测 | DeepSeek（V1） | V2 可上 Claude 做兜底 |
| Embedding | bge-large 本地 GPU | 不花 API 钱 |
| 未来隐私数据 | 本地 Qwen via vLLM | 订单含手机号不外传 |

### 3.4 数据分布

```
PostgreSQL + pgvector（统一数据层）：
  ├── knowledge_chunks 表：选购指南/售后政策 chunks + 向量 → ~80 条
  ├── product_specs 表：商品规格描述 + 向量 + 品牌/价格等元数据 → ~300 条
  ├── products 表：商品主数据（规格 JSON/价格/库存/图片路径）→ 300 条
  ├── orders 表：模拟订单数据 → 200 条
  └── 优势：向量 + 元数据一次 SQL 查询，无需维护多个存储引擎

静态文件（本地图片）：
  └── images/laptops/{product_id}.jpg：产品主图，FileResponse 直接返回
```

---

## 四、迭代计划

### Phase 1：爬虫 + 知识库（Day 1-3）✅ 已完成

**目标**：真实商品数据入库，RAG 能检索到具体产品

- [x] 编写 5 篇知识库文档（笔记本选购/手机选购/售后政策/以旧换新/支付分期）
- [x] Playwright 爬虫：中关村在线笔记本频道，过 JS Challenge
- [x] 提取：产品名 + 价格 + 规格参数表（80+ 字段）
- [ ] 图片下载 → `data/images/laptops/{product_id}.jpg`（待补）
- [x] 清洗：规格键名归一化（KEY_MAP 28 个标准字段，覆盖不同品牌命名差异）
- [x] 生成可检索文本（模板拼接自然语言描述，`generate_descriptions.py`）
- [x] 生成结构化 JSON（`laptops.jsonl`，304 产品，完整 metadata）
- [x] Ingest → PostgreSQL + pgvector（`knowledge_chunks` 表 + `laptop_products` 表，HNSW 索引）

**实际产出**：
- 6 个品牌：联想(80) / 华硕(84) / 惠普(29) / 戴尔(37) / 华为(25) / Acer宏碁(49)
- 304 个笔记本产品，275 个有完整 CPU 参数
- 28 个标准字段覆盖：CPU/内存/存储/屏幕/GPU/接口/电池/重量等
- 5 篇知识库文档 → ~55 chunks（按 ## 标题分段，400 字窗口 + 50 字 overlap）
- pgvector 验证通过：知识库查询 + 产品混合查询（向量 + WHERE 过滤）均正常

**脚本清单**：
| 脚本 | 用途 |
|------|------|
| `scripts/crawl_test.py` | Playwright 爬虫主脚本 |
| `scripts/collect_params.py` | 从爬取结果提取参数 |
| `scripts/clean_products.py` | KEY_MAP 归一化清洗 |
| `scripts/generate_descriptions.py` | 模板拼接生成自然语言描述 |
| `scripts/ingest_knowledge.py` | 知识库 Markdown → pgvector `knowledge_chunks` 表 |
| `scripts/ingest_pgvector.py` | 产品描述 → pgvector `laptop_products` 表 |
| `scripts/verify_knowledge.py` | 知识库向量检索验证 |
| `scripts/verify_retrieval.py` | 产品混合检索验证（向量 + 元数据过滤） |

---

### Phase 2：检索 + 评估 + 工程基础（Day 4-5）🔜 当前阶段

**目标**：建立"团队级"工程规范 + 量化检索质量 + 四种方案对比

**2.0 工程基础（最先做——面试时每个文件都是"团队经验"的证明）**

- [ ] `pyproject.toml` — 项目元数据 + 工具配置中心
  ```toml
  [project]
  name = "3c-cs-agent"
  requires-python = ">=3.12"

  [tool.ruff]
  # 代码规范：E/W/F/I/N 全套规则，行宽 100

  [tool.mypy]
  # 类型检查：strict = false（渐进式，后续收紧）

  [tool.pytest.ini_options]
  # 测试配置：testpaths = ["tests/"], addopts = "-v --tb=short"
  ```

- [ ] `.pre-commit-config.yaml` — git commit 前自动检查
  ```yaml
  repos:
    - repo: https://github.com/astral-sh/ruff-pre-commit
      hooks: [ruff, ruff-format]
    - repo: https://github.com/pre-commit/mirrors-mypy
      hooks: [mypy]
  ```

- [ ] `Makefile` — 常用命令入口（新成员 clone 下来就知道怎么跑）
  ```makefile
  install:  pip install -e ".[dev]"
  lint:     ruff check src/ scripts/ && mypy src/
  test:     pytest -v
  eval:     python scripts/eval.py
  ingest:   python -m scripts.ingest_knowledge && python -m scripts.ingest_pgvector
  clean:    rm -rf __pycache__ .pytest_cache .mypy_cache
  ```

- [ ] `tests/` 目录 — 从 Day 1 就写测试，不是事后补
  ```
  tests/
  ├── __init__.py
  ├── test_retrieve.py      # 检索单元测试
  ├── test_bm25.py           # BM25 分词 + 搜索测试
  ├── test_rrf.py            # RRF 融合测试
  └── test_ingest.py         # 摄入 pipeline 测试
  ```

**为什么 Phase 2 就做这些**：你一个人写，但这些文件让面试官看到的不是"我一个人写的项目"，而是"一个被 review 过的、有门禁的、新成员能接手的项目"。`Makefile` + `pyproject.toml` + `.pre-commit-config.yaml` 这三个文件一摆，团队感就出来了。

**2.1 搭建 `src/` 业务骨架**

- [ ] `src/config.py` — pydantic-settings，统一管理 PG 连接串 / Embedding 模型 / LLM Key
- [ ] `src/logging.py` — JSON 结构化日志（request_id + latency_ms + module）
- [ ] `src/exceptions.py` — 异常继承体系（先定义，Phase 3 用）
- [ ] `src/core/retrieve.py` — 多知识库检索引擎（见下方设计）
- [ ] `src/core/bm25.py` — BM25 关键词检索适配 pgvector 场景
- [ ] `src/core/rrf.py` — RRF 融合算法
- [ ] `src/core/rerank.py` — Cross-encoder 精排（bge-reranker-v2-m3）

**2.2 检索架构设计（pgvector 版，含 Rerank）**

```
用户 query
    │
    ├──→ BM25 检索（对 laptop_products.description + knowledge_chunks.content）
    │     └── 返回：Top-20 + bm25_score
    │
    ├──→ pgvector 向量检索
    │     ├── 知识库路由：query 分类 → knowledge_chunks 或 laptop_products
    │     ├── 混合查询：SELECT ... ORDER BY embedding <=> query_vec LIMIT 20
    │     │   （可选 WHERE brand/price/product_type 缩小范围）
    │     └── 返回：Top-20 + vector_score
    │
    ├──→ RRF 融合（两面 Top-20 → 融合后 Top-20）
    │     RRF_score(d) = Σ 1/(k + rank_i(d))
    │
    └──→ Rerank 精排（Cross-encoder 对 Top-20 逐条打分 → Top-5）
          bge-reranker-v2-m3(query, doc) → relevance_score
          输出最终 Top-5
```

**为什么 Rerank 放在 Phase 2**：双塔（bge-large）把 query 和 doc 分别编码，快但交互不充分；Cross-encoder 把 query+doc 拼一起编码，慢但准。先粗筛 20 条再精排 5 条，MRR 通常提 10-20%，是 RAG 性价比最高的优化。`pip install FlagEmbedding` 一行依赖，~50 行代码。

**2.3 多知识库路由策略**

| Query 类型 | 目标表 | 策略 |
|-----------|--------|------|
| "X1 Carbon 内存多大" | `laptop_products` | 精确参数查询，向量检索为主 |
| "8000 以内轻薄本推荐" | `laptop_products` | 向量检索 + `WHERE price <= 8000` |
| "笔记本怎么选CPU" | `knowledge_chunks` | 选购指南文档检索 |
| "退货需要什么条件" | `knowledge_chunks` | 售后政策检索 |
| "MateBook 和 ThinkPad 对比" | `laptop_products` | 多产品检索 + 提取对比 |

**2.4 评估体系**

- [ ] 设计 25 道测试题，覆盖 4 个维度：

```
精确查询（8 题）： "MateBook X Pro 内存多大"
对比查询（5 题）： "联想小新和华为 MateBook 怎么选"
选购建议（7 题）： "8000 以内编程本推荐"
政策查询（5 题）： "笔记本退货条件是什么"
```

- [ ] 每题标注 `expected_source`：产品 ID 或文档 source
- [ ] 跑四种方案消融实验：

```
方案 A：纯 BM25 关键词检索
方案 B：纯 pgvector 向量检索（cosine distance）
方案 C：BM25 + 向量 RRF 混合
方案 D：C + Rerank 精排
```

- [ ] 评估指标：Hit@1 / Hit@3 / Hit@5 / MRR / NDCG@5
- [ ] 输出对比表格，记录各方案延迟（P50/P95），确定最终检索方案

**产出**：检索评估报告、四方案对比数据、带 Rerank 的最终检索 pipeline

---

### Phase 3：Agent 引擎（Day 6-8）

**目标**：Agent 能走完"意图识别 → 工具调用 → 生成回答"全链路

- [ ] 意图分类：参数查询 / 商品对比 / 库存查询 / 订单查询 / 售后 / 闲聊
- [ ] 手写 ReAct 循环引擎（`src/agent/loop.py`）
- [ ] 工具注册中心（`src/agent/tools.py`）：注册 / 发现 / 执行 / 结果校验
- [ ] Tool #1：`check_stock(product_name, variant)` — 查库存
- [ ] Tool #2：`search_order(order_id)` — 查订单物流
- [ ] 商品对比链路：多文档检索 → 提取参数 → LLM 生成对比表
- [ ] 死循环防御：max_steps=5 + 同一 Tool 连续调用 3 次检测
- [ ] 流式透传：Agent 思考步骤 SSE 推给前端

**Agent Loop 设计**：
```
while step < max_steps:
    ① LLM 输出 Thought + Action
    ② 解析 Action → 调对应 Tool
    ③ Tool 返回 Observation
    ④ 判断：任务完成？→ 退出循环 → 生成最终回答
           未完成？ → 继续下一步
           异常？   → 降级兜底
```

- [ ] 单元测试覆盖（跟着代码写，不事后补）：
  - `tests/test_loop.py` — ReAct 循环状态转换测试（mock LLM 输出）
  - `tests/test_tools.py` — 工具注册/执行/校验测试
  - `tests/test_intent_router.py` — 意图分类准确率测试

**产出**：Agent 能查库存、查订单、做商品对比

---

### Phase 4：人工兜底 + 多轮对话（Day 9-10）

**目标**：异常有兜底，对话有记忆

**工程规范**：
- [ ] 启用 [Conventional Commits](https://www.conventionalcommits.org/)：
  ```
  feat(src): 实现情绪检测和转人工逻辑
  fix(agent): ReAct 循环同 Tool 连续 3 次检测未生效
  test(eval): 补充分流场景 15 道端到端测试题
  docs(README): 更新架构图和快速开始指南
  ```
  每个 commit 像被同事 review 过——`feat/fix/test/docs/chore` 前缀 + 作用域。

- [ ] 情绪检测：负面词/连续否定/投诉词 → 触发转人工
- [ ] 转人工逻辑：生成对话摘要 + 已尝试方案 → 生成工单
- [ ] 会话上下文管理：session 内记住当前产品名、订单号、意图
- [ ] 指代消解："这个有黑色的吗" → 指代上文提及的产品
- [ ] 敏感操作降级：大额订单相关查询走人工确认

**转人工触发条件**：
```
① Agent 连续 2 次未能解决 → "正在为您转接人工客服"
② 情绪检测到负面/投诉
③ 用户明确说"转人工""找真人"
④ 涉及大额订单（>5000 元）的退款类操作
```

**产出**：生产级兜底逻辑完成

---

### Phase 5：中台能力 + 管理后台（Day 11-12）

**目标**：从 Agent 升级为 AI 中台雏形

- [ ] 多知识库管理：产品库 / 售后库 / 营销素材库（预留）
- [ ] `/admin` API：
  - `GET /admin/knowledge-bases` — 列出所有知识库
  - `POST /admin/knowledge-bases/{id}/ingest` — 上传文档
  - `GET /admin/knowledge-bases/{id}/stats` — 统计
  - `GET /admin/models` — 模型列表及成本
  - `GET /admin/stats/daily` — 日调用统计
  - `GET /admin/stats/tools` — 工具调用统计
- [ ] 模型网关：按任务类型选模型，Token 消耗记录
- [ ] 商品图片端点：`GET /product/{id}/image` → FileResponse

**产出**：可演示的 AI 中台雏形

---

### Phase 6：部署 + CI/CD + 文档（Day 13-14）

**目标**：一键部署 + CI 绿勾 + 团队级 repo 面貌，面试打开 GitHub 就加分

**6.1 CI/CD（GitHub Actions）**

- [ ] `.github/workflows/ci.yml` — 每次 push 自动跑：
  ```yaml
  on: [push, pull_request]
  jobs:
    lint:    ruff check + mypy
    test:    pytest -v --cov=src --cov-report=term-missing
    eval:    python scripts/eval.py -m fast  # 5 题抽查
  ```
  面试官打开 repo 看到 CI 绿勾 → 默认你是在规范化团队待过的。

- [ ] `.github/workflows/eval-nightly.yml` — 每天凌晨完整评估：
  ```yaml
  on:
    schedule: [{cron: "37 2 * * *"}]
    # 凌晨 2:37（避开整点 + 半小时高峰）
  jobs:
    eval-full:
      python scripts/eval.py -m full --output eval_history/
  ```
  记录每次评估结果，Phase 2 到 Phase 6 的 Hit@K/MRR 变化可视化。

- [ ] `codecov.yml` — 测试覆盖率配置（可选但好看）

**6.2 团队协作文档（一个下午写完，永久加分）**

- [ ] `CONTRIBUTING.md` — 新成员上手指南：
  ```
  # 贡献指南
  1. git clone + make install
  2. 创建分支 feat/xxx 或 fix/xxx
  3. pre-commit install（自动 ruff + mypy）
  4. 写测试，make test 通过
  5. PR 到 main，描述变更 + 测试结果
  ```

- [ ] `PULL_REQUEST_TEMPLATE.md`（`.github/` 目录下）：
  ```markdown
  ## 变更说明
  ## 测试
  - [ ] make lint 通过
  - [ ] make test 通过
  - [ ] make eval 通过（不退步）
  ## 截图（如有 UI 变更）
  ```

- [ ] `CHANGELOG.md` — 按 Conventional Commits 自动或手动维护

**6.3 部署**

- [ ] Docker Compose：FastAPI + PostgreSQL 编排
- [ ] Dockerfile：多阶段构建（bge-large 模型可选挂载）
- [ ] `.env.example`：必填配置项（DEEPSEEK_API_KEY, EMBEDDING_DEVICE）
- [ ] 健康检查：PostgreSQL + Embedding 模型 + LLM 可达性
- [ ] 端到端场景测试（5 个核心场景，每个 3 个变体）
- [ ] README：架构图 + CI badge + 快速开始 + API 文档 + 评估数据
- [ ] 面试话术：每个 Phase 的关键决策和踩坑记录

**Docker 注意事项**：
```
① bge-large 模型文件 ~1.3GB，不要打进镜像
   → 挂载 volume 或首次启动时自动下载
② PostgreSQL 数据目录挂载到宿主机，容器重启不丢数据
③ 国内构建时 pip 换清华源，否则 langchain 装半天
④ embedding 如果用 CUDA，需要在 docker-compose 里配 deploy.resources
```

**产出**：完整的可交付项目

---

## 五、企业级实践（从 V1 经验搬过来）

### 5.1 模块级单例 → FastAPI Lifespan

```
❌ Demo 做法：_hf = HuggingFaceEmbeddings() 放在模块顶层
✅ 企业做法：FastAPI lifespan 管理资源生命周期，Depends() 注入
面试价值：⭐⭐⭐ 必问——"你的模型是怎么加载和管理的"
```

### 5.2 结构化日志

```
每行 JSON 带：request_id / latency_ms / module / function / message
面试价值：⭐⭐⭐——"生产环境你怎么排查问题"
```

### 5.3 异步不阻塞

```
CPU 密集（Embedding 编码）→ asyncio.to_thread() 线程池
IO 密集（LLM API 调用）→ ainvoke() 异步等待
面试价值：⭐⭐——"asyncio 和线程池的区别"
```

### 5.4 异常继承体系

```
BaseAppException
├── RetrievalError
├── LLMError（带重试 3 次）
├── ToolExecutionError
└── ConfigError
全局 exception handler 统一错误响应格式
面试价值：⭐⭐⭐——"Agent 工具调用失败了怎么办"
```

### 5.5 健康检查认真做

```
/health → 检查 PostgreSQL 连通性 + Embedding 模型状态 + LLM API 可达性
返回 {"status": "healthy/degraded", "components": {...}}
面试价值：⭐⭐——"你的服务怎么知道自己是健康的"
```

---

## 六、关键决策记录

### 6.1 为什么选 3C 数码

- 参数密集，RAG 检索精度要求高 → 面试能讲检索优化
- 对比类问题是 3C 高频场景 → 天然适合 Agent 多步推理
- 公开数据（中关村在线），不用编造假数据
- 自己就是 3C 用户，理解用户行为

### 6.2 为什么先手写 Agent 再上 LangGraph

- 手写能理解 ReAct 的每一步：状态管理、工具调度、错误恢复
- 踩完坑才能理解 LangGraph 的 StateGraph 设计动机
- 面试能讲"手写 vs 框架"的对比和选择理由

### 6.3 为什么爬图片

- 搜索结果带产品图，体验完全不一样
- 面试演示时可以展示产品图片，比纯文字直观
- 本地 FileResponse，不引入 OSS 依赖

### 6.4 为什么先笔记本单一品类

- 知识库和爬虫都聚焦一个品类，能快速出效果
- 跑通后手机、平板复制同一套流程
- 目录结构预留扩展位

### 6.5 为什么不做 Multi-Agent

- 单 Agent 多 Tool 覆盖当前场景
- 用户量小，Multi-Agent 协调开销 > 并行收益
- 架构预留模块扩展位，需要时再拆分

### 6.6 为什么用 pgvector 而不是 ChromaDB

- 项目数据不止向量：商品元数据、订单、库存天然适合关系型数据库
- pgvector 的混合查询（`WHERE brand='联想' ORDER BY vec <=> query`）一次 SQL 搞定，ChromaDB 需要向量检索 + 元数据过滤两次操作
- 统一存储引擎：PostgreSQL 一个服务替代 ChromaDB + JSON 文件 + 订单文件，架构更简洁
- ACID 事务、主从复制、备份恢复都是全行业成熟方案，ChromaDB 的持久化和高可用方案相对薄弱
- 面试能讲"从 ChromaDB 切到 pgvector 的架构决策"——这是一个好的技术选型故事

### 6.7 为什么一个人也要做 CI/CD + PR Template + pre-commit

- 前几次面试被问 "有团队协作经验吗" 答不上来
- GitHub 仓库面貌是面试官判断你团队经验的直觉依据——CI 绿勾、PR 模板、Makefile、pre-commit 门禁，这些文件摆在那，默认你在规范化团队待过
- Conventional Commits 让 commit 历史有章法——一个 `feat/fix/test/docs/chore` 的 commit log 比 50 条 "update" 有力得多
- 成本极低：`Makefile` 10 行，`.pre-commit-config.yaml` 15 行，CI yaml 30 行——加起来不到 100 行配置，但 repo 面貌从 "个人练手" 变成 "团队项目"
- 本质上是给自己做 CI——一个人写久了会忘测试、忘 lint，pre-commit 在你 commit 之前拦住你，CI 在你 push 之后告诉你有没有退化

### 6.8 为什么预留 MCP 但不现在做

- MCP 本质是工具接入的标准化协议，当前项目工具只有 2 个（查库存、查订单），手写 `ToolRegistry` 够用
- `ToolSpec` 的 `{name, description, parameters}` 和 MCP 的 Tool schema 天然对齐，未来接入不需要重构
- 面试能讲"架构预留 MCP 接入能力，关注行业标准"——证明你不是只会写 demo，知道生产环境的工具集成怎么做
- Phase 5（中台能力）阶段如果有多知识库、多系统的工具调用需求，再引入 MCP Client

---

## 七、风险与降级

| 风险 | 影响 | 降级方案 |
|------|------|---------|
| PostgreSQL/pgvector 挂了 | 检索不可用 | 降级 JSON 关键词搜索 |
| DeepSeek API 挂了 | 无法生成回答 | 返回检索文档原文 |
| 商品数据过期 | 参数不准确 | 定时重爬 + 更新索引 |
| Agent 死循环 | Token 暴涨 | max_steps=5 + 重复调用检测 |
| Prompt 注入 | 模型越权 | 输入长度限制 + 输出校验 |
| Playwright 反爬升级 | 爬虫失效 | Cookie 手动注入备选方案 |
| 图片下载失败 | 产品图缺失 | 返回占位图路径，不阻塞主流程 |

---

## 八、成功指标

| 指标 | 目标 | 测量方式 |
|------|------|---------|
| 知识库 Hit@K | ≥ 90% | eval.py 25 题 |
| 知识库 MRR | ≥ 0.85 | eval.py |
| Token/次 | ≤ 3000 | 结构化日志 |
| 响应延迟 | ≤ 5s 非流式 | 结构化日志 timing |
| 工具调用成功率 | ≥ 95% | Tool 日志 |
| 图片覆盖率 | ≥ 90% 产品有图 | products.json 检查 |
| 测试覆盖率 | ≥ 70% | pytest --cov |
| CI 通过率 | 100% | GitHub Actions badge |

---

## 九、目录结构

```
3c-cs-agent/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                # push 自动 ruff + pytest + eval
│   │   └── eval-nightly.yml      # 凌晨完整评估
│   └── PULL_REQUEST_TEMPLATE.md  # PR 模板
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI 入口 + lifespan
│   ├── config.py                # pydantic-settings 配置
│   ├── logging.py               # 结构化日志
│   ├── middleware.py            # 请求计时 + CORS
│   ├── exceptions.py            # 异常继承体系
│   ├── session.py               # 多轮会话管理
│   ├── api/
│   │   ├── __init__.py
│   │   ├── chat.py              # /chat /chat/stream
│   │   └── admin.py             # /admin/* 管理后台
│   ├── core/
│   │   ├── __init__.py
│   │   ├── retrieve.py          # RAG 多库检索引擎
│   │   ├── bm25.py              # BM25 关键词检索
│   │   ├── rrf.py               # RRF 融合算法
│   │   ├── rerank.py            # Cross-encoder 精排
│   │   ├── generate.py          # LLM 生成 + 模型网关
│   │   ├── ingest.py            # 文档/商品数据摄入
│   │   ├── intent_router.py     # 意图分类
│   │   └── model_gateway.py     # 模型路由 + Token 统计
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── loop.py              # ReAct 循环
│   │   ├── tools.py             # 工具注册中心
│   │   ├── tools/
│   │   │   ├── __init__.py
│   │   │   ├── stock.py         # 查库存
│   │   │   └── order.py         # 查订单
│   │   └── human_loop.py        # 人工审批/转接
│   └── modules/                  # 业务模块（扩展位）
│       ├── __init__.py
│       ├── customer_service/
│       ├── product_compare/
│       └── content_gen/
├── tests/                        # 单元测试（跟随代码写，不事后补）
│   ├── __init__.py
│   ├── test_retrieve.py
│   ├── test_bm25.py
│   ├── test_rrf.py
│   ├── test_rerank.py
│   ├── test_loop.py
│   ├── test_tools.py
│   └── test_intent_router.py
├── data/
│   ├── knowledge/               # 知识库源文档
│   │   ├── laptop_guide.md
│   │   ├── phone_guide.md
│   │   ├── after_sales.md
│   │   ├── trade_in.md
│   │   └── payment.md
│   ├── products/                # 商品数据（爬虫产出）
│   │   ├── raw/
│   │   ├── normalized/
│   │   └── laptops.jsonl
│   ├── images/                  # 产品图片
│   │   └── laptops/
│   ├── mock/                    # 模拟数据
│   │   ├── orders.json
│   │   └── stores.json
│   └── test_questions.json      # 评估测试题
├── scripts/
│   ├── crawl_test.py
│   ├── collect_params.py
│   ├── clean_products.py
│   ├── generate_descriptions.py
│   ├── ingest_knowledge.py
│   ├── ingest_pgvector.py
│   ├── verify_knowledge.py
│   ├── verify_retrieval.py
│   ├── eval.py
│   └── generate_orders.py
├── eval_history/                 # 每次评估结果存档（Phase 2+）
├── pgdata/                      # PostgreSQL 持久化目录（gitignore）
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml      # git commit 前自动门禁
├── pyproject.toml               # ruff + mypy + pytest 配置
├── Makefile                     # 常用命令入口
├── CONTRIBUTING.md              # 新成员贡献指南
├── CHANGELOG.md                 # 变更记录
├── requirements.txt
├── README.md
└── PLAN.md                      # 本文件
```

---

## 十、面试讲述结构

### 30 秒版：这是什么

> "我做了一个面向 3C 数码电商场景的智能客服 Agent 系统。用真实爬取的 304 个笔记本数据搭建了 pgvector 多知识库 RAG 和 ReAct Agent 引擎，支持参数查询、商品对比、库存查询和自动转人工。整个项目按团队标准维护：CI/CD、pre-commit 门禁、Makefile 一键命令、Conventional Commits。"

### 2 分钟版：怎么做的

> "分五步。第一步，用 Playwright 从中关村在线爬了 304 个笔记本的真实规格参数（覆盖 6 个品牌），28 个标准字段做了归一化清洗，用 pgvector 替代了 ChromaDB——统一 PostgreSQL 存储向量和元数据。第二步，用 BM25+向量+RRF 混合三种方案做消融实验，25 道测试题量化评估 Hit@K 和 MRR。第三步，手写了 ReAct Agent 循环，实现查库存和查订单两个工具调用。第四步，加了人工兜底——情绪检测、转人工、敏感操作审批。第五步，加了管理后台——多知识库管理、模型路由、Token 统计。整个系统 Docker 一键部署。"

### 核心技术亮点

> "一是用 Playwright 解决了中关村的 JS Challenge 反爬，二是选了 pgvector 做统一数据层——向量检索和元数据过滤一次 SQL 搞定，三是手写 ReAct 循环理解了 Agent 的状态管理，四是用四方案消融实验（含 Rerank）量化了检索效果——不是感觉好，是数据说话。另外整个项目按团队工程标准维护：GitHub Actions CI、ruff+mypy 代码门禁、Makefile 一键命令、Conventional Commits——虽然是我一个人写的，但项目面貌是团队级的。"

### 踩过的坑

> "爬虫踩了 JS Challenge 的坑。pgvector 踩了 psycopg2 向量传参的坑。Agent 踩了死循环的坑——LLM 反复调同一个工具，加了相同 Tool 连续 3 次检测。工程上踩了'一个人怎么写团队项目'的坑——靠 pyproject.toml + pre-commit + CI + Conventional Commits 这套组合拳，让 solo project 看起来像团队维护的。这些坑让我理解了为什么生产级代码需要门禁和自动化。"

---
