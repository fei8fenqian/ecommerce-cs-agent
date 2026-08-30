# S3-01 设计包：售后、退款、审计与 Outbox 数据模型

状态：设计草案，待架构验收

范围：只做 S3 设计审查与 S3-01 迁移准备。本文件不执行数据库迁移、不修改业务代码、不接支付宝或任何真实支付接口。

权威来源：

- [`S0_DECISION_RECORD.md`](../../product/refund/S0_DECISION_RECORD.md) 的 D01–D12；
- [`S0-01_PRD_REFUND_WORKBENCH.md`](../../product/refund/S0-01_PRD_REFUND_WORKBENCH.md)；
- [`S0-02_ROLE_MATRIX_STATE_MACHINES.md`](../../product/refund/S0-02_ROLE_MATRIX_STATE_MACHINES.md)；
- [`S0-03_API_SLO_DATA_CLASSIFICATION.md`](../../product/refund/S0-03_API_SLO_DATA_CLASSIFICATION.md)；
- [`PLAN_V6.md`](../../plans/PLAN_V6.md) 的 0.4、0.5、2.2 和 S3 任务拆分。

## 1. 当前订单与支付事实盘点

### 1.1 当前业务数据库状态

本次只读检查目标为当前业务库 `postgres`，Alembic 版本为 `6aa6adbb7084`。检查结果：

| 项目 | 当前事实 |
|---|---|
| `orders` | 50,000 行 |
| `order_items` | 58,961 行 |
| `users` | 0 行；测试库用户不能视为生产用户 |
| `orders.customer_user_id IS NULL` | 50,000 行，全部未确认归属 |
| 订单归属可进入退款闭环 | 0 行；当前没有任何已确认归属订单 |
| `payment_time IS NOT NULL` | 45,024 行 |
| `paid_amount > 0` | 45,024 行 |
| 现有退款表 | 不存在 |
| 现有支付流水表 | 不存在 |
| 现有售后申请表 | 不存在 |
| 现有退款 worker | 不存在 |
| 本地支付 callback endpoint | 未发现 |

当前订单 `status` 分布包含：`待付款`、`待发货`、`运输中`、`已签收`、`已完成`、`已取消`、`已退款`。该字段同时混合了支付、履约和退款语义，不能直接作为 D03 要求的独立支付状态、履约状态或售后状态。

当前 `payment_method` 包含微信支付、支付宝、花呗分期、银行卡和空值；当前模型没有货币字段，也没有支付商户号、支付流水号、支付状态版本或外部事件标识。

### 1.2 当前表结构

#### `orders`

| 字段 | 类型 | 当前用途/风险 |
|---|---|---|
| `id` | `integer` PK | 内部顺序 ID |
| `order_id` | `varchar(20)` UNIQUE NOT NULL | 旧系统订单号；`order_items` 通过它关联 |
| `customer_id` | `varchar(10)` NULL | 旧系统客户编号，不是 `users.id` 外键 |
| `customer_name` | `varchar(20)` NULL | 订单快照中的姓名，L2 |
| `order_date` | `date` NULL | 订单日期，不能单独替代支付成功时间 |
| `status` | `varchar(10)` NULL | 混合订单/支付/履约/退款状态 |
| `total_amount` | `numeric(12,2)` NULL | 订单金额；不是已确认支付流水的唯一来源 |
| `paid_amount` | `numeric(12,2)` NULL | 模拟已支付金额；不是支付流水表 |
| `discount` | `numeric(12,2)` NULL | 折扣金额 |
| `payment_method` | `varchar(20)` NULL | 支付方式文本，无外部支付流水关联 |
| `payment_time` | `timestamp without time zone` NULL | 模拟支付时间，缺少时区和外部事件关联 |
| `tracking_company` / `tracking_number` | `varchar` NULL | 物流事实；没有独立履约状态模型 |
| `shipping_address` / `phone` | `text` / `varchar` NULL | L2 个人数据 |
| `created_at` | `timestamp without time zone` NULL | 创建时间，缺少时区约束 |
| `delivered_at` | `date` NULL | 签收/送达日期 |
| `customer_user_id` | `integer` NULL，FK `users(id)` | S1 增加的真实用户归属；当前全部 NULL |

#### `order_items`

| 字段 | 类型 | 当前用途/风险 |
|---|---|---|
| `id` | `integer` PK | 行 ID |
| `order_id` | `varchar(20)` NULL，FK `orders(order_id)` | 订单关联；当前允许 NULL |
| `product_name` | `text` NULL | 商品快照 |
| `category` | `varchar(10)` NULL | 商品类别 |
| `brand` | `varchar(20)` NULL | 品牌 |
| `price` | `numeric(12,2)` NULL | 商品快照价格 |
| `quantity` | `integer` NULL | 数量 |

#### 已有归属/会话表

- `users.id` 是内部用户主键；`orders.customer_user_id` 是售后资源所有者字段。
- `sessions.owner_user_id` 是聊天会话所有者，不作为订单所有者。
- `tickets.customer_user_id` 是工单客户所有者，`tickets.assigned_agent_id` 是客服负责人。
- 售后申请必须显式保存自己的 `customer_user_id` 和客服认领字段，不能通过订单旧字段或聊天 session 推断归属。

### 1.3 当前读取入口

