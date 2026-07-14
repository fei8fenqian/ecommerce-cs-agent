# RAG 知识库问答系统 — 工程化迭代计划

> **当前状态**：Phase 5 ✅ → Phase 6 进行中  
> **核心原则**：不是 Demo，是按企业生产标准构建的 RAG 服务

## 当前进度

| Phase | 状态 | 产出 |
|-------|:---:|------|
| Phase 1: 爬虫 | ✅ | 爬取 FastAPI 中文教程 52 篇 |
| Phase 2: 检索改进 | ✅ | bge-large + MMR + 阈值 + 同源去重 |
| Phase 3: 评估 | ✅ | 25题 Hit@K 96% MRR 0.91 |
| Phase 4: 企业级基础 | ✅ | 结构化日志 / Lifespan DI / 异常体系 / 中间件链 / SSE |
| Phase 5: 混合检索 | ✅ | jieba BM25 + 向量 RRF / 三方案对比：BM25 80% / 纯向量 96% / 混合 92% |
| Phase 6: 部署+测试 | 🔜 | Docker / 健康检查 / 核心单测 / README |
| Phase 7: 意图+缓存+多库 | ⬜ | 规则路由(闲聊→direct/技术→RAG/工具→tool) + LRU + 多 collection 路由 |
| Phase 8: 简历 | ⬜ | 面试稿 |

## 0. 企业级改造 — 从 Demo 到 Production

当前代码的几个"一眼 Demo"问题，必须修掉：

| # | 问题 | 现状 | 企业做法 | 面试价值 |
|---|------|------|---------|---------|
| 1 | 模块级单例 | `_hf = HuggingFaceEmbeddings(...)` 在模块顶层，import 时就加载模型 | FastAPI `lifespan` 管理资源生命周期，`Depends()` 注入 | ⭐⭐⭐ 必问 |
| 2 | 无结构化日志 | `print()` + 零散 `logging`，无 request_id 追踪 | JSON 格式日志，每行带 `request_id`/`latency_ms`/`module` | ⭐⭐⭐ 生产排查必备 |
| 3 | 同步阻塞 | `def chat()` 同步路由，检索和 LLM 调用阻塞事件循环 | `async def` + `asyncio.to_thread()` 包装 CPU 密集操作 | ⭐⭐ |
| 4 | 异常直接抛 | LLM 挂了抛 500，无分类无降级 | 异常继承体系 + 全局 exception handler + 统一错误响应格式 | ⭐⭐⭐ |
| 5 | 配置无校验 | `Settings` dataclass，值不对启动才炸 | `pydantic-settings` 自动校验类型/范围/必填 | ⭐⭐ |
| 6 | 健康检查假 | `/health` 只返回 ok，不检查依赖 | 检查 ChromaDB 连通性 + embedding 模型状态 + LLM 可达性 | ⭐⭐ |
| 7 | 无测试 | 全靠手工 curl | 核心逻辑单测（retrieve/generate/bm25），pytest + fixtures | ⭐⭐⭐ |

### Phase 1 撞的坑（面试可讲）
1. **爬虫链接提取**：FastAPI MkDocs 侧边栏链接是 `./` 相对路径 → `urljoin(base, href)` 处理
2. **HTTP ingest 超时**：570 块嵌入耗时 > HTTP 超时 → 离线 Python 脚本跑
3. **HuggingFace 联网失败**：sentence-transformers 每次加载模型检查 `adapter_config.json`，国内墙 → `TRANSFORMERS_OFFLINE=1`，且必须放在 import HuggingFaceEmbeddings **之前**
4. **英文 embedding 做中文检索翻车**：`all-MiniLM-L6-v2` 是英文模型，对中文语义区分度差。测试 "怎样定义路径参数？" 时，`路径参数.md` 排不进 top-4，反而是 `公司介绍.md` 的 chunk 反复出现。LLM 答案正确但来源全错——说明 LLM 在用自己训练数据回答，而非检索结果 → Phase 2 换 `paraphrase-multilingual-MiniLM-L12-v2`

### 验证结果（Phase 1 最终测试）
- 问"怎样定义路径参数？"✅ 回答正确，但**来源全错**
- 检索返回：`公司介绍.md`(x2)、`后台任务.md`、`响应状态码.md` — 全部和路径参数无关
- 真正相关的 `路径参数.md` **根本没进 top-4**
- 答案正确是因为 LLM 用了自己的训练数据，不是检索结果 → **RAG 变成了"假 RAG"**
- 根因：`all-MiniLM-L6-v2` 是英文模型，对中文 embedding 质量差
- → Phase 2 需要：① 换 multilingual 模型 ② MMR 去重 ③ 阈值过滤 ④ 来源校验（带分数）

