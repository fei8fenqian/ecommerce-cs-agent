# S3-02 Store 设计

> 状态：设计稿，尚未实现
>
> 本文只冻结退款领域 Store、Repository 和事务边界。当前不写 SQL、不执行迁移、
> 不连接数据库、不实现 Application Service、API、支付或 Worker。
>
> 依赖契约：`S3-02_INTERNAL_CONTRACT.md`、`S3-02_APPLICATION_SERVICE_PLAN.md`、
> `S3-01_DESIGN_PACKAGE.md`。

## 1. 设计目标

Store 只负责四件事：

1. 使用参数化 SQL 读取和保存退款领域数据；
2. 在需要时加行锁，并返回明确的版本冲突/唯一约束结果；
3. 在同一个 Unit of Work 中写入售后申请、退款单、审计和 Outbox；
4. 不暴露任意表名、列名、状态字符串、SQL 或数据库连接给 API、Agent、worker。

Store 不负责：

- 判断业务政策是否满足；
- 决定状态机允许哪条业务边；
- 判断当前用户是否有业务权限；
- 调用 LLM、支付平台或 MCP；
- 把 callback 当作普通命令处理。

## 2. 事务与 Unit of Work

### 2.1 边界

Application Service 每个命令只创建一个 `RefundUnitOfWork`：

```text
UnitOfWorkFactory.begin()
  -> 一个数据库连接
  -> 一个数据库事务
  -> after_sales / refunds / audits / outbox 四个 Repository
  -> commit 或 rollback
```

Repository 协议不暴露裸 `AsyncConnection`。具体 Psycopg Repository 由
`RefundUnitOfWork` 使用同一个连接构造，应用服务只能看到 Repository 的受控方法。
这样既保证四个 Repository 共享同一事务，也避免服务层自己拼 SQL 或借多个连接。

```text
class UnitOfWorkFactory(Protocol):
    begin() -> AsyncContextManager[RefundUnitOfWork]

class RefundUnitOfWork(Protocol):
    after_sales: AfterSaleRepository
    refunds: RefundRepository
    audits: AuditRepository
    outbox: OutboxRepository

    commit() -> Awaitable[None]
    rollback() -> Awaitable[None]
```

### 2.2 成功命令顺序

```text
开始事务
  -> 幂等记录查询
  -> 按固定顺序锁定资源
  -> 读取并校验当前版本
  -> Application Service 做角色、归属、事实和状态校验
  -> Repository 更新领域资源，version + 1
  -> 必要时创建 Refund
  -> 必要时创建 Outbox
  -> 追加 AuditEvent
提交事务
```

任何一步出现未预期异常，都回滚本次业务状态、Refund、Outbox 和审计写入。

### 2.3 业务拒绝与异常的区别

业务拒绝不是数据库故障。例如状态不对、版本冲突、权限不允许：

```text
Application Service 的业务事务
  -> 锁定资源并发现条件不满足
  -> 不修改业务资源
  -> rollback 原业务事务

Application Service 单独开启审计事务
  -> 写一条拒绝/冲突审计
  -> commit 审计事务
  -> 再向 API 层抛出 DomainError
```

不能在原 Unit of Work 内直接抛出异常后期待上下文管理器保存审计，因为常见事务
上下文会自动 rollback，审计也会丢失。`RefundUnitOfWork` 不提供模糊的“异常时自动
提交”行为；由 Application Service 明确调用独立的 `AuditUnitOfWork`：

```text
class AuditUnitOfWorkFactory(Protocol):
    begin() -> AsyncContextManager[AuditUnitOfWork]

class AuditUnitOfWork(Protocol):
    audits: AuditRepository

    commit() -> Awaitable[None]
    rollback() -> Awaitable[None]
```

Application Service 只有在原业务事务 rollback 完成后，才使用这个独立 UoW 写拒绝
或冲突审计并 commit。

数据库连接失败、序列化失败或未知异常则回滚原业务事务；如果连审计事务也失败，
保留原领域错误/依赖错误并记录安全日志，不能把未提交的审计当作成功事实。

## 3. 锁、版本和固定锁顺序

### 3.1 `SELECT FOR UPDATE`

需要改变现有资源的命令必须先通过 Repository 的 `get_for_update()` 锁定目标行。
锁的作用是：同一时间只有一个事务可以基于该资源执行状态变更。

仅仅先普通读取再更新不够，因为两个事务可能同时读到相同状态。

### 3.2 `expected_version`

锁定后由 Application Service 比较：

```text
command.meta.expected_version == record.version
```

Repository 更新时仍使用“资源 ID + 旧版本”作为条件，并返回更新后的记录。这样即使
未来某个调用方漏掉了前置检查，数据库层仍不会静默覆盖新版本。

