# AGENTS.md

Codex 的项目接管规范。每次在本仓库工作前先阅读本文件，并以用户当前明确指令为最高优先级；`CLAUDE.md` 是既有项目约定的来源，二者冲突时以用户当前指令为准。

## 项目现状

这是“极客数码”3C 电商 AI 客服服务，使用 Python 3.12、FastAPI、PostgreSQL + pgvector、Redis、OpenAI 兼容 LLM（默认 DeepSeek）。当前已实现：

- `/api/v1/chat` 与 `/api/v1/chat/stream`：意图路由后进入 RAG、ReAct 工具 Agent 或 Plan-and-Execute。
- 混合检索：向量检索 + BM25 + RRF + BGE reranker。
- Agent 工具：商品/配件检索、库存、订单、商品比较、工单；可选 MCP 工具会在启动时注册。
- LangGraph 规划执行：配机兼容性校验与故障诊断。
- Redis 会话、多轮指代消解、JWT 登录态、Casbin 内部角色授权、工单接口与健康检查。

`README.md` 和 `CLAUDE.md` 含有部分早期目录/阶段描述；涉及现状、改动范围与行为时，优先相信当前 `src/`、`tests/`、`pyproject.toml` 和 Git 历史。

## 当前运行架构

```text
FastAPI lifespan
  -> PostgreSQL connection pool / ticket + user tables / Redis / Casbin
  -> LLMClient + IntentRouter + ToolRegistry + AgentLoop + PlanAndExecuteAgent

/api/v1/chat[/stream]
  -> SessionManager（Redis 历史与指代消解）
  -> sentiment detection
  -> IntentRouter
     -> rag: hybrid_search -> AgentLoop（携带检索上下文）
     -> agent/ticket: AgentLoop（LLM function calling -> ToolRegistry）
     -> plan_execute: LangGraph planner -> executor -> judge -> replanner -> formatter
```

目录职责：

- `src/api/`：HTTP 契约与响应编排；`src/middleware/`：请求 ID、认证与权限。
- `src/agent/llm/`：LLM 客户端、意图、会话、情绪与指代处理。
- `src/agent/rag/`：检索和排序；`src/agent/engines/`：Agent 执行引擎；`src/agent/tools/`：领域工具。
- `src/infra/`：PostgreSQL、Redis、Casbin；`src/store/`：SQL 数据访问；`src/service/`：业务服务。
- `data/`：知识、商品、模拟数据与评测集；`tests/`：现有行为和安全回归约束。

## Docker 环境

- PostgreSQL 容器：`pgvector`
- Redis 容器：`redis-session`
- PostgreSQL 宿主机端口：`5433`，容器内端口：`5432`
- Redis 端口：`6379`

执行项目命令前，可先确认容器状态：

```bash
docker ps
docker port pgvector
docker port redis-session
```

常用数据库查询：

```bash
docker exec pgvector psql -U postgres -d postgres -c "SELECT 1;"
```

常用 Redis 查询：

```bash
docker exec redis-session redis-cli ping
```

应用从宿主机连接 PostgreSQL 时使用 `localhost:5433`；在 PostgreSQL 容器内部连接时使用容器端口 `5432`。不要在本文件或仓库中写入数据库密码。

默认只执行查询和诊断。涉及数据库迁移、写入、删除、清空或重建容器时，先说明影响并等待用户确认。

## 必须遵守的工程约束

1. `pyproject.toml` 将 `src` 设为包根目录，必须使用扁平导入：`from config import settings`、`from agent...`，不能使用 `from src...`。
2. `src/log_config.py` 的名称不可改回 `logging.py`，否则会遮蔽 Python 标准库。
3. 保持异步边界：HTTP、LLM、数据库、Redis 与工具调用不得在事件循环中引入阻塞 I/O。
4. SQL 的值一律参数化；动态表名、列名或更新字段必须先白名单校验。鉴权、工单、订单和会话改动必须保留相应安全测试。
5. 配置只经 `config.Settings`/环境变量注入；不得提交密钥、`.env` 或私钥。`LLMClient` 构造参数继续显式注入，保证可测试性。
6. 不删除、覆盖或回退用户已有未提交改动。当前工作区已有用户的未跟踪文件 `src/agent/context.py`，除非用户明确指示，不碰它。
7. 只做与请求直接相关的最小改动；改动前先读相关实现与测试，改动后执行成比例的测试并如实报告结果。
8. 保持 Ruff、mypy、pytest 约定；验证优先跑受影响测试，再按需要扩展。不要把 `Makefile` 中用 `|| true` 掩盖的 lint 结果当作通过。

