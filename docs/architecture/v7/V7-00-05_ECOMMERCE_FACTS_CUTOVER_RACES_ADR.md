# V7-00-05：电商事实切换与支付库存竞态 ADR

状态：架构草案，待负责人冻结
依赖：PLAN_V7.md、S0_DECISION_RECORD.md、V7-00-01 至 V7-00-04
范围：定义 V7 canonical 电商事实、legacy 隔离、事件职责、迁移切片与支付库存竞态；不创建表、不迁移、不连接数据库、不接支付或物流。

## 1. 背景

当前项目存在 legacy 商品表、50,000 条 UNMATCHED legacy orders，以及 S3 售后/退款 schema。它们不能成为 V7 新交易写真相：legacy orders 归属未确认且 status 混合支付、履约、售后语义；paid_amount/payment_time 是模拟字段；现有商品检索表不具备 canonical SKU、价格快照和库存预留不变量；S3 已完成 Store/领域契约，未完成 legacy 售后 Application Service。

V7 需要最小可信电商沙箱为 Harness 提供工具环境，但不能用双写 legacy/canonical、模型推断或事件重放补齐事实。

## 2. 决策

### 2.1 canonical 是唯一写入真相

V7 新交易只写 canonical Catalog/SKU、Inventory/Reservation、SalesOrder、PaymentTransaction、Fulfillment、AfterSale/Refund。legacy 商品表和 orders 永远只读，不自动归属、不回填、不创建新支付、售后或退款事实。

新售后和退款只能关联 canonical SalesOrder 与 PaymentTransaction。S3 legacy 表的合成历史仅用于迁移夹具；不得直接把旧外键改指向新表，也不得让同一客户命令同时写 legacy 与 canonical。

### 2.2 Catalog 与 RAG

canonical Catalog/SKU 是可售性、SKU ID、价格和库存归属的唯一来源。checkout quote 只能读取 canonical Catalog，并把商品、价格、数量、币种和版本写入 SalesOrderItem 快照。

RAG 是已发布 Catalog 的异步检索投影，只用于搜索、说明和政策问答。投影包含 canonical SKU ID、catalog version、投影事件 ID 和失败重试记录。检索不能决定可购买 SKU、库存或成交价格；legacy 商品表仅提供标注为 legacy 的搜索信息。

### 2.3 State、Audit、Outbox 与 Ledger

| 对象 | 唯一职责 | 禁止职责 |
|---|---|---|
| Aggregate State | 当前状态与业务不变量真相 | 消息队列或审计替代品 |
| Audit | 谁、何时、为何尝试/完成/拒绝命令 | 驱动状态或供任意消费者重放 |
| Transactional Outbox | 已提交事务的可靠投递记录 | 表示支付/退款已经到账 |
| Business Event Ledger | 追加式领域事件、read model、报表和异常输入 | 重放支付、退款或库存命令 |

Ledger 事件必须有不可变 event_id，并以 aggregate_type、aggregate_id、aggregate_version、event_type 或等价业务键避免重复投影。消费者必须持久化已处理事件或使用等价唯一约束。事件重放只能重建 read model、ReportSnapshot 或 ExceptionCase 投影。

## 3. V7-02 迁移切片

V7-02 只交付 ADR、migration、迁移夹具、空库/测试库验证和前滚方案，不交付 checkout、callback、worker 或页面。

| 切片 | 新事实 | 必须验证 | 禁止提前做 |
|---|---|---|---|
| 1 | Catalog/SKU 与价格快照 | SKU/version 唯一、CNY 分整数 | RAG 作为成交价格源 |
| 2 | Inventory/Reservation | 非负库存、预留唯一、并发锁字段 | 展示库存决定可售 |
| 3 | SalesOrder/SalesOrderItem | checkout 幂等、owner、金额快照 | 写 legacy orders |
| 4 | PaymentTransaction | 商户操作号与外部事件去重 | 使用 legacy paid_amount |
| 5 | Fulfillment | 独立履约状态、轨迹引用 | 从聊天推断发货 |
| 6 | canonical AfterSale/Refund 关联 | 无双写、可退余额事实来源 | 直接重指向 legacy 外键 |
| 7 | Ledger/Outbox/read model | event_id、聚合版本、投影去重 | 用重放执行命令 |