结果约定：

- 更新成功：返回新版本；
- 更新行数为 0：映射为 `VERSION_CONFLICT`；
- 不创建 Refund、Outbox 或成功审计；冲突审计单独提交。

### 3.3 多表锁顺序

涉及售后申请和退款单时，所有命令统一按以下顺序加锁：

```text
after_sale_requests
  -> refunds
```

涉及 Refund 的 callback 也必须先定位并锁定关联售后申请，再锁定 Refund，避免不同
命令采用相反顺序造成死锁。Outbox 只在创建 Refund 后写入，不反向锁定售后申请。

## 4. AfterSaleRepository

### 4.1 受控记录类型

`AfterSaleRecord` 是 Store 内部记录，不直接作为 API 响应。至少包含：

```text
id: UUID
order_id: LegacyOrderId
customer_user_id: int
assigned_agent_id: int | None
status: AfterSaleStatus
currency: Currency
payment_transaction_ref: PaymentTransactionRef
payment_amount_cents: NonNegativeCents
refund_amount_cents: NonNegativeCents
reason_code: ReasonCode
evidence_refs: EvidenceRefs
evidence_round: int
evidence_due_at: datetime | None
customer_note: SafeText | None
qualification_path: QualificationPath | None
qualification_result: QualificationResult | None
policy_version: PolicyVersion | None
reviewed_by_agent_id: int | None
finance_decided_by: int | None
version: NonNegativeInt
claimed_at: datetime | None
claim_expires_at: datetime | None
```

### 4.2 方法签名

```text
class AfterSaleRepository(Protocol):
    get_for_update(
        after_sale_request_id: UUID,
    ) -> Awaitable[AfterSaleRecord | None]

    find_active_by_order(
        order_id: LegacyOrderId,
    ) -> Awaitable[AfterSaleRecord | None]

    insert_submitted(
        record: NewAfterSaleRecord,
    ) -> Awaitable[AfterSaleRecord]

    update_status(
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        transition: AfterSaleTransitionUpdate,
    ) -> Awaitable[AfterSaleRecord | None]

    claim_if_available(
        after_sale_request_id: UUID,
        agent_user_id: int,
        expected_version: NonNegativeInt,
        claimed_at: datetime,
        claim_expires_at: datetime,
    ) -> Awaitable[AfterSaleRecord | None]

    release_claim(
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        released_by_user_id: int | None,
        now: datetime,
    ) -> Awaitable[AfterSaleRecord | None]

    update_evidence(
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: EvidenceUpdate,
    ) -> Awaitable[AfterSaleRecord | None]

    save_review(
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: ReviewUpdate,
    ) -> Awaitable[AfterSaleRecord | None]

    save_finance_decision(
        after_sale_request_id: UUID,
        expected_version: NonNegativeInt,
        update: FinanceDecisionUpdate,
    ) -> Awaitable[AfterSaleRecord | None]
```

说明：

- `update_status()` 不接受任意列名或任意状态字符串；`transition` 是受控 DTO；
- `claim_if_available()` 必须在同一个原子更新中检查“未被认领”和版本；
- `find_active_by_order()` 只用于重复有效申请检查，不替代最终唯一索引；
- `insert_submitted()` 的金额、币种和支付流水必须来自已验证事实，不来自客户命令；
- 所有成功更新都递增 `version`；
- Repository 不决定状态边是否合法，状态机和 Application Service 先完成校验。

## 5. RefundRepository

### 5.1 方法签名

```text
class RefundRepository(Protocol):
    get_for_update(
        refund_id: UUID,
    ) -> Awaitable[RefundRecord | None]

    get_for_update_by_after_sale(
        after_sale_request_id: UUID,
    ) -> Awaitable[RefundRecord | None]

    insert_created(
        record: NewRefundRecord,
    ) -> Awaitable[RefundRecord]

    update_status(
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: RefundStatusUpdate,
    ) -> Awaitable[RefundRecord | None]

    save_verified_callback(
        refund_id: UUID,
        expected_version: NonNegativeInt,
        update: VerifiedCallbackUpdate,
    ) -> Awaitable[RefundRecord | None]
```

### 5.2 约束

- `get_for_update_by_after_sale()` 用于防止同一售后申请创建第二个 Refund；
- Refund 金额必须来自售后申请/PaymentFact，不能来自 callback 或客户端任意输入；
- `RefundStatusUpdate` 必须同时携带并持久化 `processing_at`、`succeeded_at`、
  `failed_at`、`reconciliation_due_at`，避免状态字段和时间字段不一致；
- `SUCCEEDED` 只能由已验证 callback 或对账结果触发；
- `FAILED` 或 `RECONCILIATION_EXCEPTION` 的更新必须由 Application Service 同时收敛
  关联售后申请为 `REFUND_EXCEPTION`；