| 入口 | 当前行为 | S3 影响 |
|---|---|---|
| `src/store/order_store.py:find_orders()` | 按 `customer_user_id` 加 `order_id` 或手机号查询；返回订单、支付金额、支付方式、物流和商品项 | 只能作为低风险订单查询入口，不能直接承担退款资格判断 |
| `src/agent/tools/track_order.py` | Agent 调用订单只读查询；失败时返回安全错误 | Agent 不能由此直接创建退款、写状态或写 Outbox |
| `src/agent/mcp_tool.py` 的 `check_payment` 描述 | 仅存在 MCP 工具调用包装/示例 | 当前仓库没有本地支付事实存储，不能作为 S3 的支付真相 |
| `src/api/` | 当前没有售后申请、退款批准、支付 callback 路由 | S3-03/S5 才设计具体命令和 callback 实现 |
| `src/store/` | 当前没有 payment/refund/after-sale store | S3-01 只准备表结构，S3-02 以后才增加应用服务/数据访问实现 |

当前仓库未发现本地 `PaymentTransaction`、`Refund`、`AfterSaleRequest`、`refund-worker` 或支付 callback 实现。现有 `paid_amount`、`payment_time` 和 MCP `check_payment` 不能替代支付流水。

### 1.4 S3/S5 订单资格事实来源分层

支付和履约事实来源冻结为分阶段的只读接口，而不是把现有订单模拟字段升级成事实真相：

```text
OrderEligibilityFactProvider
  ├─ PaymentFact
  │    ├─ payment_channel
  │    ├─ payment_transaction_ref
  │    ├─ payment_succeeded
  │    ├─ amount_cents
  │    ├─ currency = CNY
  │    └─ paid_at
  └─ FulfillmentFact
       ├─ fulfillment_status = UNSHIPPED / SHIPPED / DELIVERED / ...
       ├─ shipped_at（可选）
       └─ delivered_at（可选）

S3：OrderEligibilityFactProvider + MockOrderEligibilityFactProvider
S5：支付宝及真实订单/物流事实适配器、支付流水与对账能力
```

`OrderEligibilityFactProvider` 至少要同时提供支付事实和履约事实。S3-02/S3-05 判断 D01/D02 时，必须使用 `payment_succeeded`、`amount_cents`、`currency`、`paid_at` 和 `fulfillment_status`；不得从旧 `orders.status` 推断是否已发货。

S3 的 `MockOrderEligibilityFactProvider` 只能读取独立测试夹具中明确建立的支付与履约事实，不能读取或包装当前 50,000 条历史订单的 `paid_amount/payment_time/status` 来伪装真实支付或履约，也不能服务当前业务库。S3 应用服务不得在 Provider 不可用时回退到订单模拟字段。

S5 才负责支付宝适配器、真实支付流水、真实订单/物流事实、签名校验、callback 和对账。当前 50,000 条 `UNMATCHED` 订单永久不进入首版退款闭环，除非未来另行批准独立人工核验和归属流程；本计划不自动改变这一点。

## 2. S3-01 目标与共同约束

### 2.1 四张表的边界

S3-01 只新增以下四张表：

- `after_sale_requests`：客户售后申请及人工/自动路径的领域状态；
- `refunds`：一笔全额退款的执行事实和外部支付标识；
- `audit_events`：应用层追加式审计事件，同时保存命令幂等索引和 callback 去重所需的安全标识；
- `outbox_events`：已经获得客户确认或财务批准的退款投递任务。

不在本次迁移中新增 `payments`、`payment_transactions`、`idempotency_records`、`evidence_files` 或通知表。它们若被证明是冻结规则的必需事实来源，登记为阻塞项，不能通过偷偷扩大 S3-01 范围解决。

### 2.2 通用数据规范

- 主键使用应用生成的 UUID；不依赖当前数据库尚未启用的 UUID 默认扩展。
- 时间字段统一使用 `timestamptz`，应用按 UTC 写入和读取。
- 金额字段使用 `bigint`，单位为分；禁止 `float` 和 `numeric(12,2)` 作为新退款金额字段。
- `currency` 使用 `char(3)`，并设置 `CHECK (currency = 'CNY')`。
- 状态使用 `varchar` 加 `CHECK`，暂不使用 PostgreSQL enum，便于后续受控扩展和迁移。
- 所有状态、金额、归属和外部流水校验由应用服务执行；数据库约束负责最后一道一致性保护。
- 不引入空壳 `tenant_id`。
- 原始证据、支付签名、完整支付报文、密码、token 和未脱敏 PII 不进入普通表的日志字段或 `metadata`。

## 3. `after_sale_requests` 逐字段设计

### 3.1 字段

