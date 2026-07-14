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
| Phase 2: 检索 + 评估 | 🔜 | 多知识库路由 / BM25+向量+RRF 三方案消融 / 25 题 |
| Phase 3: Agent 引擎 | ⬜ | 手写 ReAct / Tool Calling / 意图路由 |
| Phase 4: 人工兜底 + 多轮 | ⬜ | 情绪检测 / 转人工 / 上下文管理 |
| Phase 5: 中台能力 + 管理后台 | ⬜ | /admin API / 多库管理 / 模型网关 / Token 统计 |
| Phase 6: 部署 + 测试 + 文档 | ⬜ | Docker / 端到端测试 / README / 面试稿 |

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

### 3.3 模型分层

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

### Phase 2：检索 + 评估（Day 4-5）🔜 当前阶段

**目标**：可量化的检索质量，三种方案对比

**2.1 搭建 `src/` 目录骨架**

- [ ] `src/config.py` — pydantic-settings，统一管理 PG 连接串 / Embedding 模型 / LLM Key
- [ ] `src/logging.py` — JSON 结构化日志（request_id + latency_ms + module）
- [ ] `src/core/retrieve.py` — 多知识库检索引擎（见下方设计）
- [ ] `src/core/bm25.py` — BM25 关键词检索适配 pgvector 场景
- [ ] `src/core/rrf.py` — RRF 融合算法

**2.2 检索架构设计（pgvector 版）**

```
用户 query
    │
    ├──→ BM25 检索（对 product_specs.description + knowledge_chunks.content 分词建倒排）
    │     └── 返回：[(id, bm25_score), ...]
    │
    ├──→ pgvector 向量检索
    │     ├── 知识库路由：query 分类 → knowledge_chunks 或 laptop_products
    │     ├── 混合查询：SELECT ... ORDER BY embedding <=> query_vec LIMIT K
    │     │   （可选 WHERE brand/price/product_type 缩小范围）
    │     └── 返回：[(id, vector_score), ...]
    │
    └──→ RRF 融合
          RRF_score(d) = Σ 1/(k + rank_i(d))
          输出最终 Top-K
```

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
- [ ] 跑三方案评估：

```
方案 A：纯 BM25 关键词检索
方案 B：纯 pgvector 向量检索（cosine distance）
方案 C：BM25 + 向量 RRF 混合
```

- [ ] 评估指标：Hit@1 / Hit@3 / Hit@5 + MRR
- [ ] 输出对比表格，确定最终检索方案

**产出**：检索评估报告，三方案对比数据，确定最终检索方案

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

**产出**：Agent 能查库存、查订单、做商品对比

---

### Phase 4：人工兜底 + 多轮对话（Day 9-10）

**目标**：异常有兜底，对话有记忆

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

### Phase 6：部署 + 测试 + 文档（Day 13-14）

**目标**：一键部署，全链路能跑，面试能讲

- [ ] Docker Compose：FastAPI + PostgreSQL 编排
- [ ] Dockerfile：多阶段构建（bge-large 模型可选挂载）
- [ ] `.env.example`：必填配置项（DEEPSEEK_API_KEY, EMBEDDING_DEVICE）
- [ ] 健康检查：PostgreSQL + Embedding 模型 + LLM 可达性
- [ ] 端到端场景测试（5 个核心场景，每个 3 个变体）
- [ ] README：架构图 + 快速开始 + API 文档 + 评估数据
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

---

## 九、目录结构

