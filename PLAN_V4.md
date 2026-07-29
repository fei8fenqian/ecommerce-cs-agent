# PLAN V4 — Plan-and-Execute + MCP + 多场景 Agent

> **核心命题**：不是为用而用 LangGraph/MCP，而是用真正需要它们的业务场景来驱动架构演进。
>
> **三大场景**：
> 1. "5000 预算，帮我配台打 3A 的主机" — 约束满足 + 兼容性验证 + 并行搜索，ReAct while-loop 做不了
> 2. "我的笔记本开不了机了" — 多步串行诊断（查订单→判在保→查知识库→建工单），需要 plan-then-execute
> 3. "我要退款" — 支付/退款是独立系统，天然适合 MCP 协议分离

> **最后更新**: 2026-07-29

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

## 四、MCP 集成 — 支付服务 ✅ 已完成

### 4.1 为什么支付适合 MCP

支付/退款在真实公司是**独立有状态服务**——微信支付回调、退款对账、资金安全，通常由财务/支付团队维护，语言可能是 Java/Go。Agent 不应该直接操作支付数据库。

### 4.2 实际实现

**MCP Payment Server** (`src/service/mcp_payment_server.py`)：
- FastMCP 独立进程，**SSE transport**（非 stdio），监听 `0.0.0.0:8081`
- 为什么 SSE 非 stdio：stdio 是父子进程模式，不适合生产（支付服务应独立部署、独立扩缩容）；SSE 走 HTTP，可以放 Nginx 后面、做负载均衡
- 两个工具：`check_payment(order_id)` + `request_refund(order_id, reason)`
- 自己 `init_pool()` 建 PG 连接（独立进程，不共享 main.py 的池）
- 退款校验：状态不能是 已退款/退款中/已取消，paid_amount > 0

**MCP Client** (`src/agent/mcp_tool.py`)：
- `MCPClientManager`: 封装 `sse_client(url)` → `ClientSession` → `initialize()` → `list_tools()` / `call_tool()` → `disconnect()`
- `MCPTool(BaseTool)`: 适配器，`name/description/parameters` 来自 `list_tools()` 自动发现，`execute()` 直接 `await call_tool()`

**BaseTool async 重构**：
- `BaseTool.execute()` 从 `def` 改为 `async def`，消除 `asyncio.run()` + `asyncio.to_thread()` 双重桥接
- 6 个本地工具 + `ToolRegistry.execute()` + `loop.py` 两处调用点全部适配

**配置驱动注册** (`main.py` lifespan)：
```python
# .env: MCP_SERVERS='["http://localhost:8081/sse"]'
for url in settings.mcp_servers:
    manager = MCPClientManager(url)
    await manager.connect()
    for tool_info in await manager.list_tools():
        registry.register(MCPTool(manager, tool_info))
```
未来加物流 MCP Server、库存 MCP Server 只需在 `.env` 里加 URL，Agent 代码零改动。

### 4.3 实施状态

| 组件 | 状态 |
|------|:---:|
| MCP Payment Server (FastMCP + SSE, port 8081) | ✅ |
| MCPClientManager + MCPTool 适配器 | ✅ |
| BaseTool.execute() async 重构 | ✅ |
| Config-driven 注册 (main.py lifespan) | ✅ |
| check_compatibility → 降级为本地 BaseTool（不需要 MCP） | ✅ |

---

## 五、LangGraph Plan-and-Execute Agent ← 当前 Step

### 5.0 前置数据准备 ✅

在开始 Plan-and-Execute 之前，完成了故障诊断场景所需的数据基础：

**故障排查知识库** (8 文件，`data/knowledge/troubleshooting_*.md`)：

| 文件 | 内容 |
|------|------|
| `troubleshooting_laptop_general.md` | 品牌无关排查树：第零步在保判断 → 6 种故障现象(A-F) → software vs hardware 速查 |
| `troubleshooting_laptop_apple.md` | Mac 特有：SMC/NVRAM 重置、Touch Bar、电池健康 |
| `troubleshooting_laptop_huawei.md` | MateBook 特有：运输模式、F10 智能还原、BIOS 更新黑屏 |
| `troubleshooting_laptop_lenovo.md` | ThinkPad 特有：POST 报错码表(0175-0251)、PC Doctor、S.M.A.R.T. |
| `troubleshooting_phone_apple.md` | iPhone 特有：强制重启按键表、恢复模式、DFU 模式对比 |
| `troubleshooting_phone_android.md` | Android 通用：Recovery 按键表（各品牌）、双清、fastboot |
| `troubleshooting_phone_huawei.md` | 华为手机：eRecovery 在线修复、安全模式、HarmonyOS 升级 |
| `troubleshooting_phone_xiaomi.md` | 小米/Redmi：MIUI Recovery 5.0、线刷 MiFlash、电池老化 |