| 字段 | 类型/约束 | 设计用途 |
|---|---|---|
| `id` | `uuid` PK | 售后申请内部 ID；由应用生成，不使用可枚举 ID |
| `order_id` | `varchar(20)` NOT NULL，FK `orders(order_id)` `ON DELETE RESTRICT` | 关联订单；不改变旧订单结构，不自动匹配历史订单 |
| `customer_user_id` | `integer` NOT NULL，FK `users(id)` `ON DELETE RESTRICT` | 客户资源所有者；创建时必须与订单已确认归属一致 |
| `assigned_agent_id` | `integer` NULL，FK `users(id)` `ON DELETE SET NULL` | 当前认领客服；只由原子认领/回收服务更新 |
| `status` | `varchar(40)` NOT NULL | `SUBMITTED`、`EVIDENCE_PENDING`、`UNDER_REVIEW`、`PENDING_CUSTOMER_CONFIRMATION`、`PENDING_FINANCE_APPROVAL`、`REFUND_PROCESSING`、`REFUNDED`、`REJECTED`、`CANCELLED`、`EXPIRED`、`REFUND_EXCEPTION` |
| `currency` | `char(3)` NOT NULL DEFAULT `'CNY'`，CHECK | 首版只能是 CNY |
| `payment_transaction_ref` | `varchar(128)` NOT NULL | `OrderEligibilityFactProvider.PaymentFact` 返回的支付流水引用；不是客户输入。当前系统没有 Provider 实现，因此 S3-01 只能先冻结字段 |
| `payment_amount_cents` | `bigint` NOT NULL，CHECK `>= 0` | 从 `PaymentFact` 返回的支付金额快照，不得从 `orders.paid_amount` 推断 |
| `refund_amount_cents` | `bigint` NOT NULL，CHECK `>= 0` | 全额退款金额；数据库 CHECK 要求等于 `payment_amount_cents` |
| `reason_code` | `varchar(64)` NOT NULL | 受控退款原因码；不把自由文本作为资格规则 |
| `customer_note` | `text` NULL | 客户补充说明，L2；禁止进入日志和普通 AI prompt |
| `evidence_refs` | `jsonb` NOT NULL DEFAULT `'[]'` | 仅保存对象引用、文件类型、大小、哈希等白名单元数据，不保存证据原文 |
| `evidence_round` | `smallint` NOT NULL DEFAULT `0`，CHECK `0..2` | 已使用补证次数；首提不计为补证，最多补证 2 次 |
| `evidence_due_at` | `timestamptz` NULL | 当前补证截止时间，D04/D11 为每次 72 小时 |
| `qualification_path` | `varchar(16)` NULL，CHECK | `AUTO` 或 `FINANCE`；只能由应用服务根据策略结果写入 |
| `qualification_result` | `varchar(24)` NULL | `ELIGIBLE`、`INELIGIBLE`、`INDETERMINATE` 等内部确定性结果 |
| `policy_version` | `varchar(64)` NULL | 做过资格评估时的政策版本 |
| `facts_snapshot` | `jsonb` NULL | 已按白名单保存的事实快照；不得保存原始证据、支付签名或完整个人数据 |
| `quote_expires_at` | `timestamptz` NULL | 自动退款报价有效期；过期后只能按状态机进入 `EXPIRED`/人工入口 |
| `customer_confirmed_at` | `timestamptz` NULL | 客户二次确认时间 |
| `reviewed_by_agent_id` | `integer` NULL，FK `users(id)` `ON DELETE RESTRICT` | 最终提交客服审核意见的客服；用于 D07 职责分离校验 |
| `reviewed_at` | `timestamptz` NULL | 客服审核意见提交时间 |
| `finance_decided_by` | `integer` NULL，FK `users(id)` `ON DELETE RESTRICT` | 财务审批/驳回人；不能与 `reviewed_by_agent_id` 相同 |
| `finance_decided_at` | `timestamptz` NULL | 财务决策时间 |
| `decision_reason_code` | `varchar(64)` NULL | 审核、驳回或异常关闭的受控原因码 |
| `reapplication_of_id` | `uuid` NULL，FK `after_sale_requests(id)` `ON DELETE RESTRICT` | `CANCELLED` 后原 7 日期限内重新申请时指向原申请 |
| `version` | `bigint` NOT NULL DEFAULT `0`，CHECK `>= 0` | 聚合乐观锁版本；每次成功状态/关键字段变更加 1 |
| `submitted_at` | `timestamptz` NOT NULL | 客户首次提交时间；D12 处理时长起点 |
| `claimed_at` | `timestamptz` NULL | 当前客服认领时间 |
| `claim_expires_at` | `timestamptz` NULL | 认领回收时间；D08 为 24 小时，进入财务审批或退款处理中后不再回收 |
| `created_at` | `timestamptz` NOT NULL | 创建时间 |
| `updated_at` | `timestamptz` NOT NULL | 最近一次应用服务更新时间 |
| `closed_at` | `timestamptz` NULL | 进入 `REFUNDED`、`REJECTED`、`CANCELLED`、`EXPIRED` 或 `REFUND_EXCEPTION` 的时间 |

### 3.2 约束与索引

- 主键：`after_sale_requests_pkey (id)`。
- 外键：`order_id → orders(order_id)`、`customer_user_id → users(id)`、`assigned_agent_id → users(id)`、`reviewed_by_agent_id → users(id)`、`finance_decided_by → users(id)`、`reapplication_of_id → after_sale_requests(id)`。
- 状态 CHECK 只允许状态机中的值；数据库不允许任意字符串状态。
- `currency = 'CNY'`、金额非负、`refund_amount_cents = payment_amount_cents`。
- 客户同一订单的有效申请使用部分唯一索引：`order_id` 仅在 `SUBMITTED`、`EVIDENCE_PENDING`、`UNDER_REVIEW`、`PENDING_CUSTOMER_CONFIRMATION`、`PENDING_FINANCE_APPROVAL`、`REFUND_PROCESSING` 时唯一，防止重复有效申请，同时允许 `CANCELLED` 后按 D04 重新申请。
- `reapplication_of_id` 使用非空部分唯一索引，保证一次取消申请最多被重新申请一次。
- 索引：`(customer_user_id, created_at DESC)`、`(status, created_at)`、`(assigned_agent_id, status, claim_expires_at)`、`(order_id)`、`(payment_transaction_ref)`。
- 不对 `customer_note`、`facts_snapshot` 或 `evidence_refs` 建全文索引，避免扩大敏感数据可检索范围。

### 3.3 数据归属

客户按 `customer_user_id` 读取自己的申请；客服只能按 `assigned_agent_id` 读取完整内容，未认领时只能读取脱敏摘要；财务读取 `PENDING_FINANCE_APPROVAL` 及后续关联记录；运营只读取脱敏聚合；admin 默认不读取订单 PII；worker 只通过授权的退款关联读取必要字段。

## 4. `refunds` 逐字段设计

### 4.1 字段

