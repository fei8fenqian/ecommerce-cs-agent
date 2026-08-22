# S3-02 应用服务与状态机实施计划

> 状态：架构修订后可交付实施；数据库集成测试仍须遵循测试库与负责人授权边界
>
> 本文只规划 S3-02。当前不修改业务代码、数据库、迁移、支付适配器或外部集成。
>
> 权威规则：`S0_DECISION_RECORD.md`、`S0-01_PRD_REFUND_WORKBENCH.md`、
> `S0-02_ROLE_MATRIX_STATE_MACHINES.md`、`S0-03_API_SLO_DATA_CLASSIFICATION.md`、
> `S3-01_DESIGN_PACKAGE.md`。

## 1. 目标与边界

### 1.1 S3-02 要完成的事

S3-02 建立退款领域的应用服务层，负责：

1. 将售后申请、退款单的状态变化集中到一个受控事务边界内；
2. 校验状态前置条件、角色范围、订单归属、`expected_version` 和职责分离；
3. 在状态变化、拒绝和幂等重放时追加应用层审计；
4. 保证领域状态、退款记录、Outbox 和审计记录在需要原子性的命令中同一事务提交；
5. 为 S3-03 API、S3-04 worker/Mock Payment Adapter、S3-05 资格策略提供稳定的
   内部服务接口。

### 1.2 明确不在 S3-02 的范围

- 不创建或修改数据库表，不执行 Alembic，不新增迁移；
- 不实现 HTTP API、SSE、前端或 Agent 工具；这些由 S3-03 及后续阶段接入；
- 不接支付宝、真实支付、真实 callback、物流 MCP 或对象存储；
- 不实现 Mock Payment Adapter、退款 worker、callback/对账收敛；这些属于 S3-04；
- 不实现退款资格政策的具体数值判断；D01/D02 的确定性策略与
  `MockOrderEligibilityFactProvider` 接入属于 S3-05；
- 不从 `orders.status` 推断支付或履约事实，不使用旧订单的
  `paid_amount/payment_time` 伪装成支付流水；
- 不自动回填历史订单，不处理当前业务库中的 50,000 条 UNMATCHED 订单；
- 不改变 D01–D12，不扩大 admin、客服、Agent 或 MCP 权限。

S3-02 可以定义并依赖只读的事实提供者接口，但不得在本阶段用旧字段实现一个
“看起来可用”的生产支付/履约判断。

## 2. 当前事实与实施约束

### 2.1 已落地的 S3-01 资源

S3-01 已在独立 `ecommerce_agent_s3_test` 完成迁移验证：迁移前 revision 为
`6aa6adbb7084`，迁移后为 `a7d1e8f4c902`；合成订单总数为 `4`、`UNMATCHED` 数量为
`1`，迁移前后均不变；四张新表存在且 schema 测试 `5 passed`。四张表的结构为：

- `after_sale_requests`：售后申请及其状态、版本、归属、证据引用；
- `refunds`：退款执行记录、金额、支付流水引用、外部请求/回调标识及状态；
- `audit_events`：应用层追加式审计；
- `outbox_events`：向支付适配器投递退款命令的可靠事件。

S3-02 不应重新执行迁移；所有数据库集成测试必须使用该隔离测试库，且不得改变其已有
合成订单的数量或归属。

### 2.2 现有代码边界

当前 `src/service/` 主要只有认证服务，`src/store/` 只有用户、订单、工单、会话
访问层，尚无退款领域应用服务。现有 `order_store` 和 Agent 订单工具是读取入口，
不得成为退款状态的写入入口。

因此 S3-02 应沿用项目的分层方式：

```text
API / Agent / worker
        |
        v
退款领域 Application Service
        |
        +--> OrderEligibilityFactProvider（只读事实端口）
        +--> after_sale / refund / audit / outbox store
        +--> 同一数据库事务
```