每个知识库的第一步都是**"第零步：判断在保状态"**——Agent 必须先 `track_order` 查 delivered_at，再决定走官方售后还是 DIY 排查。这个"先查在保"的模式就是 Plan-and-Execute 的 plan 来源。

**`delivered_at` 列**：orders 表新增 `delivered_at DATE`，只有"已签收""已完成"状态有值，用于计算保修期。

### 5.1 为什么需要两个场景

Plan-and-Execute 的核心价值是**先规划再执行**，但不同业务的"执行"模式完全不同：

| 维度 | 配机选品 | 故障诊断 |
|------|---------|---------|
| 执行模式 | **并行** — CPU 和显卡无依赖 | **串行** — 每步依赖上一步结果 |
| 步骤数 | 6-8 步（8 个品类） | 3-5 步（查单→判保→查库→建工单） |
| 验证方式 | 兼容性规则引擎 + 预算 | 在保状态判断（分支正确？） |
| 重规划 | 换不兼容部件 | 换排查路径（软件→硬件 或 换品牌知识库） |
| 最终输出 | 配置清单 + 价格 | 诊断结论 + 操作建议 + 工单号 |
| LLM 角色 | planner 生成搜索计划，executor 调 tool | planner 生成诊断步骤，executor 串行执行，judge 判断是否找到根因 |

一个 LangGraph StateGraph，planner 根据意图生成不同形状的 plan，executor 根据 plan 的依赖关系决定并行还是串行。

### 5.2 为什么不替换 AgentLoop

- 简单查询（"查库存""退货流程"）不需要 Plan-and-Execute，反而变慢
- `AgentLoop` 已稳定、有测试覆盖，SSE 流式也工作正常
- 新增 `PlanAndExecuteAgent` 类，两个 Agent 共存，`IntentRouter` 加 `plan_execute` 意图分叉

### 5.3 图结构

```
                    ┌──────────────────────┐
                    │      planner          │
                    │  LLM: query → 结构化计划 │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │      executor         │
                    │  配机: 并行执行无依赖步骤│
                    │  诊断: 串行逐步执行     │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │       judge           │
                    │  配机: 兼容性+预算检查  │
                    │  诊断: 根因是否找到?    │
                    └──────┬──────┬────────┘
                           │      │
                    ┌──────▼─┐ ┌──▼──────────┐
                    │ 通过    │ │ 未通过       │
                    └──────┬─┘ └──┬──────────┘
                           │      │
                    ┌──────▼─┐ ┌──▼──────────┐
                    │formatter│ │  replanner   │
                    │ 格式化  │ │ 分析失败原因  │
                    │ 最终输出│ │ 调整方案     │
                    └────────┘ └──┬──────────┘
                                 │
                                 ▼
                            executor (重新执行)
                            (最多 3 次循环)
```

### 5.4 统一 State

```python
class PlanExecuteState(TypedDict, total=False):
    # 输入
    messages: list[dict[str, Any]]        # 对话历史
    query: str                            # 用户原始问题
    scenario: str                         # "build_pc" | "troubleshoot"
    
    # Planner 产出
    plan: list[dict]                      # 结构化计划步骤
    #   配机 plan step: {"id": 1, "category": "cpu", "filters": {...}, "depends_on": []}
    #   诊断 plan step: {"id": 1, "action": "track_order", "args": {...}, "depends_on": [], "purpose": "查订单在保状态"}
    budget: int | None                    # 配机场景的预算
    
    # Executor 产出
    step_results: dict[int, dict]         # {step_id: tool_result}
    selected_parts: dict[str, dict]       # 配机: {category: component}
    diagnosis_path: list[str]             # 诊断: 已执行的诊断路径 ["查单→在保→查知识库", ...]
    
    # Judge 产出
    judge_passed: bool
    judge_reason: str                     # 未通过原因
    
    # 循环控制
    iteration: int                        # 当前重试次数
    max_iterations: int                   # 最大重试次数 (默认 3)
    
    # 最终输出
    answer: str
    total_tokens: int
```

### 5.5 四种节点

#### planner 节点

输入：`query` + `scenario`
输出：结构化 `plan`

