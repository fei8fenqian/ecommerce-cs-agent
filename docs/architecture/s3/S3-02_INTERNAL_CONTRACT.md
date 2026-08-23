# S3-02 内部契约冻结

> 状态：待负责人/架构验收
>
> 本文件是 `S3-02_APPLICATION_SERVICE_PLAN.md` 的 Step 1 产物。契约通过前，
> 不允许开始 Store 写入实现或完整 Application Service 实现。
>
> 本文件只冻结内部类型和接口，不修改业务代码、数据库、迁移、支付集成或外部
> callback。

当前纯契约实现对应：

- `src/service/after_sale_types.py`：内部枚举、不可变值对象、Actor/RequestContext/
  CommandMeta、命令 DTO、Provider 协议、CommandResult 和领域异常；
- `src/service/after_sale_state_machine.py`：只接受内部枚举的状态边与命令角色守卫；
- `tests/test_s3_02_state_machine.py`：不连接数据库的契约与状态机测试。

这些实现仍不包含 Store、事务、API 或任何外部副作用。

## 1. 契约原则

1. 应用服务是退款领域状态变化的唯一业务写入口。
2. API、Agent、worker、callback 只能构造受控命令对象并调用应用服务。
3. 应用服务方法不得接收 FastAPI `Request`、任意 `dict`、客户端 `role`、任意
   状态字符串或裸 psycopg/SQL connection。
4. 状态、角色、来源、错误码、原因码和证据引用均使用受控枚举或不可变类型。
5. 客户端不能提交 `actor_user_id`、`actor_type`、`target_status`、退款金额、
   资格结果或支付事实；这些值由认证上下文、事实提供者和应用服务产生。
6. 应用服务返回领域结果，不返回 FastAPI `Response`；HTTP 错误映射属于 S3-03。
7. 所有时间由注入的 `Clock` 提供，生产实现返回 UTC aware datetime，测试使用固定
   时钟。

## 2. 基础类型与枚举

以下名称是契约名称，实际 Python 实现使用 `Enum`、`dataclass(frozen=True)`、
`Protocol` 或等价的不可变类型；不能退化为任意字符串或字典。

### 2.1 `ActorType`

与 S3-01 `audit_events.actor_type` CHECK 保持一致：

```text
CUSTOMER
AGENT
FINANCE
OPERATOR
ADMIN
WORKER
PAYMENT_GATEWAY
SYSTEM
```

权限边界：

- `CUSTOMER`：只能操作自己的已确认归属申请；
- `AGENT`：只能认领、补证、审核自己范围内的申请，不能审批或执行退款；
- `FINANCE`：只能审批/驳回财务队列，不能提交同一申请的客服审核意见；
- `OPERATOR`：维护政策相关流程，不执行资金审批；
- `ADMIN`：系统配置、授权和审计元数据，不处理日常退款业务；
- `WORKER`：受控执行退款投递/状态收敛，不代表人工角色；
- `PAYMENT_GATEWAY`：只用于已验证外部事件的审计身份，不接受客户端伪造；
- `SYSTEM`：受控系统动作，例如确认过期和认领超时回收。

### 2.2 `Source`

调用来源是审计上下文，不是权限替代物：

```text
API
AGENT
WORKER
CALLBACK
SYSTEM
```

`Source` 必须与受控运行身份组合校验。例如，source=`CALLBACK` 不能由普通
Agent 直接构造；source=`AGENT` 不能调用 finance 命令。

### 2.3 状态枚举

`AfterSaleStatus`：

```text
SUBMITTED
EVIDENCE_PENDING
UNDER_REVIEW
PENDING_CUSTOMER_CONFIRMATION
PENDING_FINANCE_APPROVAL
REFUND_PROCESSING
REFUNDED
REJECTED
CANCELLED
EXPIRED
REFUND_EXCEPTION
```

`RefundStatus`：

```text
CREATED
PROCESSING
SUCCEEDED
FAILED
RECONCILIATION_EXCEPTION
```

服务层禁止接受 `str` 形式的任意状态。状态转换由内部白名单决定，调用方只能
调用语义命令。