API、Agent、worker 只能调用应用服务；store 只负责参数化 SQL、行锁和持久化，不能
自行决定业务状态转换；应用服务不能绕过 store 直接拼接 SQL。

## 3. 应用服务设计

### 3.1 推荐模块边界

第一版先保持现有扁平导入和目录约定，建议新增：

```text
src/service/after_sale_service.py       # 领域应用服务和事务编排
src/service/after_sale_types.py         # Actor、事实快照、命令结果等内部类型
src/store/after_sale_store.py            # after_sale_requests 读写
src/store/refund_store.py                # refunds 读写
src/store/audit_store.py                 # audit_events 追加
src/store/outbox_store.py                # outbox_events 原子创建/状态读写
```

如果实现过程中确认类型数量很少，可以将 `after_sale_types.py` 合并到服务文件；
不能为了省文件而把退款 SQL 放回 API、Agent 或 worker。

### 3.2 Actor 与调用来源

应用服务接收受控的内部 `Actor`，至少包含：

- `actor_type`：内部枚举 `ActorType.CUSTOMER`、`AGENT`、`FINANCE`、`OPERATOR`、
  `ADMIN`、`WORKER`、`PAYMENT_GATEWAY`、`SYSTEM`；持久化值必须与数据库 CHECK 一致的
  大写字符串；
- `actor_user_id`：用户角色调用时必填，worker/system 可使用受控服务身份；
- `request_id`：关联当前 HTTP/任务请求；
- `trace_id`：若当前调用链有 trace context 则传入；
- `source`：`api`、`agent`、`worker`、`callback`、`system`，用于审计。

Actor 不是客户端可自由提交的 JSON。API 从认证上下文将外部角色转换为内部枚举，
Agent/worker 从受控服务上下文构造；服务层不得信任请求体中的 `role`、`actor_user_id`
或“已通过审核”字段。

### 3.3 不暴露任意状态跳转方法

应用服务不能提供一个接受任意 `target_status` 的公开方法，例如：

```text
transition(request_id, target_status)  # 禁止作为业务入口
```

应提供语义明确的命令方法，内部再通过白名单状态图执行原子转换。建议的内部服务
契约如下；具体 Python 参数、返回值和异常类型由
`S3-02_INTERNAL_CONTRACT.md` 先行冻结，冻结前不得进入实现：

| 服务命令 | 允许调用者 | 作用 |
|---|---|---|
| `submit_after_sale` | customer | 创建一条新的售后申请；只验证归属与事实结构，资格分流委托 S3-05 的策略端口 |
| `cancel_after_sale` | customer | 按 D04 取消申请 |
| `request_evidence` | agent/system | 进入或保持 `EVIDENCE_PENDING` |
| `complete_evidence` | customer | 补交受控证据引用，推进到审核 |
| `claim_after_sale` | agent | 原子认领，避免两个客服同时处理 |
| `release_or_recover_claim` | agent/system | 按受控规则释放或回收认领 |
| `submit_review` | agent | 提交审核意见，不能直接批准退款 |
| `enter_customer_confirmation` | agent/system | 仅在确定性资格结果为 eligible 时进入确认 |
| `confirm_auto_refund` | customer | 验证确认、版本和有效期，进入退款处理编排 |
| `finance_approve` | finance | 通过财务审批，进入退款处理编排 |
| `finance_reject` | finance | 驳回申请 |
| `expire_customer_confirmation` | system/worker | 确认超时后进入 `EXPIRED` |
| `record_refund_processing` | worker/system | 记录退款开始处理 |
| `record_refund_callback` | callback/worker | 接受已验证的 Mock/未来支付结果 |

其中后四类会在 S3-04/S3-05 接入完整行为。S3-02 先冻结它们的边界和状态守卫，
不提前实现支付调用。

### 3.4 阶段命令范围（冻结）