配机场景 LLM 生成：
```json
{
  "scenario": "build_pc",
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

故障诊断场景 LLM 生成：
```json
{
  "scenario": "troubleshoot",
  "device_type": "laptop",
  "brand": "联想",
  "symptom": "开不了机，电源灯不亮",
  "steps": [
    {"id": 1, "action": "track_order", "args": {"order_id": "..."}, "depends_on": [], "purpose": "查订单确认在保状态"},
    {"id": 2, "action": "search_knowledge", "args": {"query": "联想笔记本 无法开机 电源灯不亮", "brand": "lenovo", "device_type": "laptop"}, "depends_on": [1], "purpose": "根据在保状态查对应知识库"},
    {"id": 3, "action": "create_ticket", "args": {}, "depends_on": [2], "purpose": "如果无法自助解决，创建工单", "conditional": "仅在无法自助解决时执行"}
  ]
}
```

关键设计决策：
- **planner 不直接调工具**——它只生成计划，不接触数据库。计划是指令，不是执行
- **plan 中的 filter 是 planner 根据用户需求推导的**，不是从数据库查的（例：预算 5000 → CPU 不超过 1200）
- executor 拿到 plan 后，用 filter 调 `search_component`，这才是真正查数据库

#### executor 节点

核心逻辑：**拓扑排序 plan → 分批执行**。

```
1. 拓扑排序 plan.steps（根据 depends_on）
2. 同一批次（无依赖步骤）并行执行
3. 每个 step 执行完后，把结果写入 state["step_results"][step_id]
4. 后续步骤可以用前置步骤的结果做 filter（如 CPU 的 socket → 主板的 socket filter）
5. 全部执行完 → 进入 judge
```

对于配机：CPU 和 GPU 并行，主板等 CPU 返回后再搜（用 CPU 的 socket 做 filter）。
对于诊断：每步 `depends_on: [上一步]` → 自然串行。

```python
async def executor_node(state: PlanExecuteState) -> PlanExecuteState:
    plan = state["plan"]
    step_results = {}
    
    # 拓扑排序 → 按批次执行
    batches = topological_sort(plan["steps"])
    for batch in batches:
        # 同批次并行
        tasks = [execute_step(step, step_results) for step in batch]
        batch_results = await asyncio.gather(*tasks)
        for step_id, result in batch_results:
            step_results[step_id] = result
    
    state["step_results"] = step_results
    return state
```

#### judge 节点

**配机场景**：
- 调 `check_compatibility(selected_parts)` → 兼容性报告
- 检查预算：`sum(price) <= budget`
- 通过 → 进入 formatter
- 未通过 → 记录冲突原因，进入 replanner

**故障诊断场景**：
- 判断是否找到了根因（从 knowledge search 结果中判断）
- 判断是否需要创建工单（用户能自助解决？）
- 判断诊断路径是否合理（在保却走了 DIY 路径？）
- 通过 → 进入 formatter（生成诊断报告 + 操作建议）
- 未通过 → replanner（换个方向，如从软件问题换到硬件问题）

#### replanner 节点

LLM 分析 judge 的失败原因，生成新的 plan（不是微调旧 plan，是重新生成）：

```
输入：原 plan + judge 失败原因 + 已执行的 step_results
输出：新 plan（替换不兼容/超预算的部件，或换诊断路径）
```

最多重试 3 次，3 次后强制进 formatter 输出"部分结果 + 冲突说明"。

#### formatter 节点

LLM 将执行结果格式化为用户友好的回答：
- 配机：配置清单表格 + 兼容性说明 + 预算分配 + 装机提示
- 诊断：诊断结论 + 操作步骤（按优先级排列）+ 工单信息（如有）

### 5.6 条件边

```python
def after_planner(state) -> str:
    return "executor"

def after_executor(state) -> str:
    return "judge"

def after_judge(state) -> str:
    if state.get("judge_passed"):
        return "formatter"
    if state.get("iteration", 0) >= state.get("max_iterations", 3):
        return "formatter"  # 超过重试 → 输出部分结果
    return "replanner"

def after_replanner(state) -> str:
    state["iteration"] = state.get("iteration", 0) + 1
    return "executor"
