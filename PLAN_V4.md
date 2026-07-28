# PLAN V4 — 配机助手 + Plan-and-Execute + MCP 集成

> **核心命题**：不是为用而用 LangGraph/MCP，而是用一个真正需要它们的业务场景来驱动架构演进。
>
> **两大场景**：
> 1. "5000 预算，帮我配台打 3A 的主机" — 约束满足 + 兼容性验证 + 并行搜索 + 回退重规划，ReAct while-loop 做不了
> 2. "我要退款" — 支付/退款是独立系统，天然适合 MCP 协议分离

---

## 零、为什么 V3 不够

| V3 架构 | 问题 | 体现 |
|---------|------|------|
| while-loop ReAct | 无规划，边走边看 | 漏搜电源，配置不完整 |
| 串行工具调用 | 独立搜索互相等待 | CPU 和显卡搜索无依赖但串行 |
| 无验证+回退 | 答了就答了，不自检 | DDR5 内存配 B550 主板不报错 |
| 所有工具同一进程 | 支付/退款不该跟 Agent 代码库耦合 | 支付是独立有状态服务 |
| PG 每调 connect+close | 无连接池 | 配一次机 6-8 次 DB 连接 |

**V4 不是重构 V3，是在 V3 基础上增加两条新链路：Plan-and-Execute 配机 + MCP 支付服务。**

---

## 已完成 (V3 + SSE)

| 功能 | 状态 |
|------|:----:|
| AgentLoop (ReAct) + 5 个工具 | ✅ |
| 意图路由 (rag/agent/ticket) | ✅ |
| 配件数据 (9 品类 1427 条) + search_component | ✅ |
| 检索 pipeline (Hybrid+RRF+Rerank, Hit@1 94.7%) | ✅ |
| SSE 流式输出 + 多轮对话 + 指代消解 | ✅ |
| Prompt injection 三层防护 | ✅ |
| 连接池 (db_pool) | ✅ |
| BM25 懒加载 | ✅ |
| eval 框架 + 75 道测试题 | ✅ |

---

## 一、架构全景

```
用户 query
    │
    ▼
┌──────────────┐
│ IntentRouter │  ← 扩展: 新增 "plan_execute" 意图
│ rag/agent/   │
│ ticket/      │
│ plan_execute │
└──┬───┬───┬──┘
   │   │   │
   ▼   ▼   ▼
 RAG  │  Plan-and-Execute  ← NEW: LangGraph 配机
      │       │
      │   ┌───┴────┐
      │   │LangGraph│  planner → executor → verifier → (replan)
      │   └───┬────┘
      │       │
      │   ┌───┴────────────────────┐
      │   │ Tool Calls             │
      │   │ ├─ search_component    │  NEW: PG 本地
      │   │ ├─ check_compatibility │  NEW: 本地规则引擎
      │   │ ├─ search_product      │  V3
      │   │ ├─ check_stock         │  V3
      │   │ ├─ track_order         │  V3
      │   │ ├─ create_ticket       │  V3
      │   │ ├─ check_payment    ◀──│── NEW: MCP 远程
      │   │ └─ request_refund   ◀──│── NEW: MCP 远程
      │   └────────────────────────┘
      │
      ▼
   AgentLoop (ReAct)  ← V3 保留不动，处理简单查询 / 支付退款
```

**核心原则**：
- V3 的 `AgentLoop` (ReAct) **不动**，简单查询走快路径
- 新增 `PlanAndExecuteAgent`，配机走 LangGraph
- 新增 MCP Payment Server，支付/退款独立进程
- `IntentRouter` 加 `plan_execute` 意图，路由分叉

---

## 二、数据层：配件库

### 2.1 爬取范围

ZOL DIY 频道 (`detail.zol.com.cn`)，8 个品类，每品类 2-3 个品牌，约 80-120 条数据：