---


## 0. 现状诊断

### 已完成的（Demo 阶段）
- FastAPI 服务，`/ingest` + `/chat` + `/health`
- 文档摄入：PDF / txt / md → 分块 → 嵌入 → ChromaDB
- 检索 + LLM 生成：相似度搜索 → prompt 拼接 → DeepSeek 回答
- DeepSeek LLM + HuggingFace 本地 embedding
- 配置文件、环境变量管理

### Demo 的问题（面试官一问就穿帮）
| 维度 | 现在 | 缺失 |
|------|------|------|
| 数据规模 | 1 篇文档 | 没有规模，撞不到坑 |
| 检索策略 | 只一种相似度检索 | 无 MMR、无混合检索、无 Reranker |
| 评估 | 无 | 不知道检索准不准 |
| 错误处理 | 无重试、无降级 | LLM 挂了直接 500 |
| 多轮对话 | 无 | 每次问答独立 |
| 日志/可观测 | print() | 无结构化日志、无耗时统计 |
| 数据获取 | 手动放文件 | 无爬虫、无自动摄入 |

---

## 1. 目标

把 rag-api 从 Demo 升级为**可以拿到面试讲的工程项目**。

核心原则：**必须撞到真实的坑**。数据量不够就加数据，检索不准就量化和改进，LLM 挂了就处理。

---

## 2. 最终交付物

一个 FastAPI 服务，具备：

```
├── 自动数据采集（爬虫 → 清洗 → 摄入）
├── 大规模知识库（100+ 篇文档，5000+ 块）
├── 多策略检索（相似度 + MMR + 阈值过滤）
├── 量化评估（20 道标准题，检索命中率/答案准确率）
├── 流式回答（SSE 打字机效果）
├── 多轮对话（session_id 上下文记忆）
├── 稳定性（重试、降级、结构化日志、耗时追踪）
└── Docker 一键部署
```

---

## 3. 新目录结构

```
rag-api/
├── src/
│   ├── config.py              # 配置（已有，需扩展）
│   ├── rag/
│   │   ├── ingest.py          # 文档摄入（已有，需加批量/进度/校验）
│   │   ├── retrieve.py        # 检索（已有，需加 MMR/阈值过滤）
│   │   ├── generate.py        # 生成（已有，需加重试/流式）
│   │   └── evaluate.py        # ★ 新增：评估模块
│   ├── crawler/
│   │   ├── fetcher.py         # ★ 新增：HTTP 请求 + 反爬
│   │   ├── parser.py          # ★ 新增：HTML → Markdown
│   │   └── pipeline.py        # ★ 新增：抓取 → 清洗 → 摄入 全流程
│   ├── routes/
│   │   ├── chat.py            # 聊天路由（已有 routes.py，拆分）
│   │   ├── ingest.py          # 摄入路由
│   │   ├── crawl.py           # ★ 新增：爬虫触发接口
│   │   └── eval.py            # ★ 新增：评估接口
│   ├── session/
│   │   └── manager.py         # ★ 新增：多轮对话 session 管理
│   ├── middleware/
│   │   └── logging.py         # ★ 新增：请求日志 + 耗时追踪
│   ├── utils/
│   │   ├── retry.py           # ★ 新增：LLM 调用重试
│   │   └── metrics.py         # ★ 新增：检索/生成耗时统计
│   └── main.py                # 入口
├── scripts/
│   ├── crawler.py             # ★ 新增：独立爬虫脚本（批量抓取）
│   ├── eval.py                # ★ 新增：批量评估脚本
│   └── seed_docs.py           # ★ 新增：生成测试数据集
├── tests/
│   └── test_retrieve.py       # ★ 新增：检索单测
├── data/                      # ★ 新增：评估用测试集
│   └── test_questions.json    # 20 道标准问题 + 期望答案
├── docs/                      # 知识库文档
├── db/                        # ChromaDB 持久化
├── docker-compose.yml         # ★ 新增
├── Dockerfile                 # ★ 新增
└── README.md                  # ★ 新增：项目文档
```

---

## 4. 分阶段计划

### Phase 1：数据采集 — 爬虫（Day 1）

**目标**：爬取 100+ 篇中文技术文档，自动清洗入库