| 字段 | 类型/约束 | 设计用途 |
|---|---|---|
| `id` | `uuid` PK | 退款单内部 ID |
| `after_sale_request_id` | `uuid` NOT NULL UNIQUE，FK `after_sale_requests(id)` `ON DELETE RESTRICT` | 首版一申请一退款单，保证全额退款不会产生第二张有效退款单 |
| `payment_transaction_ref` | `varchar(128)` NOT NULL | 支付流水引用快照；必须与售后申请一致 |
| `merchant_refund_request_no` | `varchar(128)` NOT NULL UNIQUE | 应用生成的稳定商户退款请求号；重试必须复用，不得每次生成新号 |
| `currency` | `char(3)` NOT NULL，CHECK | 只能是 CNY |
| `amount_cents` | `bigint` NOT NULL，CHECK `> 0` | 由支付流水和售后申请计算，不接受客户/Agent/LLM 输入 |
| `status` | `varchar(32)` NOT NULL | `CREATED`、`PROCESSING`、`SUCCEEDED`、`FAILED`、`RECONCILIATION_EXCEPTION` |
| `version` | `bigint` NOT NULL DEFAULT `0` | 退款聚合乐观锁版本 |
| `external_refund_id` | `varchar(128)` NULL | 支付方外部退款流水号；成功受理或回调匹配后写入 |
| `last_external_event_id` | `varchar(128)` NULL | 最近一次已接受的外部事件标识；完整去重历史在审计事件中保存 |
| `last_external_status` | `varchar(64)` NULL | 支付方状态的安全规范化值，不保存原始报文 |
| `failure_reason_code` | `varchar(64)` NULL | 内部受控失败原因码，不保存供应商异常原文 |
| `retryable` | `boolean` NOT NULL DEFAULT `false` | 应用服务根据确定性错误分类设置；worker 不自行猜测 |
| `created_at` | `timestamptz` NOT NULL | 退款单创建时间 |
| `processing_at` | `timestamptz` NULL | worker 首次受控发起时间 |
| `succeeded_at` | `timestamptz` NULL | 成功收敛时间 |
| `failed_at` | `timestamptz` NULL | 确定性失败时间 |
| `last_callback_at` | `timestamptz` NULL | 最近一次通过校验的 callback 时间 |
| `reconciliation_due_at` | `timestamptz` NULL | 需要财务对账/异常处理的时间 |
| `updated_at` | `timestamptz` NOT NULL | 最近更新时间 |

### 4.2 约束与索引

- `after_sale_request_id` 唯一，确保首版全额退款一申请一退款单。
- `merchant_refund_request_no` 全局唯一，重试、worker 重启和 callback 重放都复用同一值。
- `external_refund_id` 建非空部分唯一索引；不同退款单不能绑定同一个外部退款流水。
- `status` 只允许 Refund 状态机值。
- `amount_cents > 0`、`currency = 'CNY'`；应用服务还必须校验它等于售后申请金额且不超过可退余额。
- 索引：`(status, created_at)`、`(last_callback_at)`、`(reconciliation_due_at)`。

### 4.3 数据归属

退款通过 `after_sale_request_id → customer_user_id` 归属；客户只读自己的安全状态，客服不审批资金，finance 处理审批和异常，worker 只处理授权 Outbox 关联的退款记录。支付凭证和原始 callback 报文不进入普通退款表。

## 5. `audit_events` 逐字段设计

### 5.1 字段

| 字段 | 类型/约束 | 设计用途 |
|---|---|---|
| `id` | `uuid` PK | 追加式审计事件 ID；数据库管理员仍可能修改，不能宣称金融级不可篡改 |
| `resource_type` | `varchar(40)` NOT NULL | `AFTER_SALE_REQUEST`、`REFUND`、`OUTBOX_EVENT` 等 |
| `resource_id` | `uuid` NOT NULL | 关联资源 ID；不使用可枚举业务号作为主键 |
| `owner_user_id` | `integer` NULL，FK `users(id)` `ON DELETE RESTRICT` | 资源所有者快照，用于资源范围读取 |
| `actor_type` | `varchar(24)` NOT NULL | `CUSTOMER`、`AGENT`、`FINANCE`、`OPERATOR`、`ADMIN`、`WORKER`、`PAYMENT_GATEWAY`、`SYSTEM` |
| `actor_user_id` | `integer` NULL，FK `users(id)` `ON DELETE RESTRICT` | 人类操作者；worker、网关和系统主体为空 |
| `action` | `varchar(64)` NOT NULL | 受控动作，如 `SUBMIT`、`CLAIM`、`REVIEW`、`APPROVE`、`CALLBACK_ACCEPTED` |
| `from_status` | `varchar(40)` NULL | 状态转换前状态 |
| `to_status` | `varchar(40)` NULL | 状态转换后状态 |
| `expected_version` | `bigint` NULL | 命令提交时的版本 |
| `new_version` | `bigint` NULL | 成功状态转换后的新版本 |
| `reason_code` | `varchar(64)` NULL | 受控原因码；`UNMATCHED_ORDER` 只能作为内部原因码 |
| `policy_version` | `varchar(64)` NULL | 资格评估或审核使用的政策版本 |
| `request_id` | `varchar(128)` NULL | 关联 HTTP/API 请求；来自当前请求上下文 |
| `trace_id` | `varchar(128)` NULL | 关联跨服务链路；不含凭证 |
| `span_id` | `varchar(64)` NULL | 可选的当前操作 span |
| `command_name` | `varchar(128)` NULL | 幂等命令路径/规范化动作名 |
| `idempotency_key` | `varchar(256)` NULL | 客户/内部命令的幂等键；callback 不使用此字段作为入口幂等依据 |
| `request_hash` | `char(64)` NULL | 请求规范化摘要；只存哈希，不存原始请求体 |
| `result_resource_type` | `varchar(40)` NULL | 幂等重放时定位原业务结果 |
| `result_resource_id` | `uuid` NULL | 幂等重放时定位原业务资源 |
| `result_version` | `bigint` NULL | 幂等重放时返回的业务版本 |
| `external_event_id` | `varchar(128)` NULL | 支付网关稳定外部事件标识 |
| `merchant_refund_request_no` | `varchar(128)` NULL | callback 中的商户退款请求号 |
| `external_refund_id` | `varchar(128)` NULL | callback 中的外部退款流水 |
| `metadata` | `jsonb` NOT NULL DEFAULT `'{}'` | 严格白名单的非敏感元数据，例如安全原因码、尝试次数、字段版本 |
| `created_at` | `timestamptz` NOT NULL | 事件发生时间；只允许追加，不允许更新 |