### 2.4 其他受控枚举

`Currency` 首版只有：

```text
CNY
```

`QualificationPath`：

```text
AUTO
FINANCE
```

`QualificationResult`：

```text
ELIGIBLE
INELIGIBLE
INDETERMINATE
```

`ReasonCode`、`EvidenceReasonCode`、`DecisionReasonCode`、`FailureReasonCode`
均是版本化的受控代码类型。代码清单由 S0 决策和后续策略文档维护；本文件不擅自
新增业务数值或政策规则。

## 3. Actor 与请求上下文

### 3.1 `Actor`

```text
Actor {
  actor_type: ActorType
  actor_user_id: int | None
  service_name: str | None
}
```

约束：

- `CUSTOMER`、`AGENT`、`FINANCE`、`OPERATOR`、`ADMIN` 必须有
  `actor_user_id`；
- `WORKER`、`PAYMENT_GATEWAY`、`SYSTEM` 使用受控 `service_name`，不从请求体
  接收伪造的人类用户 ID；
- 服务层创建 Actor 时必须校验角色来源；不能信任客户端传来的 `role`；
- Actor 不包含密码、token、cookie、API key 或支付签名。

### 3.2 `RequestContext`

```text
RequestContext {
  request_id: str
  trace_id: str | None
  span_id: str | None
  source: Source
}
```

约束：

- `request_id` 必须由请求 ID 中间件生成或校验后的值；
- `trace_id`、`span_id` 只能用于关联，不承担授权；
- 这些 ID 允许写入审计上下文，但不携带用户输入或资源敏感值；
- callback 使用自己的受控 request/trace context，不要求外部支付方提供
  `Idempotency-Key`。

### 3.3 `CommandMeta`

所有需要幂等的命令都包含以下元数据：

```text
CommandMeta {
  actor: Actor
  request: RequestContext
  idempotency_key: IdempotencyKey
  expected_version: NonNegativeInt
}
```

`IdempotencyKey` 是非空、长度受限、不可变的 opaque value。应用服务内部会按
`actor + command_name + resource + key` 计算作用域；不能把裸 key 直接当全局唯一键。

callback 不使用 `CommandMeta`，而使用第 9 节的 `CallbackContext`，因为 callback
是外部事件而不是客户/内部命令。

## 4. 统一成功返回类型

### 4.1 `CommandResult`

所有状态变更命令统一返回：

```text
CommandResult {
  resource_type: ResourceType
  resource_id: UUID
  status: AfterSaleStatus | RefundStatus
  version: NonNegativeInt
  idempotent_replay: bool
  outbox_event_id: UUID | None
}
```

约束：

- `resource_id` 是售后申请或退款单内部 UUID，不返回可枚举数据库顺序 ID；
- `status` 只能是与 `resource_type` 匹配的枚举；
- `version` 是事务提交后的版本；
- 首次成功为 `idempotent_replay=false`；同作用域、同请求摘要的重放为 `true`；
- 幂等重放返回首次安全结果，不重新创建 Refund 或 Outbox；
- `outbox_event_id` 只有确实在本次事务创建或重放出已存在结果时才返回；客服审核、
  取消和补证等命令返回 `None`。

### 4.2 查询不使用命令返回类型

S3-02 只冻结命令结果。查询 DTO 另由 S3-03 API 契约定义，不能把数据库 row 或
任意 `dict` 直接暴露给 API/Agent。

## 5. 命令参数对象

以下均为不可变 command DTO。每个命令的服务方法只接收对应 DTO，不接收任意字典。

### 5.1 客户提交：`SubmitAfterSaleCommand`

```text
SubmitAfterSaleCommand {
  meta: CommandMeta
  order_id: LegacyOrderId
  reason_code: ReasonCode
  customer_note: SafeText | None
  evidence_refs: EvidenceRefs
}
```

要求：

- 必须 `Idempotency-Key`；没有已有 `expected_version` 的新申请使用
  `expected_version=0` 作为命令元数据版本，服务不得把它用于覆盖已有行；