#### 1.1 基础爬虫
- `requests` + `BeautifulSoup` 抓取静态网页
- 支持两种源：FastAPI 官方中文文档 + 维基百科中文
- HTML → Markdown 清洗（去导航、广告、脚本）

#### 1.2 反爬应对
- User-Agent 轮换
- 请求间隔（time.sleep random 1-3s）
- 遇 403/429 自动等 retry-after

#### 1.3 数据清洗
- 去空行、去重复段落
- 中文标点统一
- 太短（< 50 字）的段落丢弃

#### 1.4 自动摄入
- 爬完一篇 → 存 docs/ → 触发单篇摄入
- 或：爬完全部 → 统一摄入
- 记录：源 URL、抓取时间

**交付**：`scripts/crawler.py` 能跑，拿到 100+ 篇文档

**撞的坑**：
- 网站结构不同，解析规则不同
- 某些页面反爬严格
- 大文档摄入耗时长
- 部分 HTML 解析失败

---

### Phase 2：检索诊断 + 改进（Day 2）

**目标**：检索结果肉眼可用——相关文档进 top-K，不相关的过滤掉

#### 2.0 检索诊断（先定位，再开药）
- 写 `scripts/diagnose_retrieval.py`：对单个问题打印 top-20 的排名、分数、来源
- 确认 `路径参数.md` 实际排第几、得分多少
- 对比 `all-MiniLM-L6-v2`（英文）和 `paraphrase-multilingual-MiniLM-L12-v2`（多语言）的检索排序差异
- 记录诊断结论，作为 Phase 3 评估的基线

#### 2.1 换 embedding 模型
- `all-MiniLM-L6-v2`（英文 384 维）→ `paraphrase-multilingual-MiniLM-L12-v2`（多语言 384 维）
- 维度相同所以 ChromaDB schema 不变，但**向量值全变，必须重新摄入**
- 模型约 420MB，第一次运行自动下载

#### 2.2 MMR 去重
- LangChain Chroma 有 `max_marginal_relevance_search(query, k, fetch_k, lambda_mult)`
- `fetch_k=20`：先从 20 个候选中选
- `lambda_mult=0.5`：平衡相关性和多样性（1=纯相关，0=纯多样）
- 效果：同一篇文档的相邻 chunk 不会霸占 top-K
- **如果 LangChain 的 MMR 方法不返回分数**，则手动实现 MMR 算法（numpy 余弦相似度 + 贪心选择）

#### 2.3 相似度阈值过滤
- `similarity_search_with_relevance_scores` 返回 (Document, score)
- score < `SIMILARITY_THRESHOLD`(默认 0.3) 的丢弃
- 全部被过滤时返回空列表，generate 层给友好提示："未找到相关内容"
- **阈值不是拍脑袋定的**：Phase 3 会做阈值 sweeep（0.2/0.3/0.4/0.5）看命中率变化

#### 2.4 来源校验
- ChatResponse 的 sources 改为 `[{"source": "...", "score": 0.xx}, ...]`
- 检索耗时统计：`retrieval_time_ms`
- 前端/终端能看到每个来源的可信度

**交付**：检索结果肉眼可区分相关/不相关，来源带分数

**撞的坑**：
- 英文 embedding 对中文无效（Phase 1 遗留，已定位）
- MMR 参数调优（lambda 太大→等于没开去重，太小→结果太分散）
- 阈值设高了漏召回、设低了一堆无关结果
- multilingual 模型比英文模型慢 2-3x，但还在可接受范围

---

### Phase 3：评估体系（Day 3）

**目标**：能说出"我的 RAG 检索命中率是 X%"

#### 3.1 设计测试集
- 从已摄入文档中，人工/半自动生成 20 道问题
- 每道题标记：正确答案所在的文档名
- 格式：`{"question": "...", "expected_source": "xxx.md", "answer_should_contain": "..."}`

#### 3.2 评估指标
- **检索命中率（Hit Rate）**：top-K 结果中，正确答案所在文档是否出现
- **MRR（Mean Reciprocal Rank）**：正确答案第一次出现的位置，越靠前越好
- **答案准确性**：LLM 回答是否包含预期关键词

#### 3.3 对比实验
- chunk_size = 500 vs 1000 vs 2000 的命中率对比
- 相似度检索 vs MMR 检索对比
- k = 3 vs 4 vs 6 的效果对比

**交付**：`scripts/eval.py`，跑一次输出对比表格