## 类型、接口与可读性约束

这些约束用于让业务代码和领域代码更容易理解、审查和测试：

1. 所有新增或修改的公开函数、方法必须声明完整的参数类型和返回类型。例如：

   ```python
   async def claim_after_sale(
       self,
       command: ClaimAfterSaleCommand,
   ) -> CommandResult:
       ...
   ```

2. Application Service、Repository、Provider、Worker 和 API handler 不得省略返回类型；
   异步方法必须明确返回具体类型，例如 `-> CommandResult`、`-> list[Ticket]`，或
   在非 `async` 的 Protocol 方法中使用 `Awaitable[T]`。
3. 领域命令、领域结果、审计 metadata 和 Outbox payload 不得使用裸 `dict` 或
   `dict[str, Any]` 作为接口类型。优先使用 `dataclass(frozen=True, slots=True)`、
   `Enum`、值对象和 `Protocol`。
4. 领域层不得接收 FastAPI `Request`、客户端角色字段、任意状态字符串或裸数据库
   connection；应接收经过验证的内部命令对象、Actor、事务上下文和状态枚举。
5. 公开的 Service、Repository、Provider、Worker、API handler 和复杂领域函数必须
   使用 Google 风格 docstring。至少在适用时包含 `Args`、`Returns` 和 `Raises`，
   说明参数含义、返回值和可能抛出的领域异常。例如：

   ```python
   def require_command_meta(
       command: AfterSaleCommand,
       meta: CommandMeta,
   ) -> None:
       """校验命令元数据中的主体、来源和命令角色边界。

       Args:
           command: 要执行的领域命令。
           meta: 命令携带的主体、请求上下文、幂等键和版本号。

       Returns:
           None。校验成功表示 meta 可以进入后续业务流程。

       Raises:
           ActorNotAllowedError: 主体或来源不能执行该命令。
       """
   ```

   私有且显而易见的辅助函数可以不写完整 docstring，但复杂逻辑必须解释关键
   前置条件和设计原因。
6. 注释解释“为什么”，类型和 docstring 解释“是什么/怎么调用”；不要用注释代替
   类型签名，也不要写与实现已经不一致的注释。
7. 新增代码完成后，至少运行受影响测试、Ruff、mypy 和 Python 编译检查；如果因为
   外部依赖或测试 fixture 未能运行，必须在交付说明中明确写出，不能宣称通过。
8. `mypy` 配置应逐步收紧，禁止通过删除类型注解、扩大 `Any` 或缩小检查范围来
   掩盖错误；确需 `cast` 时必须在代码附近说明 SDK/边界原因。

## 协作与学习模式

项目既有约定表明用户处于学习阶段。未收到明确实现或设计请求时：

- 只回答具体知识点、执行用户要求的检查命令，或在用户提交后进行代码审查。
- 不主动给架构方案、函数骨架、下一步计划或扩展性建议。
- 用户明确要求“设计、实现、修复、构建、推进”时，在其授权范围内承担相应的技术负责人职责；先给出基于代码证据的结论，再执行最小可验证变更。

### 当前长期协作约定（优先）

用户要求 Codex 在本项目中只担任生产架构顾问：不编写业务代码、不代替用户做实现；工作内容是评估设计、解释生产实践、识别风险、审查用户完成后的代码，以及协助规划能落地的演进路径。除非用户明确撤销这一约定，否则即使存在可修复的问题，也只说明影响和验证思路，不直接修改业务实现。

## 交付标准

- 说明改动了什么、为什么，以及实际运行过哪些验证。
- 对未验证的外部依赖（LLM、数据库、Redis、MCP）明确标注，不将静态检查当作端到端验证。
- 发现架构文档与代码不一致、关键运行缺陷或安全风险时，记录证据和影响；除非用户要求修复，不擅自扩大修改范围。
