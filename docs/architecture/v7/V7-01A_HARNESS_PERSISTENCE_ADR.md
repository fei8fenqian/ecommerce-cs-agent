# V7-01A：Harness 持久化模型 ADR

状态：设计草案，待架构验收

范围：定义 V7-01 Harness 的 `AgentTask`、`AgentRun`、`RunStep` 与
`RunApprovalRequest` 持久化模型及后续迁移验证方案。本 ADR 不创建 migration、
不创建表、不连接数据库、不修改 legacy 聊天、订单、售后、退款或 MCP。

权威来源：

- [`PLAN_V7.md`](../../plans/PLAN_V7.md) 的 V7-01 与阶段门禁；
- `V7-00-01_AGENT_PRODUCT_TASK_CATALOG.md`；
- `V7-00-02_HARNESS_RUN_STATE_CONTRACT.md`；
- `V7-00-03_TOOL_GATEWAY_RISK_APPROVAL_CONTRACT.md`；
- `V7-00-04_CONTEXT_MEMORY_ARTIFACT_EVAL_CONTRACT.md`；
- 负责人对单组织 `HARNESS_ORGANIZATION_ID` 的确认。

## 1. 决策与边界

V7-01 新建四张只属于 Harness 的表：

```text
agent_tasks
  └─ agent_runs
       └─ run_steps
            └─ run_approval_requests
```

它们只保存受控任务输入、运行状态、脱敏 Step 摘要、引用、lease/fencing 与任务级
审批。它们不是客户聊天记录、业务订单、支付、退款、ActionProposal、WorkItem 或
ExceptionCase 的替代品。

现有 `/api/v1/chat`、`AgentLoop`、`PlanAndExecuteAgent`、`SessionManager` 和
`ToolRegistry` 保持 legacy 路径，不读取或写入这四张表。V7 Harness 后续只通过新的
类型化 Tool Gateway 调用获准的 L0 façade。

### 1.1 单组织边界

当前认证上下文只有 `id`、`username`、`role`，没有组织成员关系。因此 V7-01 是
**单组织部署隔离**，不是多租户系统：

- `HARNESS_ORGANIZATION_ID` 是必填服务端配置，必须为有效 UUID；缺失、空值或格式
  非法时，应用启动失败，且不得有默认值；
- `OrganizationResolver` 只能从该服务端配置派生 organization ID；API、模型、
  Profile、Tool 输入都不能携带、覆盖或扩大它；
- 四张表均使用 `organization_id uuid NOT NULL`；暂不建立 `organizations` 表，也不
  修改 `users`；
- 每个父子关联使用包含 `organization_id` 的复合唯一键和复合外键；
- 以后所有查询、唯一键判定、lease、恢复、审批与留存清理都必须带
  `organization_id` 条件。

这只能保证当前单部署中的 Harness 数据不会因请求参数或模型输出混入另一组织；在
用户、身份提供商和资源均具备真实组织关系前，系统不得宣称支持多租户隔离。

### 1.2 AgentProfile 的唯一可信来源

V7-01 不创建 `agent_profiles` 表。Profile 只能由服务端不可变的
`AgentProfileRegistry` 提供，首版唯一允许：

```text
support-knowledge-readonly@v1
```

- API、模型、请求体、Task 输入和 Tool 输入都不得传入、选择或覆盖
  `profile_id` / `profile_version`；
- Task 创建服务根据当前认证主体、固定任务类别和服务端策略从 Registry 选择 Profile；
- 创建 Task 前必须校验该 Profile 已发布，且其版本、允许工具集和
  `SUPPORT_KNOWLEDGE_ASSIST` 任务类别相匹配；
- 服务端把 `profile_id`、`profile_version` 与由完整不可变 Profile 内容计算的
  `profile_hash` 一起持久化；
- Profile 的任何权限、工具、字段或约束变化必须创建新版本和新 hash。历史 Task/Run
  永远按已持久化版本/hash 解释，不能被 Registry 中的新版本回写或扩大权限。

Registry 只能够收缩当前 actor 的权限；它不能替代实时身份、资源范围、Task 约束或
Tool Gateway 的授权交集校验。

### 1.3 通用数据规则

- 主键均为应用生成的 UUID；不使用可枚举业务号。
- 所有时间为 `timestamptz`，服务端 `Clock` 以 UTC 写入。
- 状态使用 `varchar` 加 `CHECK`，不使用 PostgreSQL enum，便于以后受控扩展。
- 版本与 fencing token 使用 `bigint`，并检查非负；所有写入仍须由 Repository 使用
  `expected_version` 和当前 fencing token 做条件更新。
