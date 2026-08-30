# V7-00-02：Harness 运行状态、恢复与副作用契约

状态：架构草案，待负责人冻结
依赖：[PLAN_V7.md](../../plans/PLAN_V7.md)、[V7-00-01_AGENT_PRODUCT_TASK_CATALOG.md](V7-00-01_AGENT_PRODUCT_TASK_CATALOG.md)
范围：冻结 AgentTask、AgentRun、RunStep、运行租约、任务级审批与暂停恢复；不实现数据库 schema、worker、工具或业务领域代码。

## 1. 原则

1. AgentRun 是持久化工作流，不是一次 HTTP 请求或内存聊天循环。
2. 任意时刻最多一个持有有效 lease 的执行者可以推进同一 Run。
3. 每个可能有副作用的 Step 必须有稳定幂等键；未知结果先查询，再决定恢复。
4. checkpoint、Step 结果摘要、审计引用和 Run 状态必须原子提交。
5. 恢复、审批和每个工具调用都重新计算权限交集。
6. V7-01 不开放业务 L2/L3/L4；该阶段只允许任务元数据和 RunApprovalRequest 副作用。

## 2. 受控对象

### 2.1 AgentTask

| 字段 | 规则 |
|---|---|
| task_id | 服务端不可枚举 ID |
| requester_subject_id | 当前认证主体，不从请求体接收 |
| requester_authz_version | 创建时授权版本，仅审计，不替代恢复时校验 |
| profile_id、profile_version | 已发布 Profile，只能收缩权限 |
| task_kind | V7-00-01 定义的枚举 |
| input_schema_version、typed_input | 版本化 DTO，禁止裸领域字典 |
| scope_descriptor | 服务端解析后的资源和字段范围；V7-01 必须绑定 Task 创建时派生的单一 `organization_id`，禁止跨组织范围 |
| task_constraints | 受控的时间、预算、只读、审批和产物限制 |
| idempotency_key、input_hash | 同主体、任务、范围、输入下唯一 |
| created_at、expires_at | 服务端 Clock 生成的 UTC 时间 |

同一任务幂等键和 input_hash 重放返回首次 Task；同 key、不同 hash 返回 IDEMPOTENCY_CONFLICT。任务幂等不等于每个 Step 幂等。

### 2.2 AgentRun

一个 Task 可以有多个 Run，例如首跑或经过人工明确允许的重跑。每个 Run 固定一组 profile、模型、Prompt、知识和工具版本。

| 字段 | 规则 |
|---|---|
| run_id、task_id | 内部 ID 与所属任务 |
| state、version | 第 3 节状态与单调递增乐观锁版本 |
| current_step_no、checkpoint_ref | 最后安全完成 checkpoint |
| lease_owner、lease_expires_at、fencing_token | 执行互斥与旧 worker 隔离 |
| authz_checked_at | 最近一次实时权限交集校验时间 |
| deadline_at、budget | 总时限、步骤数和费用预算 |
| retention_expires_at、retention_hold | 脱敏运行元数据默认 30 天到期；见第 6.2 节的受控保留例外 |
| model、prompt、profile、toolset、knowledge versions | 不可变版本引用 |
| terminal_reason、result_ref | 受控终态原因与结构化结果或 Artifact 引用 |

### 2.3 RunStep

| 字段 | 规则 |
|---|---|
| step_id、run_id、step_no、step_version | Run 内唯一、顺序化、版本化 |
| step_kind | PLAN、TOOL_CALL、WORKFLOW_CALL、RUN_APPROVAL、ARTIFACT、FINALIZE |
| state | PENDING、RUNNING、SUCCEEDED、FAILED_RETRYABLE、FAILED_FINAL、WAITING_APPROVAL、CANCELLED |
| step_idempotency_key | 有副作用时必填，由 Run、Step、能力版本和规范化输入推导 |
| input_summary_ref、result_summary_ref | 脱敏摘要或受控引用，不存原始 PII/payload |
| capability_version | 被调用工具或工作流的不可变版本 |
| attempt_no、started_at、finished_at | 重试与审计依据 |
| error_class | 受控错误类别，禁止原始异常文本 |

同一 run_id 与 step_no 只能有一个成功结果。未来任何领域或外部副作用都必须携带 step_idempotency_key，并由目标 Application Service 或适配器去重。

### 2.4 RunApprovalRequest

RunApprovalRequest 仅用于 V7-01 的任务级继续确认，不表达业务命令。

| 字段 | 规则 |
|---|---|
| approval_id | 不可枚举 ID |
| run_id、step_id、step_version | 三元组唯一绑定 |
| requested_scope | 当前 Run 的受控范围摘要 |
| decision | PENDING、APPROVED、REJECTED、EXPIRED、REVOKED |
| approver_policy | 可审批角色和范围规则 |
| expires_at、decided_at、decision_version | UTC 时间与并发保护 |