| 品类 | ZOL 路径 | 品牌 | 关键兼容性字段 |
|------|---------|------|--------------|
| CPU | `/cpu/` | Intel, AMD | 插槽, 核心数, TDP, 支持内存 |
| 主板 | `/motherboard/` | 华硕, 微星, 技嘉 | 芯片组, 插槽, 内存类型, 板型 |
| 显卡 | `/vga/` | 七彩虹, 华硕, 微星 | 芯片, 显存, 功耗, 长度 |
| 内存 | `/memory/` | 金士顿, 芝奇 | 类型(DDR4/5), 频率, 容量 |
| SSD | `/solid_state_drive/` | 三星, 西数 | 接口, 容量, 读取速度 |
| 电源 | `/power/` | 海韵, 振华 | 额定功率, 模组, 尺寸 |
| 机箱 | `/case/` | 联力, 追风者 | 板型支持, 显卡限长, 电源限长 |
| 散热器 | `/cooling/` | 利民, 九州风神 | 插槽支持, 高度, 散热方式 |

### 2.2 数据库设计

一张统一表 + JSONB 存品类特有字段（复用 V3 laptop_products 模式）：

```sql
CREATE TABLE components (
    id          VARCHAR(128) PRIMARY KEY,
    product_name VARCHAR(512),
    brand       VARCHAR(64),
    price       NUMERIC,
    category    VARCHAR(32),   -- 'cpu','motherboard','gpu','ram','ssd','psu','case','cooler'
    description TEXT,
    embedding   VECTOR(1024),
    metadata    JSONB,         -- 品类特有字段: socket, tdp, ddr_type, wattage, etc.
    stock       INT DEFAULT 0,
    warehouse   VARCHAR(64)
);
CREATE INDEX ON components USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON components (category);
```

品类字段示例（存 JSONB metadata）：

| 品类 | metadata 关键 key |
|------|------------------|
| CPU | `socket`, `cores`, `threads`, `base_freq`, `turbo_freq`, `tdp`, `mem_type` |
| 主板 | `chipset`, `socket`, `mem_type`, `max_mem`, `form_factor` |
| 显卡 | `chip`, `vram`, `core_freq`, `power_draw`, `length_mm` |
| 内存 | `ddr_type`, `freq`, `capacity`, `timing` |
| SSD | `interface`, `capacity`, `read_speed`, `write_speed` |
| 电源 | `wattage`, `certification`, `modular_type` |
| 机箱 | `mb_form_factors`, `gpu_max_length`, `psu_max_length`, `cooler_max_height` |
| 散热器 | `socket_support`, `height_mm`, `cooling_type` |

### 2.3 数据脚本

完全复用 V3 模式，3 个脚本：

| 脚本 | 作用 | 参考模板 |
|------|------|---------|
| `scripts/crawl_components.py` | Playwright 爬 ZOL DIY | `crawl_phones.py` |
| `scripts/clean_components.py` | 按品类标准化字段 | `clean_products.py` |
| `scripts/ingest_components.py` | pgvector 入库 | `ingest_laptops.py` |

爬虫调整点：
- ZOL DIY 类目的 URL 模式和手机不完全相同
- 每个品类一个品牌列表，不像手机按品牌分子频道
- 参数表结构类似（`div.detailed-parameters > table > tr`）

---

## 三、工具层：新增 + 保留

### 3.1 新增工具

| # | 工具 | 数据源 | 类型 | 说明 |
|---|------|--------|------|------|
| 6 | `search_component` | PG components 表 | 检索+过滤型 | `search_component(category, **filters)` — 按品类+条件搜索 |
| 7 | `check_compatibility` | MCP Compat Server | MCP 远程工具 | 验证组件兼容性，返回冲突列表 |

### 3.2 `search_component` (本地 PG 工具)