| 命令 | S3-02 | 后续阶段 |
|---|---|---|
| `claim_after_sale`、`release_or_recover_claim`、`request_evidence`、`complete_evidence`、`cancel_after_sale`、`submit_review` | 实现领域服务、store、事务与测试 | S3-03 通过 HTTP API 暴露 |
| `finance_approve`、`finance_reject` | 实现状态守卫、职责分离、Refund/Outbox 原子编排与测试 | S3-03 通过 HTTP API 暴露；S3-04 消费 Outbox |
| `submit_after_sale` | 只定义命令类型、结构校验和异常契约 | S3-05 接入 EligibilityProvider 后实现资格分流；S3-03 再暴露 API |
| `enter_customer_confirmation`、`confirm_auto_refund`、`expire_customer_confirmation` | 只定义接口与状态守卫 | S3-05 实现确定性资格、确认有效期与自动路径 |
| `record_refund_processing`、`record_refund_callback`、`record_refund_failure`、`record_reconciliation_exception` | 只定义接口、actor 和状态守卫 | S3-04 由 Mock worker / Mock callback 实现调用与故障演练 |

任何不在“实现领域服务”一列的命令，S3-02 不得提前写入数据库或调用支付适配器。

## 4. 事实提供者边界

### 4.1 唯一接口

S3-02 使用只读的 `OrderEligibilityFactProvider` 端口。它返回的事实至少包括：

```text
PaymentFact
  payment_channel
  payment_transaction_ref
  payment_succeeded
  amount_cents
  currency = CNY
  paid_at

FulfillmentFact
  fulfillment_status = UNSHIPPED | SHIPPED | DELIVERED | ...
  shipped_at
  delivered_at
```

订单归属由应用服务通过受控 store 的 `orders.customer_user_id` 校验；Provider 只提供支付和
履约事实。金额是整数分，且支付事实来源必须携带支付流水引用。

### 4.2 使用规则

- S3-02 只消费事实，不修改订单、支付或履约事实；
- `orders.status` 不得参与“是否已发货”判断；
- `orders.paid_amount`、`payment_time` 不得作为 D06 的生产支付事实；
- 当前 `customer_user_id IS NULL` 的订单直接返回内部不可用/未匹配结果，不能创建
  首版退款申请；
- provider 缺失、超时或返回不完整事实时必须 fail-closed：不创建售后申请、Refund 或
  Outbox。由于当前 schema 要求可信支付流水引用，不能伪造申请后再“转财务”；S3-03 只返回
  安全的资源/依赖不可用语义，真实人工核验入口另行设计。

Mock provider 只能服务独立合成测试夹具，不得读取或伪装业务库的 50,000 条历史
订单。

S3-02 还只定义、但不实现 `EligibilityProvider` 端口。它接收已验证的 PaymentFact、
FulfillmentFact 和申请上下文，返回受控的 `EligibilityResult`（`AUTO` / `FINANCE`、
`ELIGIBLE` / `INELIGIBLE` / `INDETERMINATE`、reason code、policy version）。只有 S3-05
实现“支付后 7 个自然日、未发货、全额、金额阈值”等 D01/D02 政策；S3-02 不得在服务层
复制这些数值或临时规则。

## 5. 状态机实施规则

### 5.1 AfterSaleRequest 状态

只允许以下边：

```text
SUBMITTED -> EVIDENCE_PENDING
SUBMITTED -> UNDER_REVIEW
SUBMITTED -> CANCELLED
EVIDENCE_PENDING -> EVIDENCE_PENDING
EVIDENCE_PENDING -> UNDER_REVIEW
EVIDENCE_PENDING -> CANCELLED
UNDER_REVIEW -> PENDING_CUSTOMER_CONFIRMATION
UNDER_REVIEW -> PENDING_FINANCE_APPROVAL
UNDER_REVIEW -> CANCELLED
PENDING_CUSTOMER_CONFIRMATION -> REFUND_PROCESSING
PENDING_CUSTOMER_CONFIRMATION -> CANCELLED
PENDING_CUSTOMER_CONFIRMATION -> EXPIRED
PENDING_FINANCE_APPROVAL -> REFUND_PROCESSING
PENDING_FINANCE_APPROVAL -> REJECTED
REFUND_PROCESSING -> REFUNDED
REFUND_PROCESSING -> REFUND_EXCEPTION
```