```

### 5.7 `PlanAndExecuteAgent` 类

```python
class PlanAndExecuteAgent:
    def __init__(self, llm: LLMClient, registry: ToolRegistry, *, max_iterations: int = 3):
        self.llm = llm
        self.registry = registry
        self.max_iterations = max_iterations
        self._graph = self._build_graph()
    
    async def run(self, query: str, *, history=None, scenario: str = "") -> PlanExecuteResult:
        initial_state = PlanExecuteState(
            messages=history or [],
            query=query,
            scenario=scenario,
            step_results={},
            iteration=0,
            max_iterations=self.max_iterations,
        )
        final = await self._graph.ainvoke(initial_state)
        return PlanExecuteResult(answer=final["answer"], ...)
    
    async def run_stream(self, query: str, *, history=None, scenario: str = ""):
        """SSE 流式 — 图节点间 yield 进度事件"""
        # async for event in self._graph.astream_events(initial_state):
        #     yield {"event": "node_start", "node": event["name"]}
        #     yield {"event": "node_end", "node": event["name"], "summary": ...}
```

### 5.8 设计要点

**1. Plan 是 LLM 生成的，但执行是确定性的**

planner 节点用 LLM 做"语义理解 → 结构化计划"，这一步需要推理能力。但 executor 不做 LLM 调用——它只是照计划执行工具调用，保证数据来自数据库而非 LLM 幻觉。judge 中的兼容性检查也是规则引擎而非 LLM。

**2. 拓扑排序决定并行度**

不需要在 plan 里标"这个可以并行"——`depends_on` 表达了依赖关系，拓扑排序自动算出哪些步骤可以并行。诊断场景全部 `depends_on: [上一步]` → 自然串行。

**3. Replanner 做减法不做加法**

replanner 不需要重排所有步骤。对于配机，只替换出问题的品类（兼容性冲突的部件）；对于诊断，只替换走不通的分支（如软件排查无效 → 换硬件排查）。

**4. MCP 工具对 Plan-and-Execute 透明**

`check_payment` 和 `request_refund` 对 Plan-and-Execute 而言只是两个普通的 tool。plan 里可以包含 `{"action": "check_payment", ...}`，executor 照常调 `registry.execute()`。MCP 的远程调用对编排层完全透明。

---

## 六、连接池 ✅ 已完成

`src/core/db_pool.py` — `psycopg2.pool.ThreadedConnectionPool`:
- `init_pool(minconn=2, maxconn=10)` → `get_connection()` → `put_connection(conn)` → `close_pool()`
- 所有 DB 工具（check_stock / track_order / create_ticket / search_product / search_component）已切换
- MCP Payment Server 独立进程有自己的 pool

---

## 七、文件变更清单

| 文件 | 状态 | 说明 |
|------|:---:|------|
| **数据层** | | |
| `scripts/crawl/crawl_components.py` | ✅ | Playwright 爬 ZOL 配件频道 |
| `scripts/clean/clean_components.py` | ✅ | 8 品类字段标准化 |
| `scripts/ingest/ingest_components.py` | ✅ | pgvector 入库 |
| `scripts/generate/orders.py` | ✅ | 新增 `delivered_at` 列生成逻辑 |
| `scripts/crawl/crawl_troubleshooting.py` | ✅ | 爬取官方支持页面 (3品牌 9URL) |
| `data/knowledge/troubleshooting_*.md` | ✅ | 8 个故障排查知识库文件 |
| `data/knowledge/pc_build_guide.md` | 🔜 | 装机选购知识文档 |
| **MCP 服务** | | |
| `src/service/mcp_payment_server.py` | ✅ | FastMCP 支付/退款服务 (SSE, port 8081) |
| **工具层** | | |
| `src/agent/tools/search_component.py` | ✅ | `SearchComponent(BaseTool)` |
| `src/agent/tools/check_compatibility.py` | 🔜 | `CheckCompatibility(BaseTool)` — 本地规则引擎 |
| `src/agent/mcp_tool.py` | ✅ | `MCPTool(BaseTool)` + `MCPClientManager` |
| `src/agent/tools_registry.py` | ✅ | `BaseTool.execute()` async 重构 |
| **Agent 层** | | |
| `src/agent/plan_execute.py` | 🔜 | `PlanAndExecuteAgent` (LangGraph StateGraph) |
| `src/agent/loop.py` | ✅ | V3 AgentLoop 保留，async 适配 |
| `src/agent/session.py` | ✅ | 不变 |
| `src/agent/sentiment.py` | ✅ | 不变 |
| **核心层** | | |
| `src/core/db_pool.py` | ✅ | PG 线程连接池 |
| `src/core/retrieve.py` | ✅ | 懒加载 + component 分支 |
| `src/core/intent_router.py` | 🔜 | 新增 `plan_execute` 意图 |
| **API 层** | | |
| `src/main.py` | ✅ | lifespan: MCP config-driven 注册 + Agent 初始化 |
| `src/api/chat.py` | 🔜 | 路由 `plan_execute` → PlanAndExecuteAgent |
| `src/config.py` | ✅ | 新增 `mcp_servers: list[str]` |
| **测试** | | |
| `tests/test_plan_execute.py` | 🔜 | PlanAndExecuteAgent 单元测试 |
| `tests/test_mcp_tool.py` | 🔜 | MCPTool 单元测试 |

---

## 八、实施顺序（2026-07-29 更新）

```
Step 1: 爬虫 + 数据入库             ✅ (9 品类 1427 条)
Step 1.5: retrieve.py 重构          ✅ (BM25懒加载+连接池+component分支)
Step 2: search_component 工具       ✅ (含价格过滤)
Step 3: 上游脚本适配                ✅ (eval/smoke init_pool)
Step 3.5: SSE 流式 + 多轮对话       ✅ (LLMClient→AgentLoop→API)
─── V4 新增 ─────────────────────────────────────────────
Step 4: MCP Payment Server          ✅ (FastMCP SSE + MCPTool + async重构 + config-driven)
Step 4.5: 故障排查知识库            ✅ (8 files + delivered_at + crawl脚本)
Step 5: Plan-and-Execute Agent      🔜 当前 (LangGraph, 双场景)
Step 6: check_compatibility         🔜 (本地规则引擎)
Step 7: IntentRouter 扩展           🔜 (新增 plan_execute)
Step 8: chat.py/main.py 集成        🔜 (路由分叉 + SSE 流式)
Step 9: 测试 + 端到端验证           🔜
```

**当前 commit**: `abeb276 feat(knowledge): 故障排查知识库 + delivered_at 列 + 爬取脚本`

**Step 5 拆分**（按实现顺序）：
1. `PlanExecuteState` + `PlanAndExecuteAgent` 类骨架 + StateGraph 构建
2. planner 节点：LLM 生成结构化 plan（支持两种 scenario 的 prompt）
3. executor 节点：拓扑排序 + 分批执行（并行/串行自动判断）
4. judge 节点：配机兼容性检查 + 诊断根因判断
5. replanner 节点：LLM 分析失败原因 + 重新生成 plan
6. formatter 节点：LLM 格式化最终输出
7. `run_stream()` SSE 流式：图节点间 yield 进度事件

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

**LangGraph Plan-and-Execute**：
> "简单查询用自研 ReAct AgentLoop，配机和故障诊断这种需要多步规划的场景上了 LangGraph StateGraph。核心是 planner → executor → judge → replanner 四个节点加条件回退。一个 StateGraph 支持两种场景：配机是并行搜索（CPU 和显卡同时搜），故障诊断是串行（查订单→判在保→查知识库→建工单），拓扑排序根据 `depends_on` 自动决定并行度。不用 `create_react_agent` 是因为配机需要定制化的 plan-then-execute 模式，不是标准 ReAct。"

**MCP**：
> "支付和退款做成独立 MCP Server（SSE transport，独立进程监听 8081）。真实生产里支付系统是独立团队维护的（Java/Go），Agent 不应该直接操作支付数据库。我写了 MCPTool 适配器继承 BaseTool，BaseTool 整个重构成了 async。Agent 侧通过 config-driven 自动发现 MCP 工具、注册进 ToolRegistry，跟本地工具调用方式一模一样。以后换成真实微信支付 API，Agent 一行代码不用改——这就是 MCP 的价值：统一工具发现和调用协议。"

**为什么手写 + LangGraph 共存**：
> "不为用而用。简单查库存用手写 ReAct（轻量、可控），复杂配机和诊断用 LangGraph（需要 plan-execute-verify-replan 循环和依赖管理），支付退款走 MCP（独立有状态服务）。三种编排模式各司其职——面试官会问'你为什么不用 LangGraph 全部替代'——因为不同场景需要不同的编排模式，简单场景用 ReAct 更直接，没有 over-engineering。"

**故障诊断知识库设计**：
> "8 个文件按 device_type × brand 拆分，每个文件第一步都是'第零步：判断在保状态'。这不是内容要求，是 Plan-and-Execute 的 planner 用到的结构——plan 里第一步永远是 track_order，然后根据在保/过保走不同分支。知识库的拆分粒度直接决定了 planner 能生成多精准的 plan。"

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

✅ 已完成。AgentLoop.run_stream() 已实现，`/api/v1/chat/stream` 端点可用。

Plan-and-Execute 也需要流式——PlanAndExecuteAgent.run_stream() 在图节点间 yield 进度事件（`plan_generated` → `step_executing` → `step_complete` → `judging` → `formatting`），让用户看到配机/诊断的每一步进展。
