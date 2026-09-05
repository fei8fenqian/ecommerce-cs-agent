# E-Commerce-Agent

一个面向 3C 电商场景的 LLM 电商客服 Agent。

项目包含前后端交互，覆盖商品检索与推荐、购物车、Checkout、银联测试支付、订单查询和退款客服等流程。用户可以直接用自然语言完成“这台”“刚才那个”“我想换一笔”“帮我看看退款成功没”等多轮交互，系统再将这些表达关联到真实商品、订单和业务状态。

主要业务链路：

```text
商品推荐 → 商品详情 → 购物车 / Checkout → 支付 → 订单 → 客服退款
```

---

## 主要能力

### 售前导购

- 笔记本、手机、配件 Catalog；
- 混合 RAG：向量检索 + BM25 → RRF 融合 → Rerank 精排；
- 支持预算、性能、CPU / GPU、拍照、游戏、学习、便携等自然语言需求；
- ProductResolver 在当前真实商品候选中完成推荐排序和指代理解；
- 支持“第二个”“刚才那几个”“对应链接呢”等多轮追问；
- 商品详情链接和购买动作与真实 Catalog 商品绑定。


### 交易协助

- 购物车与 Checkout；
- 订单创建；
- 银联测试环境支付；
- 支付状态查询；
- 支付 / 退款状态由业务层和外部业务接口维护，不依赖聊天历史判断。


### 售后服务

- 登录用户订单查询；
- 多订单候选与结构化选择；
- 支持“刚买的华为”“另一台”“刚才那笔”等自然语言订单指代；
- SupportCase 保存当前客服上下文；
- 退款资格查询、退款入口和退款状态查询；
- 设备故障会先进行安全排查，明确报修、存在风险或排查无效时可创建售后工单；
- 工单可进入人工客服队列，由客服在工作台中认领、回复和关闭；
- 支持退款对象纠正和多轮连续对话，例如：

```text
我想退刚买的华为
→ 多笔华为订单，选择其中一笔
→ 我搞错了，想退的是 iPhone
→ 重新查找 iPhone 订单
→ 我已经申请退款了，看看成功没
→ 查询当前退款状态
```


### 工程能力

- FastAPI `/chat` 与 `/chat/stream`；
- SSE 流式输出；
- React + TypeScript 前端；
- Redis 多轮 Session；
- PostgreSQL + pgvector；
- ToolRegistry 与 ToolContext；
- JWT + Casbin 权限控制；
- 限流、熔断、日志脱敏；
- request-id、指标监控和客服调用链记录；
- pytest、ruff、mypy；
- JDDC / 自建客服测试和真实浏览器端到端测试。

### 内部协作

- 售后工单支持 AI 处理与人工接管；需要人工介入时进入客服工作台队列；
- 飞书用于值班通知和协作入口，发送脱敏工单卡片，真实工单状态仍以本系统为准；
- 财务侧提供退款审批 / 驳回、退款状态刷新、资金异常扫描和 Agent 核查摘要，资金状态变更仍由受控业务服务执行。

---

## 整体架构

项目采用模块化单体结构。对外始终是一个电商客服 Agent，内部按照售前导购、交易协助和售后服务三类业务能力组织。面向用户的 LLM 在项目中作为 Operator，负责理解问题、做业务层面的选择与 Tool 调用决策；后端的 Policy / Control Plane 则负责限定当前允许使用的能力、需要核验的事实和状态条件。

```text
                         React 前端
                             │
                             ▼
                     FastAPI / Chat API
                             │
                             ▼
                       电商客服 Agent
                             │
                             ▼
                  Operator（LLM）
               理解用户 / 决策 / 调用 Tool
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
          ▼                  ▼                  ▼
       售前导购            交易协助            售后服务
   商品搜索 / 推荐      购物车 / Checkout     订单 / 退款
   商品比较 / 指代      下单 / 支付            检修 / 工单
          │                  │                  │
          └──────────────────┼──────────────────┘
                             ▼
                 Policy / Control Plane
                能力授权 / 必需事实 / 状态条件
                             │
                             ▼
                     ToolRegistry / Tools
                             │
                             ▼
                    业务 Service / Store
                             │
          ┌──────────────────┼──────────────────┐
          ▼                  ▼                  ▼
      PostgreSQL           Redis          支付 / 退款接口
                             │
                             ▼
                   已核验的业务事实与状态
                             │
                             ▼
                 Validator / CustomerResponse
                             │
                             ▼
                  Operator 组织最终说明
                             │
                             ▼
                    Chat / SSE / 页面动作
```

后端校验并不是图中的单独一步，而是贯穿 Tool 调用前后的安全边界，包括当前用户身份、候选对象绑定、权限、状态机和真实业务参数校验。

主要模块：