禁止跳转包括：

- `REJECTED`、`EXPIRED`、`CANCELLED` 直接恢复为处理中；
- 客服直接进入 `REFUND_PROCESSING`；
- 客户直接进入 `REFUND_PROCESSING`；
- Agent/LLM 直接写入 `PENDING_CUSTOMER_CONFIRMATION` 或退款金额；
- 通过修改订单 `status` 代替售后状态变化。

### 5.2 关键守卫

| 场景 | 必须满足 |
|---|---|
| 新申请 | 客户是已确认归属的订单所有者；支付和履约事实由 provider 提供；金额只从 PaymentFact 派生；有效重复申请由唯一约束/应用服务拒绝。D01/D02 的时间、未发货、金额阈值及分流判断只由 S3-05 的 EligibilityProvider 执行 |
| EligibilityResult | `ELIGIBLE` 进入客户确认；事实完整但不满足自动资格的 `INELIGIBLE` 进入财务审批；`INDETERMINATE` 仅表示事实缺失、Provider 不可用或无法验证，不创建申请、不改变状态、不写 Outbox，返回受控依赖/资源不可用语义，不能擅自转财务 |
| 进入证据补充 | 必须记录原因码和补证截止时间；最多 2 次，每次 72 小时 |
| 取消 | 仅允许 `SUBMITTED`、`EVIDENCE_PENDING`、`UNDER_REVIEW`、`PENDING_CUSTOMER_CONFIRMATION`；客户必须是申请所有者 |
| 重新申请 | 仅 `CANCELLED` 可在原 7 日期限内重新申请一次；`REJECTED`/`EXPIRED` 不走自动闭环 |
| 进入客户确认 | 必须有可信的确定性资格结果；不能来自 Agent 文本或客户端字段；记录策略版本 |
| 客户确认 | 申请仍处于确认态、未过期、版本匹配；客户必须是申请所有者；金额由支付事实计算，不能由请求体指定 |
| 财务审批 | 仅 `PENDING_FINANCE_APPROVAL`；actor 为 finance；不得与客服审核人为同一人 |
| 退款处理 | 只能由客户确认自动路径或 finance 审批路径触发；同一申请不能产生第二个有效退款执行记录 |
| 回调收敛 | 只能由已验签/已验证的回调或对账结果改变退款成功状态；worker 投递成功不等于到账成功 |

### 5.3 Refund 状态

```text
CREATED -> PROCESSING -> SUCCEEDED
                    \-> FAILED
                    \-> RECONCILIATION_EXCEPTION
```

`Refund.SUCCEEDED` 只能由已验证 callback 或后续对账收敛。`Outbox.SUCCEEDED` 只
表示支付适配器已成功接收/投递退款请求，不代表资金到账。

`Refund.FAILED` 与 `Refund.RECONCILIATION_EXCEPTION` 必须在同一应用服务事务中把关联
`AfterSaleRequest` 收敛为 `REFUND_EXCEPTION`，并追加审计；不得继续保留
`REFUND_PROCESSING` 造成两个聚合互相矛盾。`record_refund_failure` 与
`record_reconciliation_exception` 是 S3-02 冻结状态守卫的内部命令，Mock 适配器实际调用
与失败分类实现属于 S3-04。

这两个机器命令只允许 `WORKER`、`PAYMENT_GATEWAY` 或 `SYSTEM` actor 调用，且 Refund 必须
处于 `PROCESSING`。它们不接受客户端 `expected_version`：服务在同一事务内锁定 Refund 行并按
当前状态条件更新；重复的已去重 callback 返回已处理语义而不再次变更或追加第二条回调审计。
人类客户、客服和财务不能调用它们；finance 对异常的最终关闭职责在 S3-04 及后续待办模型中实现。