**撞的坑**：
- 不同 chunk_size 效果差很多
- MMR 不一定总比相似度好
- 写测试集本身就很痛苦

---

### Phase 4：企业级基础设施（今天）⭐⭐⭐

**目标**：把骨架从 Demo 升级到 Production——日志/异常/DI/中间件/流式

#### 4.1 结构化日志 + request_id 追踪
- 新建 `src/logging.py`：配置 JSON 格式日志，每行含 `timestamp`/`level`/`module`/`request_id`/`message`
- 新建 `src/middleware.py`：`RequestIDMiddleware` — 从 header `X-Request-ID` 取，没有则生成 `uuid.uuid4().hex[:8]`，注入到 `logging` 的 filter 和 response header
- **面试点**：为什么用 JSON 日志？→ ELK/Loki 可以直接索引，grep 也能搜。request_id 让一次请求的所有日志可串联。

#### 4.2 异常体系 + 全局 handler
- 新建 `src/exceptions.py`：
  - `RAGException(Exception)` — 基类
  - `RetrievalError` — 检索失败（ChromaDB 挂了）
  - `LLMError` — LLM 调用失败（重试耗尽）
  - `EmptyRetrievalError` — 未找到相关内容（不是错误，是正常结果）
- `main.py` 注册 `exception_handler`：所有 RAGException → 统一 `{"error": {"type": "...", "message": "..."}}` 响应
- **面试点**：异常分类让监控告警能区分——RetrievalError 是基础设施问题要告警，EmptyRetrievalError 是正常业务结果不需要告警。

#### 4.3 FastAPI Lifespan + 依赖注入
- 替换模块级单例（`_hf`、`_vectorstore`、`llm`）
- `main.py` 用 `@asynccontextmanager` 的 `lifespan`：
  - startup：初始化 embedding 模型 → 加载 ChromaDB → 创建 LLM 客户端 → 预热（跑一次空查询）
  - shutdown：`torch.cuda.empty_cache()` 释放显存
- 用 `Depends()` 注入到路由：`get_retriever()` / `get_generator()` / `get_config()`
- **面试点**：模块级单例在 import 时初始化，失败是整个进程炸；lifespan 在启动阶段失败可以优雅退出，且可以用 `/health` 的 readiness 探针控制流量切入时机。

#### 4.4 中间件链
- `src/middleware.py`：
  - `RequestIDMiddleware`：生成/传递 request_id
  - `TimingMiddleware`：记录每个请求的总耗时，加到 response header `X-Response-Time-Ms`
  - `LoggingMiddleware`：请求进来记 `method`/`path`/`status_code`/`duration_ms`
- FastAPI 的 `add_middleware` 顺序就是执行顺序：RequestID → Logging → Timing

#### 4.5 配置校验（pydantic-settings）
- `pip install pydantic-settings`
- `src/config.py` 改用 `BaseSettings`：自动校验类型、必填项、范围（如 `RETRIEVAL_K` 1-20）
- **面试点**：启动即校验，非法的值在服务启动时就报错，而不是运行到一半才发现

#### 4.6 结构化 ChatResponse + 耗时字段
- `ChatResponse`：`{answer, sources, request_id, timing: {retrieval_ms, llm_ms, total_ms}}`
- 生成 timing 在 middleware 自动算 total_ms，retrieval_ms/llm_ms 在 generate 内部记录

#### 4.7 SSE 流式输出 ⭐
- `POST /chat/stream`，`StreamingResponse` + async generator
- 事件流：`retrieval_done`（含 sources + retrieval_ms）→ `token` × N（逐字）→ `done`（含 llm_ms + request_id）
- 流式不做 LLM 重试（已经吐字了没法撤回），检索阶段异常 → `error` event

**交付**：对企业级骨架，面试官一眼看到你在生产环境写过代码

**撞的坑**（面试可讲）：
- 模块级单例导致启动失败无法优雅降级 → 换 lifespan
- 没有 request_id 的日志在并发场景下完全无法追踪 → 中间件注入
- pydantic-settings 能在启动时抓到配置错误，比运行时炸好得多
- SSE 流式不能用重试，异常处理策略和非流式完全不同

---

### Phase 5：混合检索 ⭐（明天）

**目标**：两级召回（BM25 关键词 + bge-large 语义）→ RRF 融合 → MMR 精排

#### 5.1 设计 Retriever 抽象基类
- `src/retriever.py` 定义 `class BaseRetriever(ABC)`:
  - `retrieve(query: str, top_k: int) -> List[RetrievalResult]` 抽象方法