- 客户身份来自 `meta.actor`，不能由参数传入；
- 不接收退款金额、币种、支付流水、资格结果、履约状态或目标状态；
- 服务从 `OrderEligibilityFactProvider` 读取只读事实；
- UNMATCHED、事实不完整、未支付、已发货、超出 7 天或存在有效重复申请均拒绝；
- 只有已确认归属且满足首版边界的合成测试订单才可创建申请。

### 5.2 客户取消：`CancelAfterSaleCommand`

```text
CancelAfterSaleCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  reason_code: DecisionReasonCode
}
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- 只允许客户取消 `SUBMITTED`、`EVIDENCE_PENDING`、`UNDER_REVIEW`、
  `PENDING_CUSTOMER_CONFIRMATION`；
- 取消不创建 Outbox；
- `CANCELLED` 是否可再次申请由原 7 日期限和 `ReapplicationOf` 规则控制，不能
  通过取消命令直接复活原申请。

### 5.3 客服认领：`ClaimAfterSaleCommand`

```text
ClaimAfterSaleCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
}
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- actor 必须为 `AGENT`；服务使用 `meta.actor.actor_user_id` 作为认领人；
- 认领必须是数据库原子竞争；已被其他客服有效认领时不得返回完整资源内容；
- 认领成功写入 `assigned_agent_id`、`claimed_at`、`claim_expires_at` 并递增版本；
- 认领不改变售后状态，不创建 Refund/Outbox。

### 5.4 客服释放/系统回收：`ReleaseOrRecoverClaimCommand`

```text
ReleaseOrRecoverClaimCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  reason_code: DecisionReasonCode
  mode: ClaimReleaseMode
}
```

`ClaimReleaseMode` 只允许：

```text
VOLUNTARY_RELEASE
TIMEOUT_RECOVERY
SUPERVISOR_RECOVERY
```

首版规则：

- `VOLUNTARY_RELEASE`：当前认领客服可执行；
- `TIMEOUT_RECOVERY`：`SYSTEM`/受控 worker 在超过 24 小时且未进入财务审批或退款
  处理时执行；
- `SUPERVISOR_RECOVERY`：D08 首版线下流程预留，不能开放为普通 Agent 命令；
- 释放/回收只清理认领字段并追加审计，不擅自改变业务状态。

### 5.5 客户补证：`SubmitEvidenceCommand`

```text
SubmitEvidenceCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  evidence_refs: EvidenceRefs
  customer_note: SafeText | None
}
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- actor 必须为申请所有者 `CUSTOMER`；
- 证据引用不是文件内容，只允许受控对象引用元数据；
- JPEG/PNG/WebP/PDF，单文件最大 10 MB，每次最多 5 个文件由应用层校验；
- `evidence_round` 最多增加到 2，每轮截止时间由 `Clock` 加 72 小时；
- 原始证据不得写入数据库日志、审计 metadata 或 AI prompt；
- 补证后只能按事实和策略进入 `UNDER_REVIEW` 或继续
  `EVIDENCE_PENDING`，不能直接进入退款处理。

### 5.6 客服审核：`SubmitReviewCommand`

```text
SubmitReviewCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  review_outcome: ReviewOutcome
  reason_code: DecisionReasonCode
  review_note: SafeText | None
  policy_version: PolicyVersion | None
}
```

`ReviewOutcome`：

```text
REQUEST_EVIDENCE
RECOMMEND_CUSTOMER_CONFIRMATION
RECOMMEND_FINANCE_REVIEW
RECOMMEND_REJECT
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- actor 必须为 `AGENT`，且必须是当前有效认领客服；
- 客服可以形成建议，不能批准退款、决定金额或直接调用支付；
- `RECOMMEND_REJECT` 只有在确定性规则明确不符合时才能转 `REJECTED`；
- 证据不足应走 `REQUEST_EVIDENCE`，不应伪装成拒绝；
- `RECOMMEND_CUSTOMER_CONFIRMATION` 必须有受控资格结果和 policy version，不能
  由 Agent 文本直接声明；
- `review_note` 是受控安全文本，不能携带原始证据或支付签名；
- 保存 `reviewed_by_agent_id` 和 `reviewed_at`，为 D07 留下职责分离依据。