```python
class SearchComponent(BaseTool):
    name = "search_component"
    description = "搜索电脑配件。category 必填：cpu/motherboard/gpu/ram/ssd/psu/case/cooler"
    parameters = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["cpu", "motherboard", "gpu", "ram", "ssd", "psu", "case", "cooler"],
                "description": "配件品类",
            },
            "keyword": {"type": "string", "description": "搜索关键词，如'5600X''B650'"},
            "price_min": {"type": "number", "description": "最低价格"},
            "price_max": {"type": "number", "description": "最高价格"},
            "socket": {"type": "string", "description": "CPU插槽，如'AM5''LGA1700'"},
            "ddr_type": {"type": "string", "description": "内存类型，'DDR4'或'DDR5'"},
        },
        "required": ["category"],
    }
```

实现：PG `SELECT * FROM components WHERE category=%s AND ...` + 向量相似检索。

### 3.3 `check_compatibility` (本地规则引擎)

直接在 Agent 进程内跑，不需要 MCP——兼容性规则是对 PG 配件数据的校验，和 Agent 在同一代码库。

```python
class CheckCompatibility(BaseTool):
    name = "check_compatibility"
    description = "验证电脑配件兼容性"
    parameters = {
        "type": "object",
        "properties": {
            "components": {"type": "array", "items": {"type": "object"}}
        },
        "required": ["components"],
    }
    
    def execute(self, components: list[dict]) -> ToolResult:
        conflicts = []
        for rule in COMPAT_RULES:
            if not rule.check(components):
                conflicts.append(rule.fail_msg)
        return ToolResult(name=self.name, status="success",
                          data={"ok": len(conflicts)==0, "conflicts": conflicts})
```

### 3.4 `check_payment` + `request_refund` (MCP 远程工具)

见第四节 MCP 集成。

---

## 四、MCP 集成 — 支付服务

### 4.1 为什么支付适合 MCP

支付/退款在真实公司是**独立有状态服务**——微信支付回调、退款对账、资金安全，通常由财务/支付团队维护，语言可能是 Java/Go。Agent 不应该直接操作支付数据库。

| 特征 | 说明 |
|------|------|
| 独立团队 | 支付和客服 Agent 不在一个代码库 |
| 跨语言 | 支付系统大概率 Java/Go，Agent 是 Python |
| 安全隔离 | 退款操作不能直接暴露给 Agent，MCP Server 做权限校验 |
| 标准化 | 如果微信支付官方出 MCP Server，Agent 直接接 |

### 4.2 MCP Payment Server (`services/mcp_payment_server.py`)

独立进程，FastMCP + stdio transport。暴露两个工具：

| 工具 | 类型 | 说明 |
|------|------|------|
| `check_payment(order_id)` | 读 | 查 orders 表：支付方式、金额、时间、状态 |
| `request_refund(order_id, reason)` | 写 | 校验退款资格 → update status='已取消' → 返回退款单号 |

**退款资格规则**：只有 `运输中` / `已签收` 的订单可退款；`待付款` / `已完成` / 已退款 不可退。

实现要点：
- 自己 `init_pool()` 建 PG 连接（独立进程，不共享 main.py 的池）
- `@mcp.tool()` 装饰器定义工具，`mcp.run(transport="stdio")` 启动
- 未来替换真实 API：把 PG 查询换成 `requests.get("https://api.mch.weixin.qq.com/...")`

### 4.3 `MCPTool(BaseTool)` 适配器 (`src/agent/mcp_tool.py`)

核心问题：`BaseTool.execute()` 是 sync，MCP 调用是 async → `asyncio.run()` 桥接。

```python
class MCPTool(BaseTool):
    def __init__(self, tool_def, session):
        self._name = tool_def.name          # MCP 工具名
        self._description = tool_def.description
        self._parameters = tool_def.inputSchema  # JSON Schema
        self._session = session

    def execute(self, **kwargs) -> ToolResult:
        try:
            result = asyncio.run(
                self._session.call_tool(self._name, arguments=kwargs)
            )
            return ToolResult(name=self.name, status="success",
                              data={"result": result.content[0].text})
        except Exception as e:
            return ToolResult(name=self.name, status="error", error=str(e))
```

