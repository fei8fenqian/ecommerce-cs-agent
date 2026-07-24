# PLAN V4 — 配机助手 + Plan-and-Execute + MCP 集成

> **核心命题**：不是为用而用 LangGraph/MCP，而是用一个真正需要它们的业务场景来驱动架构演进。
>
> **场景**："5000 预算，帮我配台打 3A 的主机" — 这是约束满足 + 兼容性验证 + 并行搜索 + 回退重规划，ReAct while-loop 做不了。

---

## 零、为什么 V3 不够

| V3 架构 | 问题 | 配机场景体现 |
|---------|------|------------|
| while-loop ReAct | 无规划，边走边看 | 漏搜电源，配置不完整 |
| 串行工具调用 | 独立搜索互相等待 | CPU 和显卡搜索无依赖但串行 |
| 无验证+回退 | 答了就答了，不自检 | DDR5 内存配 B550 主板不报错 |
| PG 每调 connect+close | 无连接池 | 配一次机 6-8 次 DB 连接 |
| 5 个固定工具 | 无配件数据/兼容性工具 | 搜不到 CPU/主板/显卡 |

**V4 不是重构 V3，是在 V3 基础上增加一条新链路。**

---

## 一、架构全景

```
用户 query
    │
    ▼
┌──────────────┐
│ IntentRouter │  ← 扩展: 新增 "plan_execute" 意图
│ V3 版: rag/ │
│ agent/ticket│
└──┬───┬───┬──┘
   │   │   │
   ▼   ▼   ▼
 RAG  │  Plan-and-Execute  ← NEW
      │       │
      │   ┌───┴────┐
      │   │LangGraph│  StateGraph: planner → executor → verifier → (replan)
      │   └───┬────┘
      │       │
      │   ┌───┴────────────────────┐
      │   │ Tool Calls             │
      │   │ ├─ search_component    │  ← NEW: PG 本地工具
      │   │ ├─ check_compatibility │  ← NEW: MCP 远程工具
      │   │ ├─ search_product      │  V3 保留
      │   │ ├─ check_stock         │  V3 保留
      │   │ ├─ track_order         │  V3 保留
      │   │ └─ create_ticket       │  V3 保留
      │   └────────────────────────┘
      │
      ▼
   AgentLoop (ReAct)  ← V3 保留不动，处理简单工具调用
```

**核心原则**：
- V3 的 `AgentLoop` (ReAct) **不动**，简单查询走快路径
- 新增 `PlanAndExecuteAgent`，复杂查询走 LangGraph
- `IntentRouter` 加一个意图值，路由分叉

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

### 3.3 `check_compatibility` (MCP 远程工具)

通过 `MCPTool(BaseTool)` 适配器包装：

```python
# 在 main.py lifespan 中注册
compat_tool_def = await mcp_compat.list_tools()  # 从 MCP Server 获取工具定义
registry.register(MCPTool(compat_tool_def, mcp_compat.session))
```

Agent 侧看到的和其他工具一样：`registry.execute("check_compatibility", components=[...])`

---

## 四、MCP 集成

### 4.1 `MCPTool(BaseTool)` 适配器 (`src/agent/mcp_tool.py`)

关键设计：`BaseTool.execute()` 是 sync，MCP 调用需要 event loop。用 `ThreadPoolExecutor` + `asyncio.run()` 解决阻抗失配：

```python
class MCPTool(BaseTool):
    """包装 MCP 远程工具为本地 BaseTool"""
    
    def __init__(self, tool_def: dict, session):
        self._name = tool_def["name"]
        self._description = tool_def["description"]
        self._input_schema = tool_def["inputSchema"]
        self._session = session  # 预连接的 MCP ClientSession
    
    @property
    def name(self): return self._name
    @property
    def description(self): return self._description
    @property
    def parameters(self): return self._input_schema
    
    def execute(self, **kwargs) -> ToolResult:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                asyncio.run,
                self._session.call_tool(self._name, arguments=kwargs)
            )
            try:
                result = future.result(timeout=30.0)
                return ToolResult(name=self.name, status="success",
                                  data={"result": str(result.content)})
            except Exception as e:
                return ToolResult(name=self.name, status="error", error=str(e))
```

`MCPClientManager` 负责连接生命周期：stdio transport，在 lifespan 里 `connect` / `disconnect`。

### 4.2 MCP Compat Server (`examples/mcp_compat_server.py`)

独立进程，纯规则引擎，无 DB 依赖。暴露一个工具 `check_compatibility`。

**兼容性规则：**