### 5.7 财务批准：`FinanceApproveCommand`

```text
FinanceApproveCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  decision_reason_code: DecisionReasonCode
  decision_note: SafeText | None
}
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- actor 必须为 `FINANCE`；
- 只允许 `PENDING_FINANCE_APPROVAL`；
- `actor_user_id != reviewed_by_agent_id`；
- 金额、币种、支付流水从申请/事实快照读取，命令不能携带金额；
- 成功时应用服务在同一事务内推进退款领域状态、创建/复用 Refund、创建 Outbox、
  追加审计；
- Outbox 成功不代表退款到账。

### 5.8 财务驳回：`FinanceRejectCommand`

```text
FinanceRejectCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  decision_reason_code: DecisionReasonCode
  decision_note: SafeText | None
}
```

要求：

- 必须 `Idempotency-Key` 和 `expected_version`；
- actor 必须为 `FINANCE`，只允许 `PENDING_FINANCE_APPROVAL`；
- 同样执行 D07 职责分离；
- 只进入 `REJECTED`，不创建 Refund/Outbox；
- 驳回原因必须为受控原因码，不能将供应商异常或内部堆栈返回给客户端。

## 6. S3-04/S3-05 预留命令契约

以下只冻结接口方向，不在 S3-02 实现。

### 6.1 客户确认：`ConfirmAutoRefundCommand`

```text
ConfirmAutoRefundCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  confirmation: CustomerConfirmation
}
```

`CustomerConfirmation` 是服务端可验证的确认标记，不接受金额或资格结果。服务
必须验证申请仍在 `PENDING_CUSTOMER_CONFIRMATION`、未过期、版本匹配，然后由
S3-05/应用服务事务编排进入退款处理并创建 Outbox。

### 6.2 资格评估：`EvaluateEligibilityCommand`

```text
EvaluateEligibilityCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  facts: EligibilityFactsRef
}
```

该命令只能由受控系统/应用服务内部调用；Agent 不能提交 `facts` 作为结果。真正
的确定性 D01/D02 规则和 `EligibilityProvider` 实现属于 S3-05。

### 6.3 退款处理：`StartRefundProcessingCommand`

```text
StartRefundProcessingCommand {
  meta: CommandMeta
  after_sale_request_id: UUID
  trigger: RefundTrigger
}
```

`RefundTrigger` 只允许：

```text
CUSTOMER_CONFIRMATION
FINANCE_APPROVAL
```

只能由应用服务内部调用，不能由 HTTP 客户端、Agent 或普通客服直接调用。它负责
冻结金额事实并原子创建/复用 Refund 与 Outbox；支付适配器调用属于 S3-04。

### 6.4 Mock callback：`RecordRefundCallbackCommand`

```text
RecordRefundCallbackCommand {
  context: CallbackContext
  external_event_id: ExternalEventId
  merchant_refund_request_no: MerchantRefundRequestNo
  external_refund_id: ExternalRefundId
  external_status: ExternalRefundStatus
  amount_cents: PositiveCents
  currency: Currency
  signature_verified: bool
}
```

该命令属于 S3-04。只有 `signature_verified=true` 的受控 Mock/未来适配器事件才
可进入领域服务；原始签名和支付报文不进入 DTO 的持久化 metadata。S3-04 必须验证
重复、乱序和超时不会重复生成退款或倒退状态。

### 6.5 退款过期/认领回收：`SystemMaintenanceCommand`

```text
SystemMaintenanceCommand {
  meta: CommandMeta
  resource_id: UUID
  maintenance_action: MaintenanceAction
}
```

`MaintenanceAction` 只允许：

```text
EXPIRE_CONFIRMATION
RECOVER_CLAIM
```

仅 `SYSTEM`/受控 worker 使用。它不是开放 API，也不能让 Agent 传入任意维护动作。

## 7. Provider 与时间接口

### 7.1 `OrderEligibilityFactProvider`

只读事实端口：

```text
interface OrderEligibilityFactProvider:
  get_facts(
    order_id: LegacyOrderId,
    customer_user_id: UserId,
  ) -> OrderEligibilityFacts