- JSONB 只承载已由版本化 DTO 校验过的受控对象；数据库至少检查其顶层为 object。
  禁止保存完整 Prompt、完整聊天、token、签名、支付 payload、证据原文或未经脱敏
  的 PII。
- 所有运行相关记录默认保存 30 天。`retention_hold` 只是预留字段；在有管理员授权、
  原因码和审计实现前，不能宣称已支持法定留存例外。

## 2. 表设计

### 2.1 `agent_tasks`

`agent_tasks` 表示一次由当前认证主体发起、已服务端解析 scope 的 Harness 任务。它
不可被模型或客户端直接构造。

| 字段 | 类型与约束 | 用途 |
|---|---|---|
| `task_id` | `uuid` PK | 不可枚举任务 ID。 |
| `organization_id` | `uuid NOT NULL` | 由 `OrganizationResolver` 写入的单组织边界。 |
| `requester_subject_id` | `integer NOT NULL`，FK `users(id)`，`ON DELETE RESTRICT` | 当前认证的人类请求者。 |
| `requester_authz_version` | `varchar(128) NOT NULL` | 创建时的授权版本快照，仅供审计；恢复时仍实时授权。 |
| `profile_id` / `profile_version` / `profile_hash` | `varchar(128)` / `integer` / `char(64)`，均 NOT NULL | 已发布、服务端选择的 AgentProfile 不可变引用与内容 hash。 |
| `task_kind` | `varchar(64) NOT NULL` | V7-01 只允许 `SUPPORT_KNOWLEDGE_ASSIST`。 |
| `input_schema_version` | `varchar(32) NOT NULL` | 输入 DTO 版本。 |
| `input_payload` | `jsonb NOT NULL` | 已脱敏、限长的版本化任务输入，不是原聊天文本。 |
| `scope_descriptor` / `scope_hash` | `jsonb NOT NULL` / `char(64) NOT NULL` | 服务端解析的 scope 与稳定摘要哈希。 |
| `task_constraints` | `jsonb NOT NULL` | 服务端确定的只读、预算、产物与审批限制。 |
| `idempotency_key` / `input_hash` | `varchar(256) NOT NULL` / `char(64) NOT NULL` | 创建任务的重放与冲突判断。 |
| `created_at` / `expires_at` | `timestamptz NOT NULL` | 创建与任务有效期。 |
| `retention_expires_at` / `retention_hold` | `timestamptz NOT NULL` / `boolean NOT NULL DEFAULT false` | 默认 30 天留存与受控 hold 预留。 |

约束与索引：

- `UNIQUE (organization_id, task_id)`：供子表使用的复合父键；
- `UNIQUE (organization_id, requester_subject_id, idempotency_key)`：同一组织内同一
  请求者不能把一个 key 指向两次任务；服务层再比较 `task_kind`、`scope_hash` 与
  `input_hash`，相同则重放首次 Task，不同则 `IDEMPOTENCY_CONFLICT`；
- `CHECK (task_kind = 'SUPPORT_KNOWLEDGE_ASSIST')`：防止 V7-01 偷开财务、运营或异常
  任务；新增类别必须通过后续 migration 与契约验收；
- `CHECK (profile_version > 0)`、`CHECK (expires_at > created_at)`、
  `CHECK (retention_expires_at >= created_at)`，以及三个 JSONB 顶层 object 检查；
- 索引 `(organization_id, requester_subject_id, created_at DESC)` 与
  `(organization_id, retention_expires_at)`。

### 2.2 `agent_runs`

一个 Task 可以有首跑和经明确授权的新 Run；旧终态 Run 永不重新进入运行态。