## 6. 事务、乐观锁与并发

### 6.1 单命令事务边界

会改变领域状态的服务命令必须在同一个数据库连接和事务内完成：

```text
开始事务
  -> 读取并锁定目标售后申请 / 退款行
  -> 校验 actor、状态、事实、版本、幂等条件
  -> 更新领域状态并 version + 1
  -> 必要时创建 refund / outbox
  -> 追加 audit_event
提交事务
```

任何一步失败都回滚全部写入。store 不得在一个命令中自行从连接池借出多个彼此
独立的连接，也不得在事务外写审计或 Outbox。

内部接口固定为：Application Service 只借出一次 `AsyncConnection` 并开启一次事务；所有
after-sale、refund、audit 与 outbox store 写方法都显式接收该 connection，禁止 store 自行
从连接池借连接或自行 commit/rollback。事务的 commit/rollback 只由 Application Service
负责。

### 6.2 `expected_version`

- 除首次 `submit_after_sale` 外，所有改变既有资源的命令都必须由调用方带入；不能在服务层因
  客户端省略而自动覆盖为最新版本；
- 应用服务在锁定行后比较 `expected_version == current_version`；
- 不相等时返回领域层 `VERSION_CONFLICT`，不更新状态、不创建 Refund/Outbox；
- 锁释放后客户端重新读取资源并发起新命令；
- 版本冲突本身也应记录安全审计事件，但不得记录证据原文、支付签名或完整 PII。

### 6.3 两名 finance 并发审批

两次审批必须竞争同一申请行：

1. `SELECT ... FOR UPDATE` 锁定 `PENDING_FINANCE_APPROVAL` 申请；
2. 第一笔事务通过版本和状态检查，原子写入 `REFUND_PROCESSING`、Refund、Outbox、
   审计；
3. 第二笔事务等待锁后重新读取，发现状态/版本已变化，返回
   `VERSION_CONFLICT` 或 `INVALID_STATE`；
4. 唯一约束和应用服务二次检查保证不会出现第二个有效 Refund 或第二个退款
   Outbox。

不以“前端按钮禁用”或应用进程内锁作为唯一保护。

### 6.4 隔离级别

首版使用 PostgreSQL 默认事务隔离级别配合行锁、条件更新和唯一约束。若测试发现
死锁或序列化冲突，服务层必须将可安全重试的数据库冲突转换为受控领域错误；不能
盲目重试包含资金副作用的完整命令。

## 7. 幂等与重复命令

### 7.1 客户/内部命令

- 所有客户和内部**人类角色**发起的状态变更命令要求 `Idempotency-Key`；worker、system
  与 callback 按本节后文的领域唯一键、状态/version 或 callback 三元组保证重放安全；
- 首版的持久化作用域固定为 `(actor_user_id, command_name, idempotency_key)`；这与
  `audit_events` 的部分唯一索引一致。调用方不得在同一 actor/命令下复用同一 key 操作不同资源；
  这会安全地返回冲突而非冒险执行；
- 首次成功提交时，保存命令结果所需的安全摘要，并与状态写入保持同一事务；
- 相同作用域、相同 key、相同请求摘要的重放返回首次结果，不再次改变状态、不再次
  创建 Refund/Outbox；
- 相同 key 但请求摘要不同返回 `IDEMPOTENCY_CONFLICT`，不执行命令；
- 幂等重放追加 `IDEMPOTENCY_REPLAY` 审计：该追加事件的 `idempotency_key` 必须为 `NULL`，
  并只在白名单 metadata 中关联首次审计事件的内部 UUID，避免触发首次命令的唯一索引；