### 5.2 约束、唯一索引与数据边界

- 首版按应用层追加式审计实现：应用角色不得更新或删除 `audit_events`，应用服务不提供 UPDATE/DELETE 业务入口。
- 这不等于数据库层或金融级不可篡改。高权限数据库管理员仍可能直接修改数据；生产级防篡改需要独立审计存储、权限隔离或外部审计系统，超出 S3 范围。
- 幂等命令建立部分唯一索引：`(actor_user_id, command_name, idempotency_key)`，仅在三者非空时生效。
- callback 建立部分唯一索引：`(external_event_id, merchant_refund_request_no, external_refund_id)`，仅在三者均非空时生效；这是外部事件组合去重键，不是 `Idempotency-Key`。
- 业务服务收到同一 callback 组合时读取已有审计事件并返回同一处理结果，不再次推进退款或创建 Outbox。
- `metadata` 禁止保存原始证据、支付签名、完整支付报文、手机号、地址、聊天原文、异常堆栈、token 或密码；支付原文若未来必须保留，进入专用受控支付审计边界，不进入本表普通 metadata。
- 索引：`(resource_type, resource_id, created_at DESC)`、`(owner_user_id, created_at DESC)`、`(actor_user_id, created_at DESC)`、`(request_id)`、`(created_at)`。

### 5.3 数据归属

审计读取始终受资源范围约束。客户只能看到自己资源的安全处理结果；客服只能看到自己认领/参与的申请；finance 看到审批和资金异常轨迹；operator 看到政策和脱敏聚合；admin 只看授权/配置及脱敏审计元数据；worker 只读自身事件轨迹。审计表不是绕过业务授权的全局日志表。

## 6. `outbox_events` 逐字段设计

### 6.1 字段

| 字段 | 类型/约束 | 设计用途 |
|---|---|---|
| `id` | `uuid` PK | Outbox 事件 ID |
| `refund_id` | `uuid` NOT NULL，FK `refunds(id)` `ON DELETE RESTRICT` | 首版只允许退款投递事件，不允许泛化为任意 Agent 任务 |
| `event_type` | `varchar(64)` NOT NULL | 首版固定为 `REFUND_REQUEST_AUTHORIZED` |
| `idempotency_key` | `varchar(256)` NOT NULL UNIQUE | 稳定事件键，例如由退款单 ID 派生；重试不生成新键 |
| `payload` | `jsonb` NOT NULL | 发给内部 PaymentGateway Adapter 的最小白名单事实：退款请求号、支付流水引用、金额分、CNY；不得含签名或原始证据 |
| `status` | `varchar(20)` NOT NULL DEFAULT `PENDING` | `PENDING`、`PROCESSING`、`SUCCEEDED`、`DEAD`；`SUCCEEDED` 只表示退款请求已被支付适配器成功接收/投递完成，不表示退款到账 |
| `attempt_count` | `integer` NOT NULL DEFAULT `0`，CHECK `>= 0` | 已尝试次数 |
| `available_at` | `timestamptz` NOT NULL | 下一次可投递时间 |
| `locked_at` | `timestamptz` NULL | worker 认领时间 |
| `locked_by` | `varchar(128)` NULL | worker 实例的不敏感标识，不写主机秘密 |
| `last_error_code` | `varchar(64)` NULL | 受控错误码，不保存供应商异常文本 |
| `dead_lettered_at` | `timestamptz` NULL | 进入 DLQ 的时间 |
| `processed_at` | `timestamptz` NULL | 成功处理时间 |
| `created_at` | `timestamptz` NOT NULL | 事件创建时间 |
| `updated_at` | `timestamptz` NOT NULL | 状态更新时间 |

### 6.2 写入条件与索引

Outbox 只能在同一个数据库事务中由应用服务写入，且同时满足以下之一：

1. 自动资格评估为 D02 合格，客户已在 `PENDING_CUSTOMER_CONFIRMATION` 完成二次确认；或
2. 申请已进入 `PENDING_FINANCE_APPROVAL`，finance 已成功批准。

客户提交申请、AI 建议、客服认领、补证、客服审核、财务驳回、客户取消都不得创建退款 Outbox。

约束和索引：

- `idempotency_key` 全局唯一；
- `(refund_id, event_type)` 建唯一约束，首版每笔退款最多一个授权投递事件；
- `status` CHECK 只允许四种投递状态；
- 领取查询索引：`(status, available_at, created_at)`；
- 异常查询索引：`(status, updated_at)`、`(dead_lettered_at)`；
- worker 使用事务和 `SELECT ... FOR UPDATE SKIP LOCKED` 认领，认领后先原子设置 `PROCESSING` 与锁信息。

## 7. 幂等设计

### 7.1 客户/内部命令

客户和内部角色发起的所有状态变更命令必须携带 `Idempotency-Key`。应用服务在事务内完成：

1. 校验 actor、资源范围、状态和 `expected_version`；
2. 对规范化请求体计算 SHA-256 `request_hash`；
3. 查询 `(actor_user_id, command_name, idempotency_key)`；
4. 不存在则执行命令，并在同一事务写入业务状态与审计事件；
5. 存在且 hash 相同，则返回原业务资源/状态结果，不重新创建申请、退款或 Outbox；
6. 存在但 hash 不同，则返回 `409 IDEMPOTENCY_CONFLICT`，不得换 key 继续执行资金动作。