- `VectorRetriever(BaseRetriever)` — 现在的手写 MMR 实现，重构进去
- `BM25Retriever(BaseRetriever)` — jieba + 手写 BM25，新建
- `HybridRetriever(BaseRetriever)` — 组合两个 retriever + RRF 融合
- **面试点**：策略模式 + 开闭原则，加新检索策略不改现有代码

#### 5.2 jieba BM25 检索器
- `src/bm25.py`：
  - 文件级索引（不是 chunk 级，chunk 太短文频信息太少）
  - 倒排索引：`{term: {doc_id: tf}}`
  - IDF 预计算：`log((N - df + 0.5) / (df + 0.5))`
  - 参数：k1=1.5（词频饱和度）, b=0.75（长度归一化）
  - top-30 返回文件名 + BM25 分数

#### 5.3 RRF 融合
- `src/rrf.py`：
  - `rrf_fuse(rankings: List[List[str]], k=60) -> List[Tuple[str, float]]`
  - 公式 `RRF(d) = Σ 1/(k + rank_i)`
  - 向量侧 chunk→文件映射取最高排名（最大池化）

#### 5.4 评估结果
- 25 题三方案对比：

| 方案 | Hit@K | MRR | 耗时 |
|------|:-----:|:----:|------|
| BM25 关键词 | 80% | 0.60 | 5ms |
| 纯向量 MMR | **96%** | **0.91** | 1238ms |
| 混合 RRF | 92% | 0.90 | 701ms |

- 混合没赢纯向量的原因：文档集同质化（全是 FastAPI 中文），bge-large 语义覆盖已足够好
- 混合检索的价值在文档种类变多时显现（API 文档 + Wiki + PDF 论文）
- **面试说**：数据驱动决策，当前数据集纯向量更强，但保留混合架构供未来扩展

**交付**：混合检索上线，评估数据支撑，面试可以画两张召回曲线图

**撞的坑**：
- BM25 文件级 vs chunk 级的选择——chunk 太短文频稀疏，文件级粗筛+向量精排效果更好
- RRF 为什么不用分数融合——BM25 和余弦相似度量纲不同，排名融合更鲁棒
- jieba 对技术术语分词粒度——"路径参数"可能被切成"路径"+"参数"，需自定义词典

---

### Phase 6：部署 + 测试（后天）

**目标**：Docker 一键部署 + 健康检查 + 核心单测

#### 6.1 健康检查（真实依赖探测）
- `/health` 返回所有组件状态：
  ```json
  {"status": "healthy", "components": {
    "chromadb": "ok",
    "embedding_model": "ok", 
    "llm": "ok"
  }}
  ```
- 任何组件不健康 → HTTP 503
- Kubernetes 可用做 liveness/readiness probe

#### 6.2 核心单测
- `tests/test_bm25.py`：分词 + BM25 分数计算
- `tests/test_retrieve.py`：用 fixture 注入小 ChromaDB，测 MMR + 阈值 + 去重
- `tests/test_generate.py`：mock LLM，测 prompt 拼接 + 降级逻辑
- `tests/test_rrf.py`：排名融合边界（空列表、单列表、带重叠）

#### 6.3 Docker
- `Dockerfile`：Python 3.12-slim，多阶段构建分离依赖安装和代码复制
- `docker-compose.yml`：单服务 + ChromaDB 持久化卷 + env 文件
- `.dockerignore`：排除 venv/__pycache__/大模型缓存

#### 6.4 README
- 架构 ASCII 图
- 快速开始 3 行命令
- API 文档（/chat, /chat/stream, /ingest, /health）
- 评估结果一览
- 面试 FAQ 链接

**交付**：`docker compose up` → 服务可用，`pytest` 通过

---

### Phase 7：意图路由 + 缓存 + 多库路由

**目标**：知道什么时候 RAG / 什么时候 LLM 直答 / 什么时候调 tool / 什么时候不搜

#### 7.1 三层意图路由（generator.py 入口处判断）

```
用户问题
  │
  ├─ 触发词命中（"帮我查"/"搜索"）→ tool
  │     └─ function calling → 返回工具结果
  │
  ├─ 闲聊/常识（"你好"/"Python 是什么"）→ direct
  │     └─ LLM 直答，不走检索，无上下文
  │
  └─ 知识库问题 → RAG
        └─ 检索 + LLM 回答
```