- 首版明确以 `audit_events` 中的 `request_hash`、`result_resource_type`、
  `result_resource_id`、`result_version` 作为**有人类 actor 的命令**幂等记录；相同 hash 的
  响应必须通过这些结果定位字段重新查询构建，不能依赖进程内缓存。worker/system 不使用
  客户端 Idempotency-Key：worker 由唯一 Refund、merchant request number 和 Outbox key 保证
  重放安全，system 定时任务由状态/version 条件更新保证安全；callback 只使用其三元组去重。
  若未来要求保存不可重建的原始 HTTP 响应或机器命令的通用幂等键，必须新增专用表和迁移，
  不能改变本契约。

### 7.2 外部 callback

callback 不是普通客户命令，不要求 `Idempotency-Key`。去重依据为稳定组合：

```text
external_event_id + merchant_refund_request_no + external_refund_id
```

重复或乱序 callback 必须返回幂等成功/已处理语义，不重复生成退款、不重复创建
Outbox、不把已成功状态倒退为处理中。未能确认外部退款是否已发生时，先查询/对账，
再决定是否用相同商户退款请求号重试。

## 8. 审计写入规则

每个状态变化、拒绝、版本冲突和幂等重放都要追加审计，至少包含：

- actor 类型和内部用户 ID（按数据分级存储，不写日志）；
- 时间；
- source、request ID、trace ID；
- 资源类型、资源内部 ID；
- action/command；
- `from_status`、`to_status`；
- reason code；
- policy version（若涉及资格或人工审核）；
- 版本变化或冲突结果；
- 安全的结构化元数据摘要。`source` 固定作为受控 metadata 的 `source` 字段（枚举
  `api` / `agent` / `worker` / `callback` / `system`），只能由应用服务构造，调用者不能传入
  任意 metadata。

禁止写入：

- 证据原文或文件内容；
- 支付签名；
- 完整支付凭证；
- 完整手机号、地址、聊天原文；
- LLM 原始 prompt/response；
- 外部供应商异常原文。

S3 首版只承诺应用层追加式审计：应用角色没有更新/删除入口，但高权限数据库
管理员仍可能修改；不能称为金融级不可篡改。

申请创建前的拒绝也必须审计：此时固定使用 `resource_type = COMMAND`，并由应用服务生成
新的内部 UUID 写入 `resource_id`；不得把客户提供的订单号、手机号或任意外部 ID 填入该字段。
已存在资源的拒绝必须使用其实际资源类型与内部 UUID。

`audit_events.metadata` 只允许服务层写入以下白名单字段：`source`、`conflict_version`、
`first_audit_event_id`、`outbox_event_id`、`retry_attempt`、`provider_reason_code`。键和值均由
内部枚举/类型构造，禁止 API、Agent、worker 传入任意 dict。

`outbox_events.payload` 必须由固定内部类型生成，至少包含 `outbox_event_id`、`refund_id`、
`after_sale_request_id`、`merchant_refund_request_no`、`payment_transaction_ref`、
`amount_cents`、`currency`、`attempt` 和 `created_at`；不允许任意 JSON、PII、证据、聊天内容
或支付签名。

## 9. Outbox 编排边界

S3-02 只允许应用服务在以下两个业务结果成立且同一事务内创建退款 Outbox：

1. 确定性资格评估合格，且客户已完成二次确认；
2. finance 已批准申请。

以下情况禁止写 Outbox：

- 客户仅提交申请但未确认；
- Agent 或 LLM 给出建议但未经过受控资格评估；
- 客服认领或提交审核意见；
- provider 不可用、事实不完整或订单 UNMATCHED；
- 客户取消、申请拒绝、确认过期。

Outbox 至少需要由服务层保证：

- 业务幂等键/退款请求号唯一；
- `PENDING`、`PROCESSING`、`SUCCEEDED`、`DEAD` 状态转换受控；
- 尝试次数、下一次尝试时间、最后错误分类、锁定时间和死信时间可追踪；
- `SUCCEEDED` 只表示适配器接收/投递成功，不表示退款到账；
- 超时、重试、worker 重启前先查询/对账，不创建新的外部退款请求号。