`MCPClientManager` 管理连接生命周期：
```python
class MCPClientManager:
    def __init__(self, server_params: StdioServerParameters):
        self._params = server_params
    
    async def connect(self) -> ClientSession:
        # stdio_client(server_params) → read_stream, write_stream
        # ClientSession(read_stream, write_stream) → session.initialize()
    
    async def disconnect(self):
        # 关闭 transport
```

### 4.4 main.py lifespan 集成

```python
# startup
mcp_payment = MCPClientManager(StdioServerParameters(
    command="python", args=["services/mcp_payment_server.py"]
))
payment_session = await mcp_payment.connect()

# 自动发现工具 → 注册进 Registry
tools_result = await payment_session.list_tools()
for tool_def in tools_result.tools:
    registry.register(MCPTool(tool_def, payment_session))

# shutdown
await mcp_payment.disconnect()
```

Agent Loop 完全无感——`check_payment` / `request_refund` 跟 `check_stock` 调用方式一模一样。

---

## 五、LangGraph Plan-and-Execute Agent

### 5.1 为什么不替换 `AgentLoop`

- 简单查询（"查库存""退货流程"）不需要 Plan-and-Execute，反而会变慢
- `AgentLoop` 已经稳定、有测试覆盖
- 新增 `PlanAndExecuteAgent` 类，两个 Agent 共存，`IntentRouter` 选路

### 5.2 图结构

```
                    ┌──────────────────────┐
                    │      planner          │
                    │  LLM: 需求 → 结构化计划 │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │      executor         │
                    │  并行执行无依赖步骤    │
                    │  ┌────┐ ┌────┐       │
                    │  │CPU │ │GPU │  ...  │
                    │  └──┬─┘ └──┬─┘       │
                    │     │      │          │
                    │  ┌──▼──────▼──┐       │
                    │  │ 依赖步骤    │       │
                    │  │ 主板(等CPU) │       │
                    │  └────────────┘       │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │      verifier         │
                    │  check_compatibility  │
                    │  + 预算检查           │
                    └──────┬──────┬────────┘
                           │      │
                    ┌──────▼─┐ ┌──▼──────┐
                    │ 通过    │ │ 未通过  │
                    └──────┬─┘ └──┬──────┘
                           │      │
                    ┌──────▼─┐ ┌──▼──────────┐
                    │formatter│ │  replanner   │
                    │ 格式输出│ │ 分析冲突原因  │
                    └────────┘ │ 调整方案      │
                               └──┬───────────┘
                                  │
                                  ▼
                              executor (重新执行)
```

### 5.3 State 定义

```python
class BuildState(TypedDict, total=False):
    messages: list[dict[str, Any]]     # 对话历史
    requirements: str                  # 用户需求原文
    budget: int                        # 预算上限（从 requirements 提取）
    plan: list[dict]                   # 结构化计划步骤
    selected_parts: dict[str, dict]    # {category: component} 已选配件
    compatibility_result: dict         # 兼容性检查结果
    iteration: int                     # 重试次数
    answer: str                        # 最终输出
    total_tokens: int
```

### 5.4 四个节点

#### planner 节点
- 输入：`requirements`（用户原文）
- LLM 生成结构化计划 JSON：
  ```json
  {
    "budget": 5000,
    "strategy": "游戏为主，CPU 和 GPU 占总预算 60%",
    "steps": [
      {"id": 1, "category": "cpu", "filters": {"price_max": 1200}, "depends_on": []},
      {"id": 2, "category": "gpu", "filters": {"price_max": 2000}, "depends_on": []},
      {"id": 3, "category": "motherboard", "filters": {}, "depends_on": [1]},
      {"id": 4, "category": "ram", "filters": {}, "depends_on": [3]},
      {"id": 5, "category": "ssd", "filters": {}, "depends_on": []},
      {"id": 6, "category": "psu", "filters": {}, "depends_on": [1, 2]},
      {"id": 7, "category": "case", "filters": {}, "depends_on": [2, 3, 6]},
      {"id": 8, "category": "cooler", "filters": {}, "depends_on": [1, 7]}
    ]
  }
  ```