`audit_events` 的幂等字段只保存安全摘要和结果资源定位，不保存完整响应。若未来 API 必须返回不可重建的完整原始响应，需要单独冻结并增加 `idempotency_records`，本 S3-01 不擅自新增。

### 7.2 支付 callback

支付 callback 不要求 `Idempotency-Key`。应用服务先完成签名、商户、时间窗口、订单/支付流水、金额、货币和状态校验，再以以下三字段组合去重：

```text
external_event_id + merchant_refund_request_no + external_refund_id
```

处理规则：

- 组合已存在且请求事实一致：返回幂等成功/已处理结果，不再次更新退款、不创建 Outbox；
- 组合不存在：锁定对应 `refunds` 行，按当前 Refund 状态机处理，并在同一事务写入 `audit_events`；
- 外部状态重复或乱序：不允许状态回退，记录安全原因码和 callback 审计事件；
- 标识与订单、金额、货币或当前退款不匹配：返回 `400 PAYMENT_CALLBACK_INVALID`，不得推进状态；
- 具体支付宝字段、签名算法和网关 HTTP 重试语义留到 S5 PaymentGateway 契约冻结。

## 8. 乐观锁与并发策略

### 8.1 `expected_version` 校验位置

`expected_version` 由客户/内部命令请求体携带，进入应用服务后校验，不能由前端、Agent 或 LLM 自行决定最终状态。应用服务在数据库事务中对聚合行执行条件更新：

```text
UPDATE ...
SET status = next_status, version = version + 1, updated_at = now()
WHERE id = :id AND version = :expected_version AND status = :expected_status
```

受影响行数为 1 才算成功；为 0 时重新读取资源，区分 `VERSION_CONFLICT` 与 `INVALID_STATE`，并统一返回 409。成功状态更新和审计事件必须在同一事务中提交。

### 8.2 两名 finance 并发审批

两名 finance 同时审批同一申请时：

1. 两个事务都必须先通过 actor/scope/role 校验；
2. 应用服务锁定 `after_sale_requests` 行，或使用带 `version + status` 的条件更新；
3. 第一个事务成功把 `PENDING_FINANCE_APPROVAL` 改为 `REFUND_PROCESSING`，创建唯一 Refund 和唯一 Outbox，并提交；
4. 第二个事务受影响行数为 0，返回 `409 VERSION_CONFLICT` 或 `409 INVALID_STATE`；
5. 唯一 `after_sale_request_id`、唯一 Outbox key 和状态机 guard 共同保证只有一个胜出者；
6. 第二名不能通过重试或换 Idempotency-Key 再创建退款。

D07 的职责分离在同一事务内查询审计/审核事实：若 `actor_user_id = reviewed_by_agent_id`，拒绝审批；不能只相信请求角色字段。

### 8.3 客服认领

认领使用条件更新或 `FOR UPDATE SKIP LOCKED`：仅在工单/申请处于可认领状态且 `assigned_agent_id IS NULL` 或已过 `claim_expires_at` 时写入当前客服。一个客服成功认领后，其他客服只能得到资源不可用/冲突结果，不能读取完整数据。D08 的 24 小时自动回收属于 S3-02/S3-03 的应用服务或受控任务，不在本迁移中执行。

## 9. 状态机落点与调用边界

状态字段只能由领域应用服务转换，不能由 API、Agent、LLM、worker 或 MCP 工具直接执行任意 SQL 更新。

```text
API 命令 ─┐
Agent 请求 ─┼→ Application Service
worker ────┘        ├─ actor/scope/状态/幂等/version guard
                    ├─ 事务更新聚合
                    ├─ 写 audit_events
                    └─ 必要时写 outbox_events
```

- API 只负责认证、参数解析、调用应用服务和映射响应；
- Agent 只能整理事实、请求受控评估或请求人工入口，不能传入资格、金额或状态结论；
- worker 只消费已授权 Outbox，通过应用服务推进 Refund；
- callback 只进入受控 API endpoint，由应用服务验签、去重和推进状态；
- SQL store 只能提供参数化查询和原子数据操作，不拥有业务状态转换决策。

状态转换必须严格遵循 S0-02：

- `AfterSaleRequest`：`SUBMITTED → EVIDENCE_PENDING / UNDER_REVIEW / CANCELLED`；`EVIDENCE_PENDING → EVIDENCE_PENDING / UNDER_REVIEW / CANCELLED`；`UNDER_REVIEW → PENDING_CUSTOMER_CONFIRMATION / PENDING_FINANCE_APPROVAL / CANCELLED`；`PENDING_CUSTOMER_CONFIRMATION → REFUND_PROCESSING / CANCELLED / EXPIRED`；`PENDING_FINANCE_APPROVAL → REFUND_PROCESSING / REJECTED`；`REFUND_PROCESSING → REFUNDED / REFUND_EXCEPTION`。
- `Refund`：`CREATED → PROCESSING → SUCCEEDED / FAILED / RECONCILIATION_EXCEPTION`。
- 订单支付、履约和售后状态保持分离；`AFTER_SALE_OPEN` 只能由有效售后申请推导，不能写入现有 `orders.status`。

## 10. 审计事件要求

每次成功或拒绝的高风险命令至少记录：

- 谁：`actor_type`、`actor_user_id`；worker、系统和支付网关使用机器主体；
- 何时：`created_at`；
- 操作：`action`、`command_name`；
- 基于哪个版本：`expected_version`、`new_version`、`policy_version`；
- 状态变化：`from_status`、`to_status`；无状态变化的拒绝也记录安全原因码；
- 为什么：`reason_code`，例如 `UNMATCHED_ORDER` 只能作为内部原因；
- 关联链路：`request_id`、`trace_id`，可选 `span_id`；
- 幂等和外部事件：只记录 key、hash、外部标识，不记录原始报文。