```
3c-cs-agent/
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI 入口 + lifespan
│   ├── config.py                # pydantic-settings 配置
│   ├── logging.py               # 结构化日志
│   ├── middleware.py            # 请求计时 + CORS
│   ├── exceptions.py            # 异常继承体系
│   ├── session.py               # 多轮会话管理
│   ├── bm25.py                  # BM25 关键词检索
│   ├── rrf.py                   # RRF 融合算法
│   ├── api/
│   │   ├── __init__.py
│   │   ├── chat.py              # /chat /chat/stream
│   │   └── admin.py             # /admin/* 管理后台
│   ├── core/
│   │   ├── __init__.py
│   │   ├── retrieve.py          # RAG 多库检索引擎
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
│       ├── customer_service/    # 客服模块
│       ├── product_compare/     # 商品对比
│       └── content_gen/         # 内容生成（预留）
├── data/
│   ├── knowledge/               # 知识库源文档
│   │   ├── laptop_guide.md      # 笔记本选购指南
│   │   ├── phone_guide.md       # 手机选购指南
│   │   ├── after_sales.md       # 售后政策
│   │   ├── trade_in.md          # 以旧换新政策
│   │   └── payment.md           # 支付与分期
│   ├── products/                # 商品数据（爬虫产出）
│   │   ├── raw/                  # 原始爬取数据（JSONL，按品牌分文件）
│   │   ├── normalized/           # KEY_MAP 归一化清洗后（JSONL，按品牌分文件）
│   │   └── laptops.jsonl         # 合并清洗后的 304 条产品数据
│   ├── images/                  # 产品图片
│   │   └── laptops/             # 笔记本主图
│   │       ├── zol_10001.jpg
│   │       └── ...
│   ├── mock/                    # 模拟数据
│   │   ├── orders.json          # 订单数据
│   │   └── stores.json          # 库存数据
│   └── test_questions.json      # 评估测试题
├── scripts/
│   ├── crawl_test.py            # Playwright 爬虫主脚本
│   ├── collect_params.py        # 从爬取结果提取参数
│   ├── clean_products.py        # KEY_MAP 规格清洗 + 归一化
│   ├── generate_descriptions.py # 模板拼接自然语言描述
│   ├── ingest_knowledge.py      # 知识库 Markdown → pgvector knowledge_chunks 表
│   ├── ingest_pgvector.py       # 产品数据 → pgvector laptop_products 表
│   ├── verify_knowledge.py      # 知识库向量检索验证
│   ├── verify_retrieval.py      # 产品混合检索验证（向量 + WHERE 过滤）
│   ├── eval.py                  # 检索评估脚本（Phase 2）
│   └── generate_orders.py       # 模拟订单生成（Phase 3）
├── pgdata/                      # PostgreSQL 持久化目录（gitignore）
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── PLAN.md                      # 本文件
```

---

## 十、面试讲述结构

### 30 秒版：这是什么

> "我做了一个面向 3C 数码电商场景的智能客服 Agent 系统。用真实爬取的商品数据搭建了多知识库 RAG 和 ReAct Agent 引擎，支持参数查询、商品对比、库存查询和自动转人工。"

### 2 分钟版：怎么做的

> "分五步。第一步，用 Playwright 从中关村在线爬了 304 个笔记本的真实规格参数（覆盖 6 个品牌），28 个标准字段做了归一化清洗，用 pgvector 替代了 ChromaDB——统一 PostgreSQL 存储向量和元数据。第二步，用 BM25+向量+RRF 混合三种方案做消融实验，25 道测试题量化评估 Hit@K 和 MRR。第三步，手写了 ReAct Agent 循环，实现查库存和查订单两个工具调用。第四步，加了人工兜底——情绪检测、转人工、敏感操作审批。第五步，加了管理后台——多知识库管理、模型路由、Token 统计。整个系统 Docker 一键部署。"

### 核心技术亮点

> "一是用 Playwright 解决了中关村的 JS Challenge 反爬，二是选了 pgvector 做统一数据层——向量检索和元数据过滤一次 SQL 搞定，比 ChromaDB 架构更简洁，三是手写 ReAct 循环理解了 Agent 的状态管理，四是用三方案消融实验量化了检索效果——不是感觉好，是数据说话。"

### 踩过的坑

> "爬虫踩了 JS Challenge 的坑。pgvector 踩了 psycopg2 向量传参的坑——没有用 pgvector Python adapter，靠字符串 cast `'[...]'::vector` 绕过去。Agent 踩了死循环的坑——LLM 反复调同一个工具，加了相同 Tool 连续 3 次检测。多库检索踩了路由的坑——用户问'X1 Carbon 怎么退货'，该先搜售后政策库还是先搜商品库。这些坑让我理解了为什么生产级 Agent 需要显式状态图。"

---