- 输出：存入 `state["plan"]`, `state["budget"]`

#### executor 节点
- 读 `plan`，拓扑排序确定执行批次
- 同一批次（无依赖关系）并行调用 `search_component`
- 依赖步骤等前置完成后执行（用上一步返回的 socket/ddr_type 做 filter）
- 每步结果存入 `state["selected_parts"]`
- **关键**：不直接用 LLM 选品，而是用 LLM 的 plan + Tool 的搜索结果，保证数据真实

#### verifier 节点
- 调 `check_compatibility(components=selected_parts)` → 兼容性报告
- 检查预算：`sum(price) <= budget`
- 如果全部通过 → `state["answer"]` 留空（进入 formatter）
- 如果失败 → 记录冲突原因（进入 replanner）

#### formatter 节点
- LLM 将 `selected_parts` 格式化为美观的配置单
- 含兼容性说明、预算分配分析
- 输出 `state["answer"]`

#### replanner 节点（循环回到 executor）
- LLM 分析 verifier 的冲突报告
- 生成新 plan：替换不兼容/超预算的部件
- 最多重试 3 次，3 次失败给出"部分完成 + 冲突说明"

### 5.5 条件边逻辑

```python
def after_planner(state) -> str:
    return "executor"

def after_executor(state) -> str:
    return "verifier"

def after_verifier(state) -> str:
    compat = state.get("compatibility_result", {})
    if compat.get("ok") and state["iteration"] == 0:
        return "formatter"
    elif state["iteration"] >= 3:
        return "formatter"  # 超过重试 → 输出部分结果
    else:
        return "replanner"

def after_replanner(state) -> str:
    return "executor"  # 循环
```

### 5.6 `PlanAndExecuteAgent` 类

```python
class PlanAndExecuteAgent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, *, max_iterations: int = 3):
        self.llm = llm
        self.registry = registry
        self.max_iterations = max_iterations
        self._graph = self._build_graph()
    
    async def run(self, query: str, *, history=None) -> BuildResult:
        initial_state = BuildState(
            messages=history or [],
            requirements=query,
            selected_parts={},
            iteration=0,
            total_tokens=0,
        )
        config = {"configurable": {"thread_id": str(uuid.uuid4())}}
        final = await self._graph.ainvoke(initial_state, config)
        return BuildResult(answer=final["answer"], parts=final["selected_parts"], ...)
```

### 5.7 `IntentRouter` 扩展

```python
# 新增意图
- plan_execute: 配机、装机、DIY（复杂多步约束满足）

# 判断逻辑（在 system prompt 中加规则）
plan_execute: 用户要配一台电脑/主机/整机、DIY装机、组装清单
```

路由结果：
- `rag` → 现有 RAG pipeline
- `agent` → 现有 `AgentLoop` (ReAct)
- `ticket` → 现有 AgentLoop + create_ticket
- `plan_execute` → **新 `PlanAndExecuteAgent`**

---

## 六、连接池修复（搭车修 PLAN_V3 已知差距 #3）

在加 MCP 的同时，把现有 DB 工具的连接问题修了：

```python
# src/core/db_pool.py (新文件)
import psycopg2
from psycopg2 import pool

_pool: pool.ThreadedConnectionPool | None = None

def init_pool(minconn=2, maxconn=10):
    global _pool
    _pool = pool.ThreadedConnectionPool(minconn, maxconn, ...)

def get_connection():
    return _pool.getconn()

def put_connection(conn):
    _pool.putconn(conn)
```

`check_stock` / `track_order` / `create_ticket` 改用 `get_connection()` / `put_connection()` 替代 `connect()` / `close()`。

这是独立的改进，和 MCP 不绑定但同期做。

---