规则判断逻辑（30 行，不用模型）：
1. tool 触发词词典：`["帮我查", "搜索一下", "计算"]`
2. 闲聊判断：不含技术关键词 + 短句（<15 字）+ 无问号
3. 默认 → RAG

#### 7.2 多 collection 路由（多库场景）

```
问题进来
  │
  ├─ "FastAPI 路径参数"  → 关键词 "fastapi" → collection: "fastapi_docs"
  ├─ "React hooks"       → 关键词 "react"   → collection: "react_docs"
  ├─ "公司报销流程"       → 没命中任一库     → 搜所有库，按分数取 top
  └─ "你好"              → 闲聊             → 不搜
```

config 里配置映射表：

```python
COLLECTION_ROUTES = {
    "fastapi": ["fastapi", "路径参数", "Depends", "中间件", ...],
    "react":   ["react", "hooks", "useState", "useEffect", ...],
    "company": ["报销", "请假", "入职", ...],
}
```

**面试点**：规则路由 vs 模型路由——规则不耗延迟，覆盖 95% 场景，剩下 5% 兜底搜全库

#### 7.3 请求校验
- `ChatRequest` 加 `max_length=500`、`min_length=1`
- 空问题/超长问题 → 422

#### 7.4 LRU 检索缓存
- `functools.lru_cache(maxsize=128)` 缓存检索结果
- 只缓存检索不缓存 LLM 回答——检索确定/LLM 有随机性

**交付**：智能路由 + 多库 + 缓存，面试可讲完整意图分发链路

---

### Phase 8：简历 + 面试稿

- 两页简历（rag-api + agent-playground 合并）
- 5 分钟演示流程（含 curl 命令 + 预期输出）
- 面试 10 问模拟（含为什么清单）

---

## 5. 面试"为什么"清单

| # | 问题 | 答案预备 |
|---|------|---------|
| 1 | 为什么用 bge-large？ | 从 all-MiniLM(英文) → bge-small(100MB) → bge-large(1.3G)，中英文模型语义区分度差距巨大，诊断数据支撑 |
| 2 | 为什么用 MMR？ | 朴素相似度返回同一文档多个 chunk，MMR 去重 + 同源过滤保证多样性，评估数据：Hit 90%→95% |
| 3 | 为什么直取 ChromaDB 向量？ | LangChain 包装层多一次 embed_documents 调用，`_collection.query()` 直出预存向量，速度快 2x |
| 4 | 为什么用混合检索？ | 纯向量对专有名词/精确匹配不敏感（如 "FastAPI" 可能匹配不到），BM25 关键词互补 |
| 5 | RAG 翻过什么车？ | ① 英文模型做中文=假RAG(答案对来源错) ② `_collection.query` L2 排序和余弦打架 ③ 阈值方向反了过滤掉正确答案 |
| 6 | 为什么不用 Pinecone/Milvus？ | ChromaDB 单机够用，HNSW 索引 O(logN) 对百万级以内足够 |
| 7 | LLM 失败怎么办？ | 3 次重试 + 指数退避，超限返回友好降级提示；流式模式用 error event 降级 |
| 8 | 怎么评估检索质量？ | 20 题测试集 + Hit@K/MRR，每次改参数跑 A/B 对比，用数据决策 |
| 9 | 为什么用 jieba 不用 ES？ | 轻量无外部依赖，面试可讲 BM25 公式细节 |
| 10 | 为什么用 lifespan 管理资源？ | 模块级单例在 import 时初始化，失败是整个进程炸；lifespan 启动阶段失败可优雅退出，配合 k8s readiness probe 控制流量切入 |
| 11 | 日志怎么设计的？ | JSON 结构化日志 + request_id 全链路追踪，ELK/Loki 可索引，grep 也能搜；中间件自动注入 request_id 到 logging context |
| 12 | 异常怎么分层的？ | RAGException 基类 → RetrievalError/LLMError/EmptyRetrievalError，全局 handler 统一响应格式，监控按异常类型区分告警级别 |

## 6. 不做的事情

- ❌ Redis — 流量不够，lru_cache 够用
- ❌ 前端界面 — 纯后端项目，curl/Postman 演示
- ❌ 多轮对话 — 时间换混合检索和流式（更值）
- ❌ Pinecone/Milvus — ChromaDB 单机够用，百万级以内 HNSW O(logN) 足够
- ❌ ES — jieba 手写 BM25 更轻量，且面试能讲算法