- callback 的 Repository 方法只能接收已经通过 `CallbackContext` 校验的
  `VerifiedCallbackUpdate`，不能接收原始签名、原始 payload 或 URL；
- 机器命令不使用 `CommandMeta` 的幂等键，而由 Refund/Outbox 唯一约束和 callback
  三元组去重保证重放安全。

## 6. AuditRepository

### 6.1 方法签名

```text
class AuditRepository(Protocol):
    find_command_idempotency(
        actor_user_id: int,
        command_name: AfterSaleCommand,
        idempotency_key: IdempotencyKey,
    ) -> Awaitable[IdempotencyRecord | None]

    find_callback_deduplication(
        external_event_id: ExternalEventId,
        merchant_refund_request_no: MerchantRefundRequestNo,
        external_refund_id: ExternalRefundId,
    ) -> Awaitable[AuditRecord | None]

    append(
        event: NewAuditEvent,
    ) -> Awaitable[AuditRecord]
```

### 6.2 人类命令幂等

首次命令开始时按以下作用域查询：

```text
(actor_user_id, command_name, idempotency_key)
```

然后比较规范化请求摘要 `request_hash`：

```text
相同作用域 + 相同 request_hash
  -> 读取 result_resource_type、result_resource_id、result_version
  -> 重新查询资源构造 CommandResult
  -> idempotent_replay = true
  -> 不重复修改状态、不重复创建 Refund/Outbox

相同作用域 + 不同 request_hash
  -> IDEMPOTENCY_CONFLICT
  -> 不执行命令
```

幂等重放审计使用 `IDEMPOTENCY_REPLAY` 动作，`idempotency_key` 必须为 NULL，
metadata 只保存首次审计事件的内部 UUID。

### 6.3 Callback 去重

callback 不查询或保存普通命令幂等键，只使用：

```text
external_event_id
merchant_refund_request_no
external_refund_id
```

重复 callback 返回“已处理”语义，不再次修改 Refund、售后状态或 Outbox。
这个过程的入口参数必须是 `CallbackContext` 加已规范化外部标识，不能是
`CommandMeta`。

### 6.4 审计数据白名单

`NewAuditEvent` 只能接收受控 `AuditMetadata`。允许保存：

- actor/source/request/trace 关联信息；
- 资源类型和内部 UUID；
- 命令、前后状态、版本和受控原因码；
- 幂等结果定位；
- 受控 provider reason code。

禁止保存证据原文、支付签名、完整 PII、LLM prompt/response、供应商异常原文。

## 7. OutboxRepository

### 7.1 方法签名

```text
class OutboxRepository(Protocol):
    find_by_idempotency_key(
        idempotency_key: IdempotencyKey,
    ) -> Awaitable[OutboxRecord | None]

    find_by_refund_and_type(
        refund_id: UUID,
        event_type: OutboxEventType,
    ) -> Awaitable[OutboxRecord | None]

    insert_refund_authorized(
        event: NewRefundOutboxEvent,
    ) -> Awaitable[OutboxRecord]

    update_delivery_state(
        event_id: UUID,
        expected_status: OutboxStatus,
        update: OutboxDeliveryUpdate,
    ) -> Awaitable[OutboxRecord | None]
```

### 7.2 约束

- `insert_refund_authorized()` 只能在客户确认或财务批准成功的同一事务中调用；
- payload 必须由 `RefundOutboxPayload` 构造，至少包含 `outbox_event_id`、`refund_id`、
  `after_sale_request_id`、商户退款请求号、支付流水引用、金额、币种、投递尝试次数
  和创建时间；
- `outbox_events.SUCCEEDED` 只表示适配器接收/投递成功，不表示资金到账；
- `(refund_id, event_type)` 和 outbox 幂等键冲突必须映射为可识别的幂等结果，而非
  产生第二条事件；
- Store 不执行支付调用，不把供应商异常原文写入 `last_error_code` 或 payload。

## 8. DTO 到字段的映射

| 命令 | Store 写入的主要字段 | 禁止从命令读取 |
|---|---|---|
| `SubmitAfterSaleCommand` | `order_id`、`customer_user_id`、原因、证据引用、事实快照、金额、币种、`SUBMITTED`、`version` | 退款金额、支付流水、资格结果、目标状态 |
| `CancelAfterSaleCommand` | `status=CANCELLED`、`decision_reason_code`、`version`、关闭时间 | 新状态字符串、退款金额 |
| `ClaimAfterSaleCommand` | `assigned_agent_id`、`claimed_at`、`claim_expires_at`、`version` | 客户 ID、任意负责人 ID |
| `SubmitEvidenceCommand` | `evidence_refs`、`evidence_round`、`evidence_due_at`、状态、`version` | 文件原文、支付信息、目标状态 |
| `SubmitReviewCommand` | `reviewed_by_agent_id`、`reviewed_at`、审核结果、原因、策略版本、状态、`version` | 退款金额、支付状态、直接退款命令 |
| `FinanceApproveCommand` | `finance_decided_by`、`finance_decided_at`、`REFUND_PROCESSING`、`version`、Refund、Outbox | 客户传入金额、支付签名、资格结果 |
| `FinanceRejectCommand` | `finance_decided_by`、`finance_decided_at`、`REJECTED`、原因、`version` | Refund、Outbox、退款金额 |