| 模块 | 主要作用 |
|---|---|
| `engines/loop.py` | Operator 的 Tool Calling 循环，负责理解问题、决策和自然语言回复 |
| `intent_router.py` | 判断当前请求属于哪类业务，并提供结构化业务语义 |
| `support_command.py` | 把客服中的改口、换对象、取消等语义整理成结构化指令 |
| `support_control.py` | 定义客服能力、必需事实、授权范围和完成条件 |
| `support_workflow.py` | 根据当前 Policy 读取必要事实、执行受控 Tool 调用并汇总已核验结果 |
| `product_resolver.py` | 在当前商品候选中做推荐排序和商品指代 |
| `product_context.py` | 保存当前商品候选和已选择商品 |
| `support_case.py` / Case Service | 保存客服会话中的当前业务对象和上下文 |
| `order_subject_resolver.py` | 在当前用户真实订单候选中找到用户说的那一笔 |
| `tools_registry.py` | 统一管理 Tool 调用，并通过 ToolContext 约束身份、权限和已绑定对象 |
| `customer_response.py` | 最终响应校验与安全边界，阻止未经核验的业务事实进入客户回复 |
| `service/` | 订单、支付、退款、售后等业务服务和状态机 |
| `store/` | PostgreSQL 数据访问 |
| `infra/` | Redis、支付、飞书通知、权限、熔断等基础设施 |

---

## 商品推荐是怎样完成的

例如用户说：

> 5000 元左右，想要一台适合学习和偶尔玩游戏的笔记本。

大致流程：

```text
用户自然语言
    ↓
识别商品类别和明确条件
    ↓
Catalog / RAG 找到当前真实商品
    ↓
形成一组商品候选
    ↓
ProductResolver 根据用户偏好在候选中排序
    ↓
绑定真实 product_id / variant
    ↓
生成商品详情动作
    ↓
LLM 组织自然语言推荐
```

价格、类别这类明确条件可以直接用于商品过滤；“性能更好”“适合游戏”“便携一些”这类开放需求则交给 LLM 结合当前商品信息判断。

推荐结果和用户真正选中的商品是分开的。Agent 可以同时推荐多款商品，但只有用户明确选择后，才会把某一款保存为当前选中商品，供后续“第二个”“这台”“对应链接呢”等对话继续使用。

---

## 一条退款请求是怎样完成的

例如用户说：

> 我想把刚买的华为退了。

简化来看，客服侧的控制关系是：

```text
用户自然语言
    ↓
Operator 理解与决策
“申请退款，目标是刚买的华为”
    ↓
Policy / Control Plane
确定当前允许的能力、需要核验的事实和状态条件
    ↓
Tool 获取真实订单 / 退款事实
    ↓
后端校验与状态约束
确认用户身份、订单候选、真实 order_id 和当前业务状态
    ↓
Validator
检查最终回答中的业务事实和页面动作是否有可靠依据
    ↓
Operator
把已核验结果解释成自然语言
```

其中 `SupportWorkflow` 更接近这条链路中的**受控事实读取与执行层**：它根据 Policy 补齐当前目标需要的事实、执行能够确定参数的 Tool 调用，并把结果交回 Operator，而不是代替 Operator 理解用户或固定地推进整段对话。

如果只有一笔符合条件，可以继续处理；如果同时有多笔符合条件，则展示真实订单让用户选择。

这里的“候选编号”只是后端在当前一轮临时生成的编号，例如：

```text
order_candidate_1 → 某笔华为订单
order_candidate_2 → 另一笔华为订单
```

LLM 可以判断用户指的是 `order_candidate_1`，真正的 `order_id` 仍由后端根据当前候选表映射。

---

## Tool 调用

Operator 可以根据当前问题调用订单、支付、退款等 Tool；在客服场景中，Policy / Control Plane 会先限定当前允许使用的能力和必须核验的事实。例如：

```text
track_order
query_refund_status
check_refund_eligibility
check_payment_status
```

除了 LLM 生成的 Tool 参数，后端还会注入 `ToolContext`：

```text
user_id
role
selected_order_id
允许 / 禁止调用的 Tool
商品候选编号
```

例如 `track_order` 查询订单时，真正使用的是当前登录用户的 `user_id`，而不是让 LLM 自己填写用户身份。

如果当前客服上下文已经确定某笔订单，后续退款状态查询也会继续使用这笔已确认订单。这样 Tool Calling 可以保留 Agent 的灵活性，同时继续复用传统后端已有的认证、权限和业务校验。

---

## 多轮上下文

项目会区分“当前在谈什么”和“当前业务事实是什么”。

Session / SupportCase 可以保存：

```text
当前讨论的订单
当前选中的商品
上一轮商品候选
当前客服任务
待用户选择的订单
```

这些信息用于理解：

```text
“这台”
“另一笔”
“刚才那个”
“继续”
```

订单状态、支付状态、退款状态、库存等可能随时间变化的数据，则在真正需要回答时重新查询 Tool / 数据库 / 外部业务接口。

---

## 项目结构