实际 worker 消费与 Mock callback 演练放到 S3-04；S3-02 只提供事务编排契约和
状态守卫。

## 10. 实施顺序（交给组员执行）

### Step 1：冻结内部契约

- 先提交服务方法、Actor、事实快照、命令结果和领域异常的草案；
- 画出状态边集合并让测试逐边覆盖；
- 明确哪些方法由 S3-02 实现，哪些只为 S3-03～S3-05 预留；
- 发现 schema 无法支撑幂等/回调唯一性时，先停止编码并上报阻塞项。
- 冻结 `Clock.now()` 端口：所有领域时间都使用 UTC；服务禁止直接调用 `datetime.now()`。
  S3-02 对补证 72 小时、认领 24 小时等机制使用可注入时钟；“支付后 7 个自然日”的具体
  时区、包含边界和计算规则属于 S3-05 EligibilityProvider 的冻结事项。

### Step 2：实现 store 的最小原子操作

- 所有 SQL 参数化；
- 明确 `FOR UPDATE`、条件更新、版本递增和唯一约束失败的映射；
- store 不接受任意列名或任意状态字符串；
- store API 不暴露给 API/Agent/worker 作为绕过应用服务的写入入口。

### Step 3：实现应用服务事务编排

- 统一事务上下文；
- 先做资源范围和事实校验，再做状态转换；
- 状态、Refund、Outbox、审计要么全部提交，要么全部回滚；
- 将支付/履约事实视为只读 provider 依赖；
- 不在服务层调用 LLM，也不接受 LLM 决定金额、资格或状态。

### Step 4：实现审计与异常语义

- 为成功转换、拒绝、冲突、幂等重放生成安全审计；
- 领域层不依赖 FastAPI，不直接返回 HTTP 响应；
- S3-03 再把领域异常映射为已冻结的 API 错误码；
- 日志只记安全摘要和 request/trace 关联，不复制审计中的敏感字段。

### Step 5：并发与回滚测试

- 使用独立合成数据和事务并发测试；
- 测试失败事务不会留下半个状态、Refund、Outbox 或审计；
- 测试 worker/回调尚未实现时，应用服务不会错误地把 Outbox 成功当退款成功；
- 数据库集成测试只能在 DBA 已准备、且负责人已授权的 `ecommerce_agent_s3_test` 上运行；
  先只读确认 revision 为 `a7d1e8f4c902`。任何测试夹具写入必须是合成数据、在报告中列出范围，
  且不得连接业务库 `postgres`。

## 11. 测试清单

### 11.1 状态图

- 每条合法 AfterSaleRequest 边至少一个测试；
- 每个非法边至少一个测试；
- Refund 三类最终结果和不可逆边测试；
- `CANCELLED` 原 7 日期限内只能重新申请一次；
- `REJECTED`/`EXPIRED` 不能重新进入自动闭环；
- 客户取消四个允许状态和其余拒绝状态；
- 补证最多 2 次、每次 72 小时、过期后行为；
- 客服审核不能直接批准或进入退款处理。

### 11.2 权限与归属

- 客户只能操作自己已确认归属的申请；
- UNMATCHED 订单不能创建首版申请；
- 客服只能操作自己认领的申请；
- finance 只能审批 `PENDING_FINANCE_APPROVAL`；
- 同一人不能同时提交审核意见并审批同一申请；
- admin/operator/Agent 不获得财务审批或资金执行能力；
- worker/callback 只能使用受控服务身份和对应命令。

### 11.3 一致性与并发

- `expected_version` 缺失、过期和冲突；
- 两名 finance 同时审批仅一人成功；
- 重复命令不重复状态、Refund 或 Outbox；
- 相同幂等键不同请求摘要被拒绝；
- 事务中任意一步失败后全部回滚；
- S3-02 仅测试 callback 进入领域服务时的调用边界和非法状态拒绝；重复、乱序、超时、查询与
  对账后的 callback 场景由 S3-04 的 Mock Payment Adapter 验收；