审计事件必须可重建申请/退款的状态轨迹，但不能成为敏感数据副本。禁止写入证据原文、支付签名、完整 callback、手机号、地址、聊天原文、token、密码或未脱敏异常堆栈。

## 11. Outbox 投递与失败处理

### 11.1 事务边界

客户确认或 finance 批准时，应用服务在一个 PostgreSQL 事务中完成：

1. 校验当前状态、权限、`expected_version` 和 Idempotency-Key；
2. 将售后申请推进到 `REFUND_PROCESSING`；
3. 创建唯一 `refunds` 记录，状态为 `CREATED`；
4. 创建唯一 `outbox_events` 记录，状态为 `PENDING`；
5. 写入审计事件；
6. 一次性提交。

任一步失败则全部回滚，不能出现“申请已进入处理中但没有 Outbox”或“有 Outbox 但申请未获授权”的半状态。

### 11.2 worker 状态

- `PENDING`：等待投递；
- `PROCESSING`：worker 已通过事务锁领取；
- `SUCCEEDED`：本次退款请求已被 PaymentGateway 适配器成功接收或投递完成；它不表示资金已经到账，也不直接把 Refund 置为 `SUCCEEDED`。
- `DEAD`：重试耗尽或不可重试，进入 finance 异常待办。

超时、网络错误和供应商 5xx 由系统按受控策略处理；每次重试复用同一 `merchant_refund_request_no` 和 Outbox `idempotency_key`。在外部结果未知时，S3-04 必须先验证查询/对账，再决定是否重试，不能因为本地超时就直接再次发起可能已完成的退款。不可重试错误、回调不匹配和对账差异不得盲目重试，进入 `DEAD`/`REFUND_EXCEPTION`，由 finance 最终关闭。系统记录 `attempt_count`、`available_at`、`last_error_code`，不记录原始供应商异常。

只有已验签且与支付流水、金额、货币、退款请求号匹配的 callback，或后续对账确定性确认成功，才能把 `Refund` 收敛到 `SUCCEEDED`。`outbox_events.SUCCEEDED` 与 `refunds.status = SUCCEEDED` 是两个不同事实。

### 11.3 S3-04 与 S5 的 callback 边界

S3-04 可以使用内部 Mock PaymentGateway Adapter 生成 callback，专门演练重复 callback、乱序 callback、超时后查询/对账再重试、错误金额和未知结果；这些测试不接入支付宝，也不代表已经完成真实验签。

S5 才接入支付宝真实 callback endpoint、签名校验、商户/流水/金额/货币/时间窗口校验和真实对账能力。S3 的 callback 测试验证的是领域状态机和幂等边界，不是支付宝协议兼容性。

## 12. S3-01 迁移方案

### 12.1 Upgrade 范围

未来 Alembic migration 只创建四张新表、必要的外键、CHECK、唯一约束和查询索引。不得：

- 修改 `orders`、`order_items`、`users`、`tickets` 或 `sessions`；
- 自动回填任何历史订单归属；
- 根据 `customer_id`、手机号、姓名或 UID 后缀绑定 `customer_user_id`；
- 把 `UNMATCHED` 订单转换成可退款订单；
- 修改现有订单的 `status`、金额、支付时间或物流事实；
- 在应用启动时 `CREATE TABLE`；
- 自动清理、匿名化或删除数据。

新表没有历史数据，因此本迁移不执行业务数据 backfill。所有历史订单继续保持原状。

### 12.2 Downgrade 风险

四张表存在外键依赖，降级顺序应为 `outbox_events → audit_events → refunds → after_sale_requests`。即使顺序正确，降级也会删除售后、退款、审计和投递事实，生产环境不应使用 downgrade 作为常规回滚方案。

S3-01 的 downgrade 只在空的临时测试库验证。生产发布采用备份、前滚修复或恢复演练；如果四张表已产生业务数据，必须停止 downgrade，并由负责人决定前滚迁移或恢复方案。

### 12.3 测试库验证步骤

本次设计阶段不执行以下命令；通过设计验收后，S3-01 实施时才在独立数据库执行：

```bash
# 仅示意，目标库必须是临时测试库，不得指向业务库
PG_DBNAME=ecommerce_agent_s3_test alembic upgrade head
PG_DBNAME=ecommerce_agent_s3_test alembic current
PG_DBNAME=ecommerce_agent_s3_test pytest -q tests/test_s3_01_schema.py
```

验证内容：

- Alembic revision 到达新 head；
- 四张表、主键、外键、CHECK、唯一约束和索引均存在；
- 空库升级成功；
- 现有业务库在迁移前后 `orders` 行数、`customer_user_id NULL` 数量和订单字段摘要不变；
- 不产生历史售后、退款或 Outbox 记录；
- 在空临时库执行 downgrade 后四张表消失，已有基线表仍存在；
- downgrade 失败/中断时不继续盲目执行，保留数据库和 Alembic 版本证据。

## 13. S3-01 测试清单与验收证据格式

### 13.1 Schema 测试

- 空库 `upgrade head` 成功；
- 四张表的列、类型、非空、默认值和 CHECK 与设计一致；
- 所有外键引用正确，删除策略符合设计；
- `after_sale_requests` 有效申请部分唯一索引阻止同订单第二个有效申请；
- `reapplication_of_id` 唯一索引阻止二次重新申请；
- `refunds.after_sale_request_id` 和 `merchant_refund_request_no` 阻止重复退款；
- `refunds.external_refund_id` 非空部分唯一索引阻止外部流水跨退款复用；
- `audit_events` 的命令幂等索引和 callback 三元组去重索引存在；
- `outbox_events.idempotency_key` 和 `(refund_id, event_type)` 唯一约束存在；
- 金额负数、非 CNY、非法状态和全额金额不一致时被数据库/应用层拒绝。