进入 WAITING_APPROVAL 时，worker 必须在同一事务写入等待中的 Step、RunApprovalRequest、Run checkpoint 和版本，然后释放 lease；不得在人工等待期间长期占用 worker。

审批处理必须校验：Run 非终态、Step 仍等待、版本一致、审批未过期且审批人有当前范围权限。审批本身不要求持有 worker lease。批准只写入 approval 决定并触发可恢复调度；新的 worker 获取 lease 后，再原子校验批准仍有效并将 Run 推进至 RUNNING。拒绝、撤销或过期使 Run 进入受控终态或人工交接。任何状态、版本、scope 或权限变化都使旧审批无法恢复 Run。

## 3. AgentRun 状态机

| 状态 | 含义 | 允许下一步 |
|---|---|---|
| RECEIVED | Task 已受理，尚未规划 | PLANNING、CANCELLED、EXPIRED |
| PLANNING | 正在产生受限计划 | RUNNING、WAITING_APPROVAL、FAILED_RETRYABLE、FAILED_FINAL、CANCELLED、EXPIRED |
| RUNNING | 正在推进当前 Step | WAITING_APPROVAL、WAITING_DEPENDENCY、FAILED_RETRYABLE、FAILED_FINAL、SUCCEEDED、CANCELLED、EXPIRED |
| WAITING_APPROVAL | 等待任务级审批 | RUNNING、CANCELLED、EXPIRED、FAILED_FINAL |
| WAITING_DEPENDENCY | 等待依赖恢复或人工处理 | RUNNING、FAILED_RETRYABLE、FAILED_FINAL、CANCELLED、EXPIRED |
| FAILED_RETRYABLE | 已记录可恢复失败 | RUNNING、FAILED_FINAL、CANCELLED、EXPIRED |
| SUCCEEDED、FAILED_FINAL、CANCELLED、EXPIRED | 终态 | 不可继续 |

终态永不重进 RUNNING。想重试时创建新 Run 并引用旧 Run 的安全结果与失败原因，不修改历史。

## 4. Run Lease 与 fencing

执行者通过原子条件更新获取 lease，必须同时满足：Run 非终态；当前无 lease 或已过期；expected_version 匹配；调用者是获准 worker/service identity；任务未过期且预算可用。

成功后写入 lease_owner、lease_expires_at，递增 fencing_token 和 version。fencing_token 只增不减；旧 worker 即使继续运行，也不得使用旧 token 写 Run、Step、checkpoint、审批结果或触发下一工具。

续租只允许当前 fencing token 持有者执行。续租失败、数据库连接丢失、任务取消、审批撤销或权限交集失效时，worker 必须停止后续调用，安全记录已知状态，不得假定自己仍能推进 Run。

Run Lease 是 Run 聚合的写 fencing，不替代业务服务幂等。未来两个 worker 即使都尝试同一命令，第二个调用也必须由目标 Application Service 识别为重放或冲突。

### 4.1 调用前 lease 覆盖检查

worker 开始任意 Tool Gateway 或 workflow 调用前必须计算：

```text
remaining_lease = lease_expires_at - now
required_lease  = ToolSpec.timeout_seconds + RUN_LEASE_SAFETY_MARGIN_SECONDS
```

仅当 `remaining_lease >= required_lease` 时才允许发起调用；不足时，worker 必须先以当前 fencing token 成功续租。续租失败、Run 已取消、权限交集失效或 deadline/预算不足时，不得派发调用。

调用返回后，worker 写入 Step、checkpoint 或推进 Run 时必须同时校验：`run_id`、`expected_version`、当前 `fencing_token`，以及 `lease_expires_at > now`。任一条件不成立，旧 worker 的结果不得推进 Run；它只能停止处理，由新的 lease 持有者从最后安全 checkpoint 或未知结果查询流程恢复。

fencing 只能保护 Run 聚合的写入，不能撤回旧 worker 已发出的外部请求。因此未来任何副作用仍必须由目标 Application Service 使用 `step_idempotency_key` 去重；调用超时或 worker 失 lease 后一律先查询结果，不能因为新 worker 接管而换 key 重发。

## 5. Step 与 checkpoint 顺序

只读 Step 的顺序：有效 lease 下标记 RUNNING；调用 Tool Gateway；在同一事务写脱敏结果摘要、引用、Step 状态与 checkpoint；推进 current_step_no 和 Run version。

未来有副作用的 Step 的顺序：创建时持久化 step_idempotency_key；有效 lease 下标记 RUNNING；先按该键查询目标服务或适配器；确认未执行后才调用；将服务结果、审计/Outbox 引用和 Step 状态原子写入；最后推进 checkpoint。

进程在调用和结果落库之间中断时，Step 进入未知结果恢复。新执行者必须先按同一 step_idempotency_key 查询；只有确认未执行且目标服务允许时才可重试。

## 6. 失败、超时和恢复