- provider 超时或事实不完整时 fail-closed；
- 证明没有读取旧 `orders.status` 作为履约判断。

### 11.4 审计与数据分级

- 每个成功状态变化有一条审计；
- 拒绝、版本冲突、幂等重放有审计；
- 审计含 request/trace 关联和状态前后值；
- 审计、日志、异常和测试输出不包含证据原文、支付签名、完整 PII 或供应商异常；
- 审计只有追加入口，没有应用层更新/删除入口。

## 12. 验收证据格式

组员提交 S3-02 时必须附以下证据，不得只报“测试通过”：

```text
变更范围：列出新增/修改文件；确认未修改迁移和外部支付集成
数据库：目标库名、alembic current、是否执行迁移、是否写入合成夹具
状态机：合法边数量/非法边数量/通过数
权限：customer/agent/finance/worker 各类越权测试结果
并发：双 finance、版本冲突、重复命令结果
一致性：失败事务前后行数与关联记录核对
审计：状态变化/拒绝/冲突/重放覆盖，敏感字段扫描结果
静态检查：ruff、mypy、compileall、git diff --check
测试命令：逐条给出完整可复制命令和原始摘要
未决阻塞：明确影响哪个后续阶段，不用“暂时忽略”代替决策
```

建议的纯代码验证命令为：

```bash
ruff check src tests
mypy src
python -m compileall -q src
git diff --check
pytest -q tests/test_s3_02_*.py
```

数据库相关测试必须显式使用以 `_test` 结尾的数据库，并报告迁移版本。S3-02 不得
连接或写入当前业务库 `postgres`。

## 13. 当前阻塞项与风险登记

1. **首次 HTTP 响应可重建是首版前提。** 现有 `audit_events` 只保存结果资源定位与版本，
   因此 S3-03 的成功响应必须能从该资源安全重建；若接口需要保存不可重建响应，必须新增
   `idempotency_records` 迁移，禁止用进程内内存实现。
2. **callback 去重字段需核对实际 migration。** 设计要求外部事件 ID、商户退款请
   求号和外部退款流水的稳定组合；若 `refunds` 现有唯一约束不能表达组合，必须在
   S3-04 前补充设计，不能靠应用层查询后插入。
3. **支付后 7 个自然日的精确定义留待 S3-05 冻结。** 必须明确业务时区、截止边界和
   支付时间缺失时的结果；S3-02 不得自行作出政策判断。
4. **证据引用的生命周期尚未实现。** S3-02 只传递受控引用和次数/截止时间，不落
   证据原文；对象存储、病毒扫描和 180 天保留是后续独立任务。
5. **S3-05 才提供确定性资格结果。** S3-02 不得自行写一套临时规则，也不得回退
   到旧订单字段。
6. **真实支付事实留到 S5。** S3 的 Mock provider 只服务隔离合成夹具，不能把
   S3 测试结果描述为真实支付闭环。

## 14. S3-02 完成标准

只有同时满足以下条件，S3-02 才能验收并进入 S3-03：

- 所有领域状态写入都经过应用服务，API/Agent/worker 没有直写路径；
- 合法和非法状态边、权限、归属、取消、补证和职责分离均有测试；
- `expected_version` 和双 finance 并发只有一个胜出；
- 事务失败不会留下半个业务结果；
- 重复命令、版本冲突、拒绝和状态变化均有追加式审计；
- Outbox 只从客户确认或 finance 批准路径产生，且不把投递成功当退款成功；
- 支付/履约事实来自 provider 端口，不使用旧模拟字段推断；
- 幂等、callback 唯一性与 Refund 失败后的聚合收敛均按本契约实现；
- `ruff`、完整 `mypy src`、编译检查、受影响测试和数据边界证据均通过；
- 未执行迁移、未修改业务库、未接入真实支付。