### 13.2 迁移安全测试

- 迁移只新增四张表，不改变既有表结构；
- 迁移前后业务库订单数量与 `UNMATCHED` 数量一致；
- 不自动生成售后、退款、审计或 Outbox 历史数据；
- 测试库 downgrade 仅验证空新表，明确记录生产 downgrade 禁止事项；
- 不执行真实支付、callback、worker 或外部集成。

### 13.3 幂等与并发设计验收预告

这些属于 S3-02/S3-03/S3-04 的实现验收，但 S3-01 设计必须能支撑：

- 同一客户命令重复 100 次只得到同一业务结果；
- 同一 key 不同请求摘要返回 `409 IDEMPOTENCY_CONFLICT`；
- 两名 finance 并发审批只有一方推进状态并创建一套 Refund/Outbox；
- 同一 callback 重放不重复推进退款；
- 乱序 callback 不造成状态回退；
- worker 重启、超时和重试不生成第二个商户退款请求号；
- 每个状态变化均可由审计事件重建。

### 13.4 统一验收证据格式

```text
目标数据库：<临时库名>
目标主机/端口：<host>:<port>
迁移前 revision：<revision>
迁移后 revision：<revision>
执行命令：<脱敏后的命令>
新增表：after_sale_requests / refunds / audit_events / outbox_events
主键/外键/唯一约束/CHECK/索引：<查询结果或归档文件路径>
迁移前后既有表行数：<orders/order_items/users/tickets/sessions/session_messages>
UNMATCHED 数量迁移前后：<数量>
新表初始行数：均为 0
测试结果：<passed/skipped/failed>
业务数据库是否写入：否
是否执行真实支付或外部 callback：否
异常与未验证项：<逐项列出>
```

## 14. 当前阻塞项与待负责人冻结事项

### P0：`OrderEligibilityFactProvider` 尚未实现

冻结规则 D06 要求金额唯一来源是支付流水，并要求 callback 按外部事件、商户退款请求号和外部退款流水收敛。当前仓库只有 `orders.paid_amount`、`payment_time` 和一个 MCP `check_payment` 包装，没有支付流水表、支付交易 ID、货币、支付状态或回调事实来源。

因此当前不能证明：

- 某订单的支付事实可被确定性读取；
- 退款金额来自可信支付流水；
- 已成功退款金额和处理中保留金额可计算；
- callback 能与唯一支付交易和金额匹配。

已冻结的分层方案为：S3 使用 `OrderEligibilityFactProvider` 和只服务独立测试夹具的 `MockOrderEligibilityFactProvider`，同时提供 `PaymentFact` 与 `FulfillmentFact`；S5 再接入支付宝、真实订单/物流事实、支付流水和对账能力。Provider 尚未实现前，S3-01 可以做 schema 设计审查和独立测试准备，但 S3-02/S3-05 不得使用 `orders.paid_amount/payment_time/status`，也不能宣称四张表已经足以支撑真实退款闭环。

### P0：当前订单不能支撑已确认归属的首版资格测试

业务库 50,000 条订单的 `customer_user_id` 全部为 NULL，且业务库当前没有用户。按 D01 和 S1 约束，不能自动回填或弱关联绑定。因此不能使用当前业务库证明“客户可以申请退款”；S3 测试必须使用独立、明确建立归属的测试数据，且不能把它回填到业务库。

### P0：当前旧订单字段不能作为支付/履约资格事实

现有 `orders.status` 混合 `待付款`、`待发货`、`运输中`、`已签收`、`已退款` 等含义。D03 要求三类状态分开保存，因此 S3-02 必须通过已冻结的 `OrderEligibilityFactProvider` 获取支付与履约事实；不得从旧 `orders.status` 推断“已支付、未发货、未退款”。

### P1：证据对象存储不在本次四表范围

本设计只在 `evidence_refs` 保存受控对象引用和元数据，不保存证据原文。对象存储、病毒扫描、下载授权、D05 文件校验和 180 天保留任务不属于 S3-01；实现前必须由 S3-03/S4 或独立数据治理任务冻结，不得把原始文件塞进 PostgreSQL JSONB。

### P1：幂等记录与审计职责复用边界

本设计使用 `audit_events` 中的命令摘要和结果资源定位支撑可重放幂等，避免在 S3-01 擅自增加第五张表。如果后续 API 要求保存不可重建的完整响应，必须单独冻结 `idempotency_records` 的字段、保留期限和清理规则；在此之前不得自动清理幂等记录或审计事件。

### P1：当前没有通知/异常队列实体

D10/D11 要求系统通知、死信、finance 最终关闭和工作台待办，但本次四表只覆盖支付 Outbox 和审计。通知/待办实体不应通过修改四表语义临时解决，应在后续任务中明确其数据模型和责任边界。

## 15. 本设计阶段结论

- 已完成当前订单/支付事实盘点和 S3-01 四表设计；
- 已明确主键、外键、唯一约束、索引、金额、时间、状态、归属、幂等、并发、审计和 Outbox 边界；
- 已明确迁移只新增表/索引/约束，不回填历史订单，不修改 `UNMATCHED` 归属；
- 已登记支付流水来源、当前订单归属、状态分离等阻塞项；
- 未执行 Alembic 迁移，未创建/删除/修改数据库对象，未修改业务代码，未接入支付或外部服务；
- 等待架构负责人确认阻塞项和四表设计后，才能进入 S3-01 的实际 migration 实施。