## 七、文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| **数据层** | | |
| `scripts/crawl_components.py` | ✅ 已完成 | Playwright 爬 ZOL 配件频道 |
| `scripts/clean_components.py` | ✅ 已完成 | 8 品类字段标准化 |
| `scripts/ingest_components.py` | ✅ 已完成 | pgvector 入库 |
| `data/knowledge/pc_build_guide.md` | 新建 | 装机选购知识文档 |
| **MCP 服务** | | |
| `services/mcp_payment_server.py` | 新建 | FastMCP 支付/退款服务 |
| **工具层** | | |
| `src/agent/tools/search_component.py` | ✅ 已完成 | `SearchComponent(BaseTool)` |
| `src/agent/tools/check_compatibility.py` | 新建 | `CheckCompatibility(BaseTool)` — 本地规则引擎 |
| `src/agent/mcp_tool.py` | 新建 | `MCPTool(BaseTool)` + `MCPClientManager` |
| **Agent 层** | | |
| `src/agent/plan_execute.py` | 新建 | `PlanAndExecuteAgent` (LangGraph) |
| `src/agent/loop.py` | **不动** | V3 AgentLoop 保留 |
| `src/agent/session.py` | **不动** | |
| `src/agent/sentiment.py` | **不动** | |
| **核心层** | | |
| `src/core/db_pool.py` | ✅ 已完成 | PG 线程连接池 |
| `src/core/retrieve.py` | ✅ 已完成 | 懒加载 + 连接池 + component 分支 |
| `src/core/intent_router.py` | 修改 | 新增 `plan_execute` 意图 |
| **API 层** | | |
| `src/main.py` | 修改 | lifespan 加 MCP Payment Server + PlanAndExecuteAgent |
| `src/api/chat.py` | 修改 | 路由 `plan_execute` → PlanAndExecuteAgent |
| **依赖** | | |
| `pyproject.toml` | ✅ 已完成 | `langgraph>=1.0`, `mcp>=1.0` |
| **测试** | | |
| `tests/test_plan_execute.py` | 新建 | PlanAndExecuteAgent 单元测试 |
| `tests/test_mcp_tool.py` | 新建 | MCPTool 单元测试 |

---

## 八、实施顺序（2026-07-28 修订）

```
Step 1: 爬虫 + 数据入库             ✅ 已完成 (9 品类 1427 条)
Step 1.5: retrieve.py 重构          ✅ 已完成 (BM25懒加载+连接池+component分支)
Step 2: search_component 工具       ✅ 已完成 (含价格过滤)
Step 3: 上游脚本适配                ✅ 已完成 (eval/smoke init_pool)
Step 3.5: SSE 流式 + 多轮对话       ✅ 已完成 (LLMClient→AgentLoop→API)
─── V4 新增 ─────────────────────────────────────────────
Step 4: MCP Payment Server + MCPTool 适配器  ← 当前
Step 5: check_compatibility 本地规则引擎
Step 6: LangGraph PlanAndExecuteAgent
Step 7: IntentRouter 扩展 + chat.py/main.py 集成
Step 8: 测试 + 端到端验证
```

---

## 九、验证方式

```bash
# 1. 爬虫
python scripts/crawl_components.py
python scripts/clean_components.py
python scripts/ingest_components.py

# 2. 静态检查
make lint

# 3. 现有测试必须全过
make test  # 含 test_session.py 45 tests + test_loop.py

# 4. MCP Payment Server 独立启动
python services/mcp_payment_server.py

# 5. 端到端配机测试
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "5000预算帮我配一台能玩3A游戏的主机"}'

# 6. 验证 V3 功能不受影响
curl -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "联想拯救者Y9000P有货吗"}'
```

---

## 十、面试叙事

**LangGraph**：
> "简单查询用自研 ReAct，配机这种多步约束满足场景上了 LangGraph StateGraph。核心是 planner → executor(并行) → verifier → replanner 四个节点加条件回退。不用 `create_react_agent` 是因为配机需要定制化的 plan-then-execute 模式，不是标准 ReAct。"