```

`OrderEligibilityFacts`：

```text
OrderEligibilityFacts {
  ownership: OwnershipFact
  payment: PaymentFact
  fulfillment: FulfillmentFact
}
```

`OwnershipFact`：

```text
OwnershipFact {
  ownership_status: CONFIRMED | UNMATCHED | NOT_AVAILABLE
  customer_user_id: UserId | None
}
```

`PaymentFact`：

```text
PaymentFact {
  payment_channel: PaymentChannel
  payment_transaction_ref: PaymentTransactionRef
  payment_succeeded: bool
  amount_cents: NonNegativeCents
  currency: Currency
  paid_at: UtcDateTime | None
}
```

`FulfillmentFact`：

```text
FulfillmentFact {
  fulfillment_status: UNSHIPPED | SHIPPED | DELIVERED | CANCELLED | UNKNOWN
  shipped_at: UtcDateTime | None
  delivered_at: UtcDateTime | None
}
```

约束：

- provider 只读，不接受状态/金额写入；
- S3 Mock 实现只读取隔离合成夹具；
- 不从旧 `orders.status` 推断 `fulfillment_status`；
- provider 故障或事实不完整时返回受控依赖错误/不可用结果，不能回退旧字段；
- `NOT_AVAILABLE`/`UNMATCHED` 不向客户暴露枚举细节，API 层统一资源不可用。

### 7.2 `EligibilityProvider`

确定性资格端口，供 S3-05 实现：

```text
interface EligibilityProvider:
  evaluate(
    request: AfterSaleEligibilityInput,
    facts: OrderEligibilityFacts,
    now: UtcDateTime,
  ) -> EligibilityDecision
```

`EligibilityDecision`：

```text
EligibilityDecision {
  result: QualificationResult
  path: QualificationPath
  policy_version: PolicyVersion
  reason_code: DecisionReasonCode
  safe_facts_snapshot: SafeFactsSnapshot
}
```

约束：

- 只能由确定性规则产生，不调用 LLM 做最终资格决定；
- 不接收或返回金额以外的可自由改写字段；金额仍必须来自 `PaymentFact`；
- `safe_facts_snapshot` 只保留白名单事实，不保存证据原文或支付签名；
- Agent 的建议不能伪装成 `EligibilityDecision`。

### 7.3 `Clock`

```text
interface Clock:
  now() -> UtcDateTime
```

所有 7 天申请期限、72 小时补证期限、24 小时认领回收、确认过期和审计时间都经
`Clock` 计算。禁止在服务内部直接调用系统 wall clock，确保边界测试可重复。

## 8. 事务上下文接口

应用服务不接收裸 SQL connection；由构造时注入一个受控的事务工厂：

```text
interface UnitOfWorkFactory:
  begin() -> AsyncContextManager[RefundUnitOfWork]
```

`RefundUnitOfWork` 是内部协议，不向 API/Agent/worker 暴露：

```text
interface RefundUnitOfWork:
  after_sales: AfterSaleRepository
  refunds: RefundRepository
  audits: AuditRepository
  outbox: OutboxRepository

  commit() -> Awaitable[None]
  rollback() -> Awaitable[None]