| 字段 | 类型与约束 | 用途 |
|---|---|---|
| `run_id` | `uuid` PK | 不可枚举 Run ID。 |
| `organization_id` / `task_id` | `uuid NOT NULL` | 复合 FK 指向同组织 Task。 |
| `run_no` | `integer NOT NULL` | 同一 Task 内从 1 开始的 Run 序号。 |
| `state` / `version` | `varchar(32)` / `bigint`，均 NOT NULL | Run 状态机与乐观锁版本。 |
| `current_step_no` | `integer NOT NULL DEFAULT 0` | 最后安全完成的 Step 序号。 |
| `checkpoint` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | 版本化、脱敏 checkpoint envelope；不能保存完整上下文。 |
| `lease_owner` / `lease_expires_at` | `varchar(128)` / `timestamptz`，均可空 | 当前 worker lease。 |
| `fencing_token` | `bigint NOT NULL DEFAULT 0` | 每次成功取得 lease 单调递增，隔离旧 worker。 |
| `authz_checked_at` | `timestamptz` 可空 | 最近一次实时权限交集校验。 |
| `deadline_at` | `timestamptz NOT NULL` | Run 总时限。 |
| `max_model_calls` / `model_calls_used` | `integer NOT NULL` | 固定预算与已使用次数。 |
| `max_tool_calls` / `tool_calls_used` | `integer NOT NULL` | 固定预算与已使用次数。 |
| `execution_versions` | `jsonb NOT NULL` | model、prompt、profile ID/version/hash、toolset、knowledge 的不可变版本引用。 |
| `terminal_reason` / `result_ref` | `varchar(64)` / `varchar(256)`，可空 | 受控终态原因与结构化结果/Artifact 引用。 |
| `created_at` / `updated_at` / `finished_at` | `timestamptz` | 生命周期时间。 |
| `retention_expires_at` / `retention_hold` | 与 Task 同义 | Run 及其运行轨迹留存控制。 |

约束与索引：

- `FOREIGN KEY (organization_id, task_id)` 引用
  `agent_tasks (organization_id, task_id)`；
- `UNIQUE (organization_id, run_id)`：供 Step 与审批使用的复合父键；
- `UNIQUE (organization_id, task_id, run_no)`：同组织同一 Task 不会出现重复 Run 序号；
- 状态只允许 `RECEIVED`、`PLANNING`、`RUNNING`、`WAITING_APPROVAL`、
  `WAITING_DEPENDENCY`、`FAILED_RETRYABLE`、`SUCCEEDED`、`FAILED_FINAL`、
  `CANCELLED`、`EXPIRED`；状态转换本身由领域状态机校验；
- `version`、`fencing_token`、`current_step_no`、调用预算和已用次数必须非负，且
  `used <= max`；`deadline_at > created_at`；
- lease 字段必须同时为 NULL 或同时非 NULL；
- JSONB 顶层 object 检查；
- worker 候选索引：`(organization_id, state, lease_expires_at)`；恢复/超时索引：
  `(organization_id, deadline_at)`；留存索引：`(organization_id, retention_expires_at)`。

数据库列不能单独阻止旧 worker 写入；Repository 必须使用下述条件更新，fencing 才
真正有效：

```text
WHERE organization_id = :organization_id
  AND run_id = :run_id
  AND version = :expected_version
  AND fencing_token = :fencing_token
  AND lease_owner = :worker_id
  AND lease_expires_at > :now
```

获取 lease 时还必须要求 Run 非终态、lease 为空或已过期、`expected_version` 匹配、
未超过 deadline 且预算可用；成功后在同一原子更新中递增 `fencing_token` 与
`version`。续租与释放同样包含 organization、version、owner、token 和有效期条件。

### 2.3 `run_steps`

`run_steps` 是 Run 内顺序化的计划、工具调用、审批与最终化记录。只持久化脱敏输入/
输出摘要和引用，不保存模型原始 reasoning 或完整工具响应。

| 字段 | 类型与约束 | 用途 |
|---|---|---|
| `step_id` | `uuid` PK | 不可枚举 Step ID。 |
| `organization_id` / `run_id` | `uuid NOT NULL` | 复合 FK 指向同组织 Run。 |
| `step_no` / `step_version` | `integer NOT NULL` / `bigint NOT NULL DEFAULT 0` | Run 内顺序与并发版本。 |
| `step_kind` / `state` | `varchar(32)`，均 NOT NULL | 步骤类别与状态机。 |
| `step_idempotency_key` | `varchar(256)` 可空 | 有副作用能力的稳定幂等键；V7-01 L0 多数为空。 |
| `capability_id` / `capability_version` | `varchar(128)` / `varchar(64)`，可空 | 已批准工具或工作流的不可变能力引用。 |
| `input_summary` / `result_summary` | `jsonb NOT NULL DEFAULT '{}'::jsonb` | DTO 校验后的受控摘要。 |
| `result_ref` | `varchar(256)` 可空 | 知识、Artifact 或受控结果引用。 |
| `error_class` | `varchar(64)` 可空 | 受控失败类别，禁止原始异常。 |
| `attempt_no` | `integer NOT NULL DEFAULT 0` | 当前 Step 的尝试次数。 |
| `started_at` / `finished_at` | `timestamptz` 可空 | 执行时间。 |
| `created_at` / `updated_at` | `timestamptz NOT NULL` | 记录时间。 |
| `retention_expires_at` / `retention_hold` | 与 Task 同义 | Step 留存控制。 |