**MCP**：
> "支付和退款做成独立 MCP Server。因为真实生产里支付系统是独立团队维护的（Java/Go），Agent 不应该直接操作支付数据库。我写了 MCPTool 适配器继承 BaseTool，Agent 侧通过 MCP 协议自动发现工具、调用工具，跟本地工具一模一样。以后换成真实微信支付 API，Agent 一行代码不用改——这就是 MCP 的价值：统一工具发现和调用协议。"

**为什么手写 + LangGraph 共存**：
> "不为用而用。简单查库存用手写 ReAct（轻量、可控），复杂配机用 LangGraph（需要 plan-execute-verify-replan 循环和并行执行）。支付退款走 MCP 因为它是独立有状态服务。三种编排模式各司其职——面试官会问'你为什么不用 LangGraph 全部替代'——因为不同场景需要不同的编排模式。"

---

## 十一、SSE 流式输出

### 11.1 为什么需要 SSE

当前 `/api/v1/chat` 是请求-响应模式：用户发 query → 等 Agent 全部跑完 → 一次性返回结果。问题：

| 场景 | 用户等待时间 | 体验 |
|------|------------|------|
| RAG 简单查询 | 2-3s | 可接受 |
| Agent 多轮工具调用 | 5-15s | 焦虑，不知道在干嘛 |
| Plan-and-Execute 配机 | 20-60s | 完全不可接受 |

SSE 让前端实时看到 Agent 的每一步——思考、工具调用、中间结果——用户知道在推进，不会觉得卡死了。

### 11.2 技术方案

FastAPI 原生支持 `StreamingResponse`，Agent Loop 改造成 async generator：

```
POST /api/v1/chat          ← 保留，非流式（兼容）
POST /api/v1/chat/stream   ← 新增，SSE 流式
```

**SSE 事件类型：**

| event | payload | 说明 |
|-------|---------|------|
| `thinking` | `{"content": "..."}` | LLM 思考/推理 |
| `tool_call` | `{"name": "search_component", "args": {...}}` | 即将调用工具 |
| `tool_result` | `{"name": "...", "status": "success", "summary": "..."}` | 工具执行结果摘要 |
| `answer` | `{"content": "...", "delta": true}` | 最终回答（流式 token） |
| `error` | `{"message": "..."}` | 异常 |
| `done` | `{"total_steps": 5, "total_tokens": 1234}` | 结束标记 |

### 11.3 改造点

| 模块 | 改动 |
|------|------|
| `AgentLoop.run()` | 新增 `run_stream()` 方法，`async yield` 每一步事件 |
| `api/chat.py` | 新增 `/chat/stream` 端点，`StreamingResponse` + `text/event-stream` |
| `LLMClient` | `chat()` 支持 `stream=True`，逐 token yield |
| `PlanAndExecuteAgent` | 图节点间 yield 进度事件 |
| 前端 | `EventSource` 接收 SSE，渲染步骤卡片 |

### 11.4 LLM streaming 怎么嵌入 Agent Loop

DeepSeek 兼容 OpenAI streaming API。关键：streaming 模式下 `tool_calls` 可能跨多个 chunk 累积，需要手动拼合：

```python
async def run_stream(self, query: str):
    yield {"event": "thinking", "content": "正在分析..."}
    
    messages = [{"role": "user", "content": query}]
    for step in range(self.max_steps):
        # streaming LLM call
        accumulated = await self._stream_chat(messages, tools)
        
        if accumulated.content:
            yield {"event": "answer", "content": accumulated.content, "delta": True}
        
        if not accumulated.tool_calls:
            break  # 最终回答
        
        for tc in accumulated.tool_calls:
            yield {"event": "tool_call", "name": tc.name, "args": tc.args}
            result = self.registry.execute(tc.name, **tc.args)
            yield {"event": "tool_result", "name": tc.name, "status": result.status}
            messages.append(...)  # 喂回 observations
    
    yield {"event": "done", "total_steps": step, "total_tokens": ...}
```

### 11.5 实施状态

✅ 已完成。放在 Plan-and-Execute 之前做了，因为非流式配机 20-60s 的等待体验不可接受。