```

要求：

- 四个 repository 必须共享同一个事务；
- service 只能通过 repository 的命名方法完成读写；
- repository 方法接收受控参数对象，不接受任意 SQL、任意列名、任意状态字符串；
- `begin()` 的具体 psycopg 实现留在 infrastructure/store 层，不能流入服务方法签名；
- commit 前必须完成状态、Refund/Outbox（若适用）和审计写入；
- 任何异常回滚全部写入。

## 9. Callback 上下文与外部标识

callback 不要求 `Idempotency-Key`，使用独立上下文：

```text
CallbackContext {
  actor: Actor  # 只能是 PAYMENT_GATEWAY 或受控 SYSTEM
  request: RequestContext
  signature_verified: bool
  merchant_id_verified: bool
}
```

去重使用以下稳定组合：

```text
external_event_id
merchant_refund_request_no
external_refund_id
```

三者必须由受控 callback adapter 规范化；任一缺失、签名/商户校验失败或金额/币种
不一致，都不能改变 Refund 状态。原始 callback payload、签名和 URL 不进入审计
metadata 或普通日志。

## 10. 审计 metadata 类型

审计 metadata 只能使用白名单类型：

```text
AuditMetadata {
  safe_reason: str | None
  evidence_count: int | None
  evidence_round: int | None
  qualification_result: QualificationResult | None
  qualification_path: QualificationPath | None
  policy_version: PolicyVersion | None
  conflict_kind: VERSION | IDEMPOTENCY | UNIQUE_CONSTRAINT | None
  replay_of_audit_id: UUID | None
  external_status: NormalizedExternalStatus | None
  retryable: bool | None
}
```

禁止字段：

- `raw_payload`、`raw_prompt`、`raw_response`、`signature`；
- 证据文件内容、下载 URL、对象存储临时 token；
- 完整手机号、地址、支付凭证、银行卡号；
- 数据库异常原文、供应商异常原文；
- 客户输入的完整聊天原文。

实际实现可以将该类型序列化为 JSONB，但必须先通过白名单构造器，不能把任意
`dict` 直接传给 `audit_store`。

## 11. Outbox payload 类型

退款 Outbox 只允许使用受控 payload：

```text
RefundOutboxPayload {
  outbox_event_id: UUID
  refund_id: UUID
  after_sale_request_id: UUID
  merchant_refund_request_no: MerchantRefundRequestNo
  payment_transaction_ref: PaymentTransactionRef
  amount_cents: PositiveCents
  currency: Currency
  attempt: PositiveInt
  created_at: UtcDateTime
}
```

禁止 payload 包含：

- 客户密码、token、cookie、API key；
- 原始证据、支付签名或完整 callback body；
- 不必要的手机号、地址、姓名；
- LLM prompt/response；
- 任意用户可传入的 URL 或 adapter 名称。

Outbox payload 只有两种合法触发来源：

1. 客户完成二次确认且确定性资格为合格；
2. finance 批准申请。

`Outbox.SUCCEEDED` 只表示适配器已接收/投递成功；资金成功必须由已验证 callback
或对账结果更新 Refund。

## 12. 领域异常契约

领域服务不抛 FastAPI `HTTPException`，也不返回 HTTP 状态码。使用受控领域错误：

```text
DomainError {
  code: DomainErrorCode
  safe_message: str
  resource_type: ResourceType | None
  resource_id: UUID | None
  retryable: bool
}
```

`DomainErrorCode` 至少包括：

```text
VERSION_CONFLICT
IDEMPOTENCY_CONFLICT
IDEMPOTENCY_REPLAY
INVALID_STATE
RESOURCE_NOT_AVAILABLE
FORBIDDEN
DEPENDENCY_UNAVAILABLE
DUPLICATE_ACTIVE_REQUEST
ORDER_NOT_ELIGIBLE
EVIDENCE_REQUIRED
EVIDENCE_LIMIT_EXCEEDED
CLAIM_NOT_AVAILABLE
CLAIM_EXPIRED
QUOTE_EXPIRED
RESPONSIBILITY_CONFLICT
INVALID_AMOUNT
INVALID_CURRENCY
EXTERNAL_EVENT_INVALID
```

映射原则：

- S3-03 才将领域错误映射为 HTTP/统一 API 错误；
- 客户面对不存在、非本人和 UNMATCHED 统一为 `RESOURCE_NOT_AVAILABLE`，防止枚举；
- `IDEMPOTENCY_REPLAY` 在服务内部更适合作为结果标志，通常不作为失败响应；
- 不把 SQLSTATE、堆栈、供应商异常原文放入 `safe_message`；
- `DEPENDENCY_UNAVAILABLE` fail-closed，不创建状态、Refund 或 Outbox。

## 13. 应用服务方法清单

以下是 S3-02 应实际冻结的 typed service 方法；每个方法只接收对应 command，返回
`CommandResult`，不接收 `Request`、任意 dict、客户端角色或裸 connection：

```text
submit_after_sale(command: SubmitAfterSaleCommand) -> Awaitable[CommandResult]
cancel_after_sale(command: CancelAfterSaleCommand) -> Awaitable[CommandResult]
claim_after_sale(command: ClaimAfterSaleCommand) -> Awaitable[CommandResult]
release_or_recover_claim(
  command: ReleaseOrRecoverClaimCommand,
) -> Awaitable[CommandResult]
submit_evidence(command: SubmitEvidenceCommand) -> Awaitable[CommandResult]
submit_review(command: SubmitReviewCommand) -> Awaitable[CommandResult]
finance_approve(command: FinanceApproveCommand) -> Awaitable[CommandResult]
finance_reject(command: FinanceRejectCommand) -> Awaitable[CommandResult]
```

以下方法只作为 S3-04/S3-05 预留，不在 S3-02 实现：

```text
confirm_auto_refund(command: ConfirmAutoRefundCommand) -> Awaitable[CommandResult]
evaluate_eligibility(command: EvaluateEligibilityCommand) -> Awaitable[CommandResult]
start_refund_processing(
  command: StartRefundProcessingCommand,
) -> Awaitable[CommandResult]
record_refund_callback(
  command: RecordRefundCallbackCommand,
) -> Awaitable[CommandResult]
run_system_maintenance(
  command: SystemMaintenanceCommand,
) -> Awaitable[CommandResult]
```

## 14. 契约到数据库字段的约束

- `after_sale_request_id`、`refund_id`、`outbox_event_id` 只能是内部 UUID；
- `order_id` 是旧订单号引用，不代表已确认归属；
- `customer_user_id` 必须来自已确认所有权事实；
- `payment_transaction_ref`、`amount_cents`、`currency` 必须来自受控 PaymentFact；
- `refund_amount_cents == payment_amount_cents`，不能接收命令金额；
- `version` 每次成功聚合变更加 1；
- 状态字段只能由应用服务写入，repository 不提供任意状态更新方法；
- 外部 callback 标识只由 callback 服务写入，普通客户/Agent 命令不能提交；
- 审计 metadata 和 Outbox payload 必须由白名单类型构造，不能直接接收 JSON 字典。

## 15. 契约验收清单

契约通过前必须逐项确认：

- [ ] ActorType、Source 与数据库 CHECK/审计模型一致；
- [ ] 每个 S3-02 命令都有明确参数对象；
- [ ] 客户/内部命令的幂等键要求已明确；
- [ ] 除新申请外的每个并发写命令都有 `expected_version`；
- [ ] 统一成功结果包含资源 ID、状态、版本、幂等重放标志；
- [ ] 领域异常已与 API 层解耦；
- [ ] Provider、EligibilityProvider、Clock、UnitOfWorkFactory 已有 typed 协议；
- [ ] 审计 metadata 和 Outbox payload 不允许任意 JSON；
- [ ] callback 不要求 Idempotency-Key，且有独立去重上下文；
- [ ] 预留命令没有提前实现 S3-04/S3-05 行为；
- [ ] 幂等记录落点和 callback 三元组唯一约束已核对，未解决则登记阻塞；
- [ ] 没有任何签名接收 FastAPI Request、客户端 role、任意 status 或裸 SQL connection。

## 16. 当前阻塞项

1. `audit_events` 当前可以通过 `(actor_user_id, command_name, idempotency_key)`
   的部分唯一索引表达命令幂等索引，但需要在实现前确认如何安全保存首次命令摘要
   和结果；不能用进程内字典替代。
2. `refunds` 当前有商户退款请求号和外部退款流水唯一约束，但 callback 要求的
   `external_event_id + merchant_refund_request_no + external_refund_id` 稳定组合，
   需要在 S3-04 前核对是否需要额外唯一结构；不能只依赖“先查后插”。
3. 事实 provider 和确定性资格 provider 尚无生产实现；S3-02 只能依赖接口和隔离
   测试 fake，不能回退旧订单字段。

契约通过后，下一步才是实现 Store 的受控 repository 方法和 Application Service；
在此之前不执行数据库写入、不运行迁移、不接支付、不实现 API。