约束与索引：

- `FOREIGN KEY (organization_id, run_id)` 引用 `agent_runs (organization_id, run_id)`；
- `UNIQUE (organization_id, run_id, step_no)`：一个 Run 的一个序号只对应一个 Step；
- `UNIQUE (organization_id, run_id, step_id)`：供审批使用的复合父键；
- `UNIQUE (organization_id, step_idempotency_key) WHERE step_idempotency_key IS NOT NULL`：
  同一组织中不允许同一个稳定副作用键生成两条 Step；
- `step_kind` 只允许 `PLAN`、`TOOL_CALL`、`WORKFLOW_CALL`、`RUN_APPROVAL`、
  `ARTIFACT`、`FINALIZE`；`state` 只允许 `PENDING`、`RUNNING`、`SUCCEEDED`、
  `FAILED_RETRYABLE`、`FAILED_FINAL`、`WAITING_APPROVAL`、`CANCELLED`；
- `step_no`、`step_version`、`attempt_no` 均非负，JSONB 顶层必须为 object；
- 索引 `(organization_id, run_id, step_no)`、
  `(organization_id, run_id, state, updated_at)` 和
  `(organization_id, retention_expires_at)`。

`step_idempotency_key` 必须由 Run、Step、能力版本和规范化输入派生。timeout、进程中断
或失 lease 后，新的 worker 只能用此 key 查询已知结果；不得通过生成新 key 重发可能
有副作用的调用。

### 2.4 `run_approval_requests`

这张表只实现 V7-01 的任务级“允许当前 Run 继续”审批。它不承载订单、退款、库存或
任何业务命令审批。

| 字段 | 类型与约束 | 用途 |
|---|---|---|
| `approval_id` | `uuid` PK | 不可枚举审批 ID。 |
| `organization_id` / `run_id` / `step_id` | `uuid NOT NULL` | 复合 FK 精确绑定同组织 Run Step。 |
| `step_version` | `bigint NOT NULL` | 审批所针对的 Step 版本快照。 |
| `decision` / `decision_version` | `varchar(16)` / `bigint NOT NULL DEFAULT 0` | 审批决定与乐观锁版本。 |
| `requested_scope` | `jsonb NOT NULL` | 脱敏、服务端生成的当前 scope 摘要。 |
| `approver_policy` | `jsonb NOT NULL` | 受控审批角色与范围规则快照。 |
| `requested_at` / `expires_at` | `timestamptz NOT NULL` | 创建与过期时间。 |
| `decided_at` / `decided_by_subject_id` | `timestamptz` / `integer` 可空，后者 FK `users(id)` | 当前有效决定与审批人。 |
| `decision_reason_code` | `varchar(64)` 可空 | 受控拒绝、撤销或过期原因。 |
| `created_at` / `updated_at` | `timestamptz NOT NULL` | 记录时间。 |
| `retention_expires_at` / `retention_hold` | 与 Task 同义 | 审批元数据留存控制。 |

约束与索引：

- `FOREIGN KEY (organization_id, run_id, step_id)` 引用
  `run_steps (organization_id, run_id, step_id)`，阻止跨组织、跨 Run 的审批绑定；
- `UNIQUE (organization_id, run_id, step_id, step_version)`：同一 Step 版本最多一个
  审批请求；
- decision 只允许 `PENDING`、`APPROVED`、`REJECTED`、`EXPIRED`、`REVOKED`；
- `decision_version >= 0`、`expires_at > requested_at`，JSONB 顶层 object 检查；
- 审批决定必须使用可直接实现为数据库 `CHECK` 的一致性规则：

  ```text
  PENDING                     -> decided_at IS NULL
                                 AND decided_by_subject_id IS NULL
  APPROVED / REJECTED         -> decided_at IS NOT NULL
                                 AND decided_by_subject_id IS NOT NULL
  REVOKED                     -> decided_at IS NOT NULL
  EXPIRED                     -> decided_at IS NOT NULL
                                 AND decided_by_subject_id IS NULL
  ```

  `REVOKED` 可以由有权限的人或系统执行，因此审批人字段可空；其余情况不得绕开
  上述约束。该 CHECK 只保证字段自洽，审批 API 仍须校验实时角色和 scope。
- 待处理扫描使用部分索引 `(organization_id, expires_at)`，条件为
  `decision = 'PENDING'`；留存使用 `(organization_id, retention_expires_at)`。