| 错误类别 | 自动重试 | 恢复规则 |
|---|---|---|
| 输入或权限拒绝 | 否 | FAILED_FINAL 或人工新任务 |
| 资源范围变化 | 否 | 重新授权或新 Task |
| 明确瞬时依赖错误 | 有限重试 | 记录失败、释放 lease、退避后重取 |
| 无副作用工具 timeout | 可重试 | 记录 timeout 与能力版本 |
| 可能有副作用的 timeout | 不可盲重试 | 先按 Step 幂等键查询 |
| lease 丢失 | 否 | 当前 worker 停止，新持有者查询 checkpoint |
| 审批过期或拒绝 | 否 | EXPIRED、CANCELLED 或 FAILED_FINAL |
| 模型输出不合 schema | 有限重试或降级 | 不把未验证输出传给工具 |
| 预算耗尽 | 否 | FAILED_FINAL，交付人工可读摘要 |

retry 预算至少包括单 Step 尝试数、单 Run 工具调用数、总运行时长和模型费用。V7-01 开发环境的技术默认值见第 6.1 节；生产值必须由配置注入，不得由 worker 或模型临时硬编码。

### 6.1 V7-01 开发环境默认值

以下是 Harness 开发与合成评测环境的默认配置，不构成生产 SLO；应通过 Settings/部署配置注入，并由负责人结合 P95 再冻结生产值。

| 配置 | 默认值 | 约束 |
|---|---:|---|
| `RUN_LEASE_SECONDS` | 90 秒 | 单次 lease 时长 |
| `RUN_LEASE_RENEW_INTERVAL_SECONDS` | 30 秒 | 只允许当前 fencing token 持有者续租 |
| `RUN_LEASE_SAFETY_MARGIN_SECONDS` | 15 秒 | 调用前必须预留；ToolSpec timeout 必须小于 lease 减该余量 |
| `RUN_MAX_DURATION_SECONDS` | 300 秒 | 超过后 Run 不得继续派发调用 |
| `RUN_MAX_MODEL_CALLS` | 6 次 | 每个 Run 的模型调用上限 |
| `RUN_MAX_TOOL_CALLS` | 12 次 | 每个 Run 的 Gateway 调用上限 |

### 6.2 V7-01 组织与运行记录留存

V7-01 固定为单组织运行：`organization_id` 只能由当前认证身份和服务端任务创建流程派生，模型、客户端、Profile 和工具输入均不能指定或扩大它。Task、Run、Step、上下文和工具结果引用必须属于同一组织；任何跨组织资源引用统一按安全不可用处理。

Run、Step、审批和调用审计只保存脱敏元数据、受控摘要与引用，默认 `RUN_RETENTION_DAYS=30`。它不授权保存完整 Prompt、聊天、PII、token、签名、支付报文或证据原文。

法律或合规留存例外只能采用显式 `retention_hold`：仅受权管理员可设置或解除，且每次变更必须记录设置人、时间、理由码、原到期时间和解除人。没有已实现且可审计的 `retention_hold` 机制前，系统不得声称具备法律留存能力；默认 30 天规则仍然生效，不能由普通 worker、Agent、Profile 或用户输入延长。

## 7. 取消与权限变化

取消命令携带 expected_version，并原子标记 Run 不可再获取 lease。worker 在续租、写 Step 或调用工具前检测取消。已提交给外部服务的未知副作用不通过取消 Run 假装未发生，而走目标服务的查询、补偿或异常流程。

角色、部门、资源范围、Profile 发布状态或任务期限变化时，恢复必须重新授权。若交集不成立，Run 不可继续，只写安全拒绝原因和人工交接信息，不泄露超范围资源。

## 8. V7-01 最小验收

1. 两个 worker 并发恢复同一 Run，只有一个取得 lease。
2. 剩余 lease 小于 ToolSpec timeout 加安全余量时，worker 不派发调用；续租失败同样不得派发。
3. lease 过期后旧 fencing token 的写入和迟到工具结果推进均被拒绝。
4. 同一 Step 重放不重复成功结果。
5. 工具结果未知时先查询，不直接重试或更换幂等键。
6. 旧 step_version、取消或过期审批无法恢复。
7. 权限或 scope 收缩后恢复和工具调用被拒绝。
8. 进程中断后从最后成功 checkpoint 恢复。
9. Run、Step 和日志不保存 token、密钥、签名、完整 PII 或原始模型上下文。

## 9. 未决项

- 生产环境 lease、续租间隔与预算阈值；开发默认值见第 6.1 节。
- `retention_hold` 的管理员授权模型、理由码目录与实现验收；未实现前不能宣称法律留存能力。
- 任务取消后外部未知结果的人工处理模板。
- V7-03 起哪些 L2 工作流允许 Agent 自动提交。
- AgentRun 持久化表、索引和 migration，必须在 V7-01 实施前单独 ADR 和授权。