```python
COMPAT_RULES = [
    # 1. CPU 插槽 == 主板插槽
    {"check": "socket_match", "fields": ["socket"], "categories": ["cpu", "motherboard"],
     "match_type": "equal", "fail_msg": "CPU插槽({cpu_socket})与主板插槽({mb_socket})不匹配"},
    # 2. 主板内存类型 == 内存条类型
    {"check": "ddr_match", "fields": ["mem_type", "ddr_type"],
     "categories": ["motherboard", "ram"], "match_type": "equal",
     "fail_msg": "主板支持{mb_mem}，但选了{ram_type}"},
    # 3. CPU TDP + GPU 功耗 + 余量 <= 电源额定功率
    {"check": "power_budget", "fields": ["tdp", "power_draw", "wattage"],
     "categories": ["cpu", "gpu", "psu"], "match_type": "numeric",
     "formula": "cpu.tdp + gpu.power_draw + 150 <= psu.wattage",
     "fail_msg": "总功耗({total}W)超出电源额定({psu_wattage}W)"},
    # 4. 显卡长度 <= 机箱限长
    {"check": "gpu_length", "fields": ["length_mm", "gpu_max_length"],
     "categories": ["gpu", "case"], "match_type": "numeric",
     "fail_msg": "显卡长度({gpu_len}mm)超出机箱限长({case_max}mm)"},
    # 5. 主板板型 ∈ 机箱支持板型
    {"check": "form_factor", "fields": ["form_factor", "mb_form_factors"],
     "categories": ["motherboard", "case"], "match_type": "list_contain",
     "fail_msg": "主板板型({mb_ff})不在机箱支持列表({case_ffs})中"},
    # 6. 散热器高度 <= 机箱散热器限高
    {"check": "cooler_height", "fields": ["height_mm", "cooler_max_height"],
     "categories": ["cooler", "case"], "match_type": "numeric",
     "fail_msg": "散热器高度({cooler_h}mm)超出机箱限高({case_max_h}mm)"},
    # 7. CPU 插槽 ∈ 散热器支持插槽
    {"check": "cooler_socket", "fields": ["socket", "socket_support"],
     "categories": ["cpu", "cooler"], "match_type": "list_contain",
     "fail_msg": "CPU插槽({cpu_socket})不在散热器支持列表({cooler_sockets})中"},
]
```

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
| `scripts/crawl_components.py` | 新建 | Playwright 爬 ZOL 配件频道 |
| `scripts/clean_components.py` | 新建 | 8 品类字段标准化 |
| `scripts/ingest_components.py` | 新建 | pgvector 入库 |
| `scripts/generate_component_inventory.py` | 新建 | 随机库存生成 |
| `data/knowledge/pc_build_guide.md` | 新建 | 装机选购知识文档 |
| **工具层** | | |
| `src/agent/tools/search_component.py` | 新建 | `SearchComponent(BaseTool)` |
| `src/agent/mcp_tool.py` | 新建 | `MCPTool(BaseTool)` + `MCPClientManager` |
| `src/agent/tools/check_stock.py` | 修改 | 使用连接池 |
| `src/agent/tools/track_order.py` | 修改 | 使用连接池 |
| `src/agent/tools/create_ticket.py` | 修改 | 使用连接池 |
| **Agent 层** | | |
| `src/agent/plan_execute.py` | 新建 | `PlanAndExecuteAgent` (LangGraph) |
| `src/agent/loop.py` | **不动** | V3 AgentLoop 保留 |
| `src/agent/session.py` | **不动** | |
| `src/agent/sentiment.py` | **不动** | |
| **核心层** | | |
| `src/core/db_pool.py` | 新建 | PG 线程连接池 |
| `src/core/intent_router.py` | 修改 | 新增 `plan_execute` 意图 |
| **API 层** | | |
| `src/main.py` | 修改 | lifespan 加 MCP + PlanAndExecuteAgent |
| `src/api/chat.py` | 修改 | 路由 `plan_execute` → PlanAndExecuteAgent |
| `src/config.py` | 修改 | 加 mcp_enabled, mcp_server_cmd, db_pool 配置 |
| **MCP** | | |
| `examples/mcp_compat_server.py` | 新建 | MCP 兼容性规则引擎 |
| **依赖** | | |
| `pyproject.toml` | 修改 | 加 `langgraph>=1.0`, `mcp>=1.0` |
| **测试** | | |
| `tests/test_plan_execute.py` | 新建 | PlanAndExecuteAgent 单元测试 |
| `tests/test_mcp_tool.py` | 新建 | MCPTool 单元测试 |
| `tests/test_session.py` | **不动** | 45 个测试全保留 |
| `tests/test_loop.py` | **不动** | V3 测试全保留 |

---

## 八、实施顺序

```
Step 1: 爬虫 + 数据入库             (脚本，独立可跑)
Step 2: search_component 工具       (纯 PG，无 MCP 依赖)
Step 3: PG 连接池                    (改进现有 3 个工具)
Step 4: MCP 适配器 + Compat Server  (MCP 基础设施)
Step 5: LangGraph PlanAndExecute    (核心编排)
Step 6: IntentRouter 扩展           (路由接入)
Step 7: chat.py / main.py 集成      (端到端联通)
Step 8: 测试 + 端到端验证           (配机流程全链路)
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

# 4. MCP Server 独立启动
python examples/mcp_compat_server.py

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
> "兼容性规则引擎做成独立 MCP Server，Agent 通过 MCPTool 适配器调用。这样规则可以独立更新——新 CPU 发布不用动 Agent 代码。MCPTool 继承 BaseTool，注册进同一个 Registry，Agent Loop 完全无感。"

**为什么手写 + LangGraph 共存**：
> "不为用而用。简单查库存用手写 ReAct（轻量、可控），复杂配机用 LangGraph（需要 plan-execute-verify-replan 循环和并行执行）。面试官会问'你为什么不用 LangGraph 全部替代'——因为不同场景需要不同的编排模式。"