审批 API 不要求持有 worker lease，但它的条件更新必须带 organization、approval ID、
`decision = 'PENDING'`、`decision_version`、未过期和当前审批者 scope。批准仅记录决定
并调度恢复；新 worker 取得 Run lease 后仍须重新校验审批、Run/Step 版本、组织 scope
和实时权限，才能推进 Run。

## 3. 事务、锁与留存边界

1. 创建 Task 的幂等查询和插入在单一事务中完成；遇到唯一冲突时读取同组织同请求者
   同 key 的既有记录，再比较受控 hash，不依赖异常字符串判断。
2. 获取、续租、释放 lease 只更新 `agent_runs` 的单行，不在人工审批等待时保留 lease。
3. 一个 Step 成功时，必须在同一事务写入 Step 的脱敏结果/引用、checkpoint、Run 的
   `current_step_no` 与 version；所有 Run 更新都携带 fencing 条件。
4. 审批进入等待时，必须在同一事务写入 `WAITING_APPROVAL` Step、审批行、Run
   checkpoint 与 Run version，然后释放 lease。
5. `agent_tasks` 是整个运行树的留存根。创建 Run、Step 或 Approval 时，必须从所属
   Task 继承完全相同的 `retention_expires_at` 与 `retention_hold`；不能由 worker、
   模型或子对象自行缩短/延长。未来设置或解除 hold 时，必须在一个事务中同步更新
   同一 Task 的 Run、Step、Approval 全树，避免子记录被提前清理。
6. 留存清理按 `organization_id` 和 `retention_expires_at` 选择，且只清理
   `retention_hold = false` 的终态/过期记录。V7-01 不实现清理 job，也不实现 hold 的
   管理审计；本 ADR 只冻结字段、继承规则和未来查询边界。

所有 Repository 的读写方法都必须显式接收 `organization_id`。缺失组织条件视为实现
缺陷；不得写一个“管理员可跨组织查所有 Run”的便捷查询。

## 4. 后续 migration 方案

实际 migration 只能在本 ADR 通过独立审查、得到独立 `_test` 数据库授权后创建。它
必须：

1. 以当前 head revision 为 `down_revision`，只 `CREATE TABLE`、`CREATE INDEX` 与
   受控约束；
2. 不修改 `users`、legacy `orders`、S3 售后/退款表、聊天 session 表或任何旧数据；
3. 不插入默认任务、Run、审批、Profile 或组织数据；`HARNESS_ORGANIZATION_ID` 和
   `AgentProfileRegistry` 都是应用服务端配置/代码，不是 migration seed；
4. 在空库执行 `alembic upgrade head`，检查四张表、四类状态 CHECK、所有复合外键、
   部分唯一索引和 `alembic_version`；
5. 仅在授权的独立测试库验证升级。测试只使用合成 users 和合成 Harness 数据，并在
   事务回滚或测试清理后验证旧订单总数、UNMATCHED 数量和既有 S3 表行数未变；
6. 生产发布前走备份、schema 对照、`alembic current`、upgrade、约束/索引核验、数据
   量核验和应用启动的 Runbook。

### 4.1 前滚与 downgrade 风险

生产故障优先停止新 Harness API/worker，保留表和运行记录，使用后续小 migration
前滚修复。直接 downgrade 删除四张表会丢失任务、Run、Step 和审批审计；因此 downgrade
只能用于尚无数据的本地开发/全新测试库，不能作为生产恢复方案。

若 migration 仅部分完成，先检查表、索引和 `alembic_version` 的真实状态，再以显式
前滚 revision 修复；不得用 `alembic stamp` 掩盖结构不一致。

## 5. 验收与未验证项

V7-01A 通过前至少需要独立审查确认：

- 四张表没有 payment、refund、ActionProposal、WorkItem、ExceptionCase 或业务资金逻辑；
- organization 的服务端来源、NOT NULL 字段、复合唯一键/外键及所有查询条件完整；
- Profile 只能由服务端不可变 `AgentProfileRegistry` 选择，且 Task/Run 持久化
  Profile 版本与 hash；
- `step_idempotency_key`、`(organization_id, run_id, step_no)` 和审批绑定关系都有
  数据库级唯一性；
- fencing token、version 与 lease 条件更新足以拒绝旧 worker 的迟到写入；
- 30 天留存字段、Task 到子树的继承规则、数据最小化和 hold 的未实现边界明确；
- 空库、独立测试库、前滚与 downgrade 风险说明完整。

本 ADR 尚未验证 PostgreSQL DDL、Alembic、Redis、LLM、MCP、真实 worker 抢占或实际
租约恢复；这些分别属于后续 migration、Repository/Unit of Work、Runner 和评测验收。