```text
src/
├── agent/
│   ├── llm/
│   │   ├── intent_router.py          # 请求意图识别
│   │   ├── support_command.py        # 客服语义结构化
│   │   └── llm_client.py             # LLM 调用
│   │
│   ├── engines/
│   │   ├── loop.py                   # Operator 的 Tool Calling 循环
│   │   ├── support_workflow.py       # 客服受控事实读取与执行
│   │   └── plan_execute.py
│   │
│   ├── product_context.py            # 商品候选与当前选择
│   ├── product_resolver.py           # 商品推荐排序 / 指代理解
│   ├── order_subject_resolver.py     # 订单指代与候选绑定
│   ├── support_command_runtime.py    # 执行客服结构化指令
│   ├── support_control.py            # 客服能力授权 / 必需事实 / 状态条件
│   ├── customer_response.py          # 最终回复校验与展示
│   ├── tools_registry.py             # Tool 注册与调用约束
│   ├── tools/                        # Agent 可调用的 Tool
│   └── rag/                          # 商品 / 知识检索
│
├── api/                              # FastAPI 接口
├── service/                          # 订单 / 支付 / 退款等业务服务
├── store/                            # PostgreSQL 数据访问
├── infra/                            # Redis / 支付 / 飞书通知 / Casbin / 熔断
├── middleware/
└── main.py

web/                                  # React + TypeScript + Vite
scripts/                              # 数据导入 / 评测 / JDDC / 冒烟测试
tests/                                # 单元 / 契约 / 集成 / 回归测试
docs/                                 # 架构 / 运维 / 评测文档
```

---

## 技术栈

| 领域 | 技术 |
|---|---|
| 后端 | Python 3.12, FastAPI, Pydantic |
| 前端 | React, TypeScript, Vite |
| LLM | DeepSeek / OpenAI 兼容接口 |
| Agent | Tool Calling、自定义 Agent Loop、LangGraph |
| RAG | pgvector, BGE Embedding, BM25, RRF, BGE Reranker |
| 数据库 | PostgreSQL + pgvector |
| 会话 | Redis |
| 认证 / 权限 | JWT, Casbin |
| 支付 | 银联测试环境 |
| 测试 | pytest |
| 代码质量 | ruff, mypy |

---

## 本地运行

### 环境要求

- Python 3.12+
- Node.js 20+
- PostgreSQL 16 + pgvector
- Redis 7+
- OpenAI 兼容的 LLM API

### 1. 后端

```bash
git clone https://github.com/fei8fenqian/ecommerce-cs-agent.git
cd E-Commerce-Agent

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

至少配置：

```env
PG_HOST=localhost
PG_PORT=5433
PG_USER=postgres
PG_PASSWORD=...
PG_DBNAME=...
REDIS_URL=redis://localhost:6379/0

LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
```

生成 JWT key：

```bash
ssh-keygen -t rsa -b 2048 -m PEM -N "" -f private_key.pem
openssl rsa -in private_key.pem -pubout -out public_key.pem
```

启动：

```bash
uvicorn main:app --app-dir src --reload --host 127.0.0.1 --port 8000
```

### 2. 数据库与商品数据

当前 schema 参考：

```text
current-schema.sql
```

导入商品和知识库数据：

```bash
make ingest
```

### 3. 前端

```bash
cd web
npm install
npm run dev
```

浏览器访问：

```text
http://127.0.0.1:5173
```

Vite 默认代理：

```text
/api    → http://127.0.0.1:8000
/health → http://127.0.0.1:8000
```

---

## 容器化运行

仓库提供 Docker Compose 配置，用于启动后端及相关依赖。启动前需要准备环境变量、数据库连接和 LLM API 配置。

---

## 测试与评测

```bash
make test
make lint
make eval
```

客服相关评测脚本包括：

```text
scripts/run_jddc_customer_support_eval.py
scripts/run_jddc_refund_intent_ab.py
scripts/run_customer_support_real_eval.py
scripts/run_support_reasoning_trace_eval.py
scripts/run_realistic_execution_benchmark_v1.py
```

除单元和集成测试外，项目还会通过真实 `/chat`、`/chat/stream` 和浏览器多轮端到端测试检查 Session、商品 / 订单候选、Tool 返回结果以及最终 UI 是否保持一致。

---

## 扩展方式

增加新的客服能力时，尽量复用已有的自然语言理解、用户身份、订单绑定和 Tool 调用链。

例如：

```text
物流查询
→ 增加对应的物流 Tool / 数据接口

设备检修
→ Agent 先安全排查，必要时创建工单并进入人工客服协作链路

售后状态
→ 增加售后 Tool 和业务服务

完整换货
→ 在现有订单识别基础上增加换货业务流程和状态机
```

新增能力沿用现有的用户上下文、订单绑定、Policy 和 Tool 调用链，在对应业务层补充数据接口或业务流程即可。

---

## 文档

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 项目架构
- [`docs/README.md`](docs/README.md) — 文档索引
- [`docs/runbooks/`](docs/runbooks/) — 数据库迁移 / 发布 / 恢复
- [`docs/evaluation/`](docs/evaluation/) — 评测设计

---

## 开源协议

MIT