注意：`customer_user_id` 来自 `CommandMeta.actor` 和订单归属事实，不来自命令
请求体；`assigned_agent_id` 来自客服 Actor，不来自请求体字段。

### 8.1 ReviewOutcome 的持久化落点

`after_sale_requests` 没有 `review_outcome` 字段，因此不新增字段，也不修改 S3-01
schema。客服审核结果直接映射为状态转换：

| `ReviewOutcome` | 售后状态 | 审计保存位置 |
|---|---|---|
| `REQUEST_EVIDENCE` | `EVIDENCE_PENDING` | `action` 或白名单 metadata |
| `RECOMMEND_CUSTOMER_CONFIRMATION` | `PENDING_CUSTOMER_CONFIRMATION` | `action` 或白名单 metadata |
| `RECOMMEND_FINANCE_REVIEW` | `PENDING_FINANCE_APPROVAL` | `action` 或白名单 metadata |
| `RECOMMEND_REJECT` | `REJECTED` | `action` 或白名单 metadata |

审计仍保存 `reviewed_by_agent_id`、前后状态、原因码、策略版本和安全审核摘要；
原始审核文本不进入审计 metadata。

## 9. Callback 进入门禁

callback 必须走独立路径：

```text
外部回调
  -> Callback Adapter 验签、校验商户、规范化外部标识
  -> 创建 CallbackContext
  -> 创建 RecordRefundCallbackCommand(context=CallbackContext, ...)
  -> Application Service
  -> RefundRepository.save_verified_callback()
```

禁止以下做法：

- 用 `CommandMeta` 表示 callback；
- 用客户端 `Idempotency-Key` 去重 callback；
- 让普通客户/客服/财务构造 `PAYMENT_GATEWAY` Actor；
- 把原始签名、完整 callback body 或供应商 URL 传进 Repository；
- 未验证签名就修改 Refund 或售后状态。

## 10. Store 测试计划

### 10.1 Repository 单元测试

- 所有写方法只接受受控 DTO，不接受任意 dict、状态字符串或列名；
- 状态和版本更新返回新记录，更新失败返回明确的空结果/冲突结果；
- 生成的 SQL 值使用参数绑定；
- 审计 metadata 和 Outbox payload 只包含白名单字段；
- callback 方法不能接收 `CommandMeta`；
- callback 未验证、来源错误或外部标识缺失时不允许写入。

### 10.2 独立测试库集成测试

只能使用已授权的 `ecommerce_agent_s3_test`，并先确认 Alembic revision 为
`a7d1e8f4c902`。不得连接业务库 `postgres`，不得执行迁移或写入真实订单。

覆盖：

- `SELECT FOR UPDATE` 下两个事务只有一个可以成功改变同一版本；
- 两名客服认领同一申请只有一个成功；
- 两名 finance 并发审批只有一个创建 Refund/Outbox；
- 重复命令不重复改变状态、不重复创建 Refund/Outbox；
- 相同幂等键不同摘要返回 `IDEMPOTENCY_CONFLICT`；
- callback 三元组重复不会重复收敛；
- 状态成功但 Outbox 写入失败时，售后状态、Refund、审计全部回滚；
- 业务拒绝只提交拒绝审计，不留下业务半成品；
- 订单数量、UNMATCHED 数量和 S3-01 既有数据不被改变。

### 10.3 测试边界

本设计阶段不运行数据库测试。Store 实现完成后，先运行针对性 Repository 测试，
再由负责人授权在独立测试库运行并发和事务集成测试。

## 11. 进入 Store 实现前的门禁

- [ ] 四个 Repository 方法签名经过负责人审查；
- [ ] Unit of Work 确认四个 Repository 共享一个连接和事务；
- [ ] callback 已确认只能通过 `CallbackContext`；
- [ ] 幂等查询、首次保存和重放审计规则已确认；
- [ ] 多表固定锁顺序已确认；
- [ ] DTO 到数据库字段映射已确认；
- [ ] 测试数据库和写入授权边界已确认；
- [ ] 本文件通过后才能开始 Store SQL 实现。