每切片必须有 ADR、独立 migration、合成迁移夹具、独立 _test 授权、空库验证、含数据测试库验证、约束/索引核对、downgrade 风险说明和前滚方案。生产回退不以 downgrade 代替前滚修复。

切片 6 的夹具必须包含 active after-sale、refund、audit、outbox 合成记录，证明迁移不丢关联、不自动激活 legacy 历史，也不改变 legacy orders 总数或 UNMATCHED 数量。

## 4. 支付、预留与取消竞态

### 4.1 checkout 与预留

1. 创建 SalesOrder 时，InventoryReservation 与订单初始状态在确定性事务边界内建立。
2. 同一 checkout 幂等键、同一 owner、同一输入摘要只能产生一个订单；同 key、不同摘要为冲突。
3. 可售量只由 Inventory 与有效 Reservation 决定；竞争最后一件库存时至多一人成功。
4. Reservation 过期释放必须幂等，并记录释放原因与事件。

### 4.2 支付成功与取消

1. callback 在同一事务语义中锁定 PaymentTransaction 与 SalesOrder，并校验商户操作号、外部事件 ID、外部交易流水、金额、币种和允许状态。
2. 取消只允许在系统尚未接受支付成功的状态完成，并只能由 Application Service 处理。
3. 支付成功晚于取消或 Reservation 过期时，订单不得自动恢复、自动发货或再次占用库存；必须进入等价于 PAYMENT_SUCCEEDED_AFTER_CANCELLATION 的异常，由查询/对账和人工处理收敛。
4. PaymentTransaction 为 UNKNOWN 时，不得释放 Reservation、标记失败或重复发起支付；必须先查询 Gateway。查询期限后仍未知则创建异常，不盲目重试。
5. 只有确认未支付或支付失败后，才允许释放 Reservation。

### 4.3 callback 与退款去重

支付 callback 的稳定去重组合至少包含外部事件 ID、商户支付操作号和外部交易流水。退款使用独立商户退款请求号和外部退款流水组合。callback 不使用浏览器 Idempotency-Key；必须先验签、校验商户、金额、币种、关联资源与状态，再写领域事实。

Outbox 投递成功只表示适配器已接收或投递请求，不能表示支付成功、退款到账或库存已同步。外部 timeout 或连接中断先查询或对账，再决定重试。

## 5. legacy 隔离与售后切换

同一客户购买、支付、履约、售后或退款命令只允许一个 canonical 写入口。legacy 表不接收新命令，也不被 canonical 命令同步更新。查询层必须带来源标识，不能把 legacy 与 canonical 混在同一当前订单列表。

客户不能因 legacy UNMATCHED 数据而看到或操作他人订单。内部 legacy 查询必须有来源标识、脱敏字段、只读权限与审计，不能成为新交易、支付、退款或指标输入。

切片 6 实施前负责人必须选择：新建 canonical 售后表；现有表增加来源判别和 canonical 引用；或显式映射表与只读历史投影。任何方案都不得删除或重写历史审计，也不得把 active legacy 合成记录自动升级为可处理业务。

## 6. S0 兼容性与后果

S0 D01-D12 继续约束正式售后退款：仅 CNY、全额退款、金额来自支付流水、支付/履约/售后分离、客户确认与财务职责分离、AI 不决定资格/金额/资金。V7 canonical 领域是落实这些约束的新事实基础，不改变业务含义。

正面后果是 Harness 获得可追溯事实、迁移风险被切片、迟到 callback 和库存竞争有固定收敛方式、RAG 与交易事实隔离。成本是更多小 migration/夹具、legacy 不能顺手作为新数据、切片 6 前不开放正式售后工具。

## 7. 实施前门禁

开始 V7-02 任一 migration 前，负责人必须确认：

1. 七切片的 owner、ADR、依赖和前滚方案；
2. canonical/legacy ID 的命名和来源标识；
3. Catalog 到 RAG 的投影事件、版本和失败处理；
4. State、Audit、Outbox、Ledger 的 schema 边界与事件键；
5. checkout、预留、取消、callback、UNKNOWN 的状态机；
6. 支付与退款 callback 去重组合；
7. active S3 合成夹具和迁移后不变量；
8. 不连接或修改业务库 postgres 的执行边界。

待负责人冻结：切片 6 具体方案；canonical 订单/支付状态枚举；Reservation 有效期、支付查询期限和异常 SLA；PaymentGateway/LogisticsGateway 字段与环境契约；legacy 内部只读最终范围。
