# V7-00-03：Tool Gateway、风险与审批契约

状态：架构草案，待负责人冻结
依赖：PLAN_V7.md、V7-00-01_AGENT_PRODUCT_TASK_CATALOG.md、V7-00-02_HARNESS_RUN_STATE_CONTRACT.md
范围：冻结 ToolSpec、统一网关、授权交集、风险、幂等、失败和 MCP 治理；不实现工具、网关或业务服务。

## 1. 不可违反的边界

1. Agent 不直接访问 PostgreSQL、Redis、对象存储、支付 SDK、物流 SDK 或任意 HTTP 地址。
2. 工具只能调用 Application Service 或明确的只读数据服务；不得把 SQL、状态字段更新或 SDK 访问包装成自由工具。
3. 本地工具、MCP 工具、工作流工具和报表工具都必须通过同一 Tool Gateway。
4. Tool Gateway 不是 Application Service 的替代品。领域服务仍负责状态机、金额、事务、幂等和审计。
5. Prompt、客户端参数、模型输出或 MCP 元数据不能扩大工具、字段、资源或角色权限。

## 2. ToolSpec 契约

每个注册工具必须有一个不可变、版本化 ToolSpec。

| 字段 | 规则 |
|---|---|
| tool_id、tool_version | 稳定标识；新不兼容输入输出必须新版本 |
| capability_kind | READ、ARTIFACT、INTERNAL_WORKFLOW、BUSINESS_COMMAND、EXTERNAL_ADAPTER |
| input_schema、output_schema | 类型化 DTO；禁止裸领域字典和任意 SQL/表达式 |
| risk_level | L0 到 L4，按第 4 节处理 |
| allowed_profiles | Profile 白名单；仍需与 actor 权限求交集 |
| required_scope | 订单、案件、部门、时间范围等资源规则 |
| field_policy | 输入和输出允许字段、脱敏和最大大小 |
| timeout_policy、retry_policy | 受控配置，不由模型传入 |
| idempotency_policy | NONE、STEP_REQUIRED 或 COMMAND_REQUIRED |
| approval_policy | NONE、RUN_APPROVAL 或 BUSINESS_APPROVAL |
| audit_policy | 必写调用审计字段和允许结果摘要 |
| dependency_class | 本地、MCP、支付、物流、报表或对象存储 |

ToolSpec 不得包含密钥、URL 凭证、任意动态模块名、任意表名、任意列名或模型可覆写的权限说明。

## 3. 授权交集与调用顺序

每次调用重新计算：

当前操作者权限 ∩ AgentProfile 允许工具 ∩ 当前资源范围 ∩ AgentTask 约束

V7-01 固定为单组织：Task 的 `organization_id` 由认证与服务端任务创建流程派生，Gateway 必须拒绝任何跨组织资源引用、聚合查询或模型传入的组织标识。

调用顺序固定：

1. 读取当前 actor、Run、Step、Profile 与 Task；
2. 校验有效 lease、fencing token、Run 状态、预算和任务期限；
3. 确认剩余 lease 不少于 `ToolSpec.timeout_seconds + RUN_LEASE_SAFETY_MARGIN_SECONDS`；不足时仅可先成功续租，不能派发调用；
4. 计算授权交集并确认 tool_id 被允许；
5. 服务端解析资源引用，校验 owner、部门、案件或字段 scope；
6. 按 input_schema 校验模型输出，拒绝多余字段和任意资源 ID；
7. 应用风险、审批、timeout、retry、circuit breaker 和幂等规则；
8. 调用 Application Service 或只读数据服务；
9. 输出按 field_policy 脱敏、截断和转换为引用；
10. 原子写 Tool 调用摘要、结果引用、错误类别和 Run checkpoint，并以 fencing token、lease 未过期和版本条件保护写入。

权限交集失败时，网关返回安全的 RESOURCE_NOT_AVAILABLE 或 FORBIDDEN 语义，不向模型或调用者泄露
超范围资源是否存在。恢复、审批和重试时重复上述校验。

lease 失效后的旧 worker 不得使用迟到结果推进 Run。该限制只保护 Harness 的状态写入，无法撤回已离开的外部请求；因此 L2 及以上能力必须把同一 `step_idempotency_key` 传给目标 Application Service，未知结果必须查询后再重试，不能靠换 worker 或换 key 重发。

## 4. 风险与审批规则

| 风险 | 典型能力 | V7-01 规则 | 后续规则 |
|---|---|---|---|
| L0 | 知识、时间线、指标、对账摘要读取 | 可自主调用 | 保持 scope 校验 |
| L1 | 摘要、回复草稿、确定性报表 Artifact | 可自主调用 | 输出必须有来源、as-of 与完整性 |
| L2 | 创建工单、WorkItem、ActionProposal | V7-01 禁止业务 L2，仅允许任务级审批元数据 | V7-03D 后按确定性服务、幂等、审计和撤销语义开放 |
| L3 | 取消订单、释放库存、审批售后 | 禁止 | 人工业务审批后由 Application Service 执行 |
| L4 | 收款、退款、确认到账、改金额 | 永久禁止 Agent 直接调用 | 仅 worker、验签 callback、对账流程 |

RunApprovalRequest 只允许确认 Run 是否继续，不得被用作 L3 业务审批的替代品。业务审批必须有单独
业务资源、审批人、版本和 Application Service 命令。

## 5. 幂等、timeout 和失败语义

| 工具类别 | 幂等要求 | timeout 后处理 |
|---|---|---|
| L0 只读 | 无业务幂等，但记录 Step | 可按策略重试或降级 |
| L1 Artifact | 使用 Artifact 输入摘要与版本去重 | 查询是否已生成同一 Artifact，再决定重试 |
| L2/L3 命令 | 必须透传 step_idempotency_key 到领域服务 | 先查询命令结果，不得换 key 重放 |
| 外部适配器 | 必须有领域操作号和适配器查询能力 | 未知结果先查询或对账 |

错误只可归入受控类别：INVALID_INPUT、NOT_AUTHORIZED、RESOURCE_NOT_AVAILABLE、DEPENDENCY_UNAVAILABLE、
TIMEOUT_NO_SIDE_EFFECT、UNKNOWN_OUTCOME、CONFLICT、BUDGET_EXCEEDED、POLICY_REJECTED、INTERNAL_ERROR。
原始异常、URL、凭证、支付报文和完整 PII 不得进入模型、审计摘要或普通日志。

## 6. MCP 治理

MCP 只可作为低风险、可审计、可超时降级的能力载体。接入一个 MCP 工具前必须：

1. 由服务端注册成具体 ToolSpec，禁止动态信任远端工具列表；
2. 给出类型化输入输出、最大响应、字段脱敏、timeout 和熔断策略；
3. 在 Gateway 内做 actor、scope、风险、幂等和审计校验；
4. 将远端错误转换为受控错误类别；
5. 明确该工具不持有支付、物流写、数据库或超范围资源权限。

MCP 不因运行在独立进程或远端而获得更高权限。外部服务 URL 和凭证由配置注入，模型不能指定或修改。

## 7. 现有工具盘点与 V7-01 准入门禁

现有 `ToolRegistry` 注册项不是 V7 ToolSpec。V7-01 不直接复用它们：下表记录当前能力的事实性处置，直到通过独立 ToolSpec、字段策略和 Gateway 测试后才可重新准入。

| 工具 | 当前身份来源 | 读写能力 | 数据范围 | 基础依赖 | V7 处置 | 复审人 |
|---|---|---|---|---|---|---|
| `search_product` | 当前聊天认证用户与 Agent 上下文；无 Harness task 身份 | 读 | legacy 商品/RAG 数据，字段范围未按 V7 固定 | PostgreSQL、向量检索、embedding/reranker | 禁用，待 canonical Catalog 与 Gateway 字段策略完成 | 小组长 + 安全复审人 |
| `search_component` | 同上 | 读 | legacy 配件/RAG 数据，字段范围未按 V7 固定 | PostgreSQL、向量检索、embedding/reranker | 禁用，待 canonical Catalog 与 Gateway 字段策略完成 | 小组长 + 安全复审人 |
| `compare_products` | 同上 | 读 | legacy 商品与检索结果 | PostgreSQL、RAG | 禁用，待 canonical Catalog、报价快照和 Gateway 契约完成 | 小组长 + 交易域负责人 |
| `check_stock` | 同上 | 读 | 当前库存查询范围未绑定 canonical SKU/组织 scope | PostgreSQL | 禁用，待 Inventory/Reservation 与资源范围模型完成 | 小组长 + 库存域负责人 |
| `track_order` | 同上 | 读 | legacy 订单；当前不是 canonical SalesOrder scope | PostgreSQL | 禁用，待 canonical 订单事实与客户/组织 scope 完成 | 小组长 + 安全复审人 |
| `create_ticket` | 同上 | 写 | 工单、客户描述 | PostgreSQL | 禁用；属于 L2，必须经未来 Application Service、幂等和审计 | 小组长 + 客服域负责人 |
| 动态 MCP 注册工具 | 远端 MCP 元数据与当前聊天上下文 | 读/写未知 | 供应商定义，字段与依赖未固定 | MCP Server | 全部禁用；只有显式 ToolSpec 评审后可按单工具重新准入 | 安全复审人 + 负责人 |

V7-01 新增的准入工具必须是 Gateway façade，不得直接把下面能力映射为现有 `ToolRegistry` 类或动态 MCP 工具：

| V7-01 ToolSpec | 风险 | 身份来源 | 输入/输出字段范围 | 基础依赖 | 处置 | 复审人 |
|---|---|---|---|---|---|---|
| `knowledge.search.v1` | L0 READ | Harness 从认证中间件取得的 `AgentTask.requester_subject_id`；模型不可提供或覆盖 | `timeout_seconds=10`；输入仅 `query`、服务端固定 `limit` 与同组织已批准知识 source；输出仅 `document_ref`、`title`、`excerpt`、`knowledge_version`；不返回 embedding、原始 metadata、客户/订单事实 | 已批准的只读知识投影/RAG 服务 | 允许实现为新的只读 Gateway façade；只服务 V7-01 合成或公开知识 scope | 小组长 + 安全复审人 |
| `policy.excerpt.v1` | L0 READ | 同上 | `timeout_seconds=5`；输入仅受控 `policy_topic` 或受限检索词；输出仅 `policy_ref`、`policy_version`、`effective_at`、客户安全的 `excerpt`；不得创建、编辑、发布或回滚政策 | 已批准、版本化、只读的政策知识投影 | 允许实现为新的只读 Gateway façade；政策内容必须有版本与有效时间 | 运营域负责人 + 安全复审人 |
| `ticket.authorized_summary.v1` | L0 READ | 同上，且 Gateway 服务端验证当前 agent 对 task scope 内、同组织工单的授权 | `timeout_seconds=5`；输入仅任务 scope 已绑定的 `ticket_ref`，拒绝模型任意枚举 ID；输出仅 `ticket_id`、`status`、`urgency`、`created_at`；不得返回客户姓名、电话、issue 原文、聊天或其他订单 | 受控 Ticket Query Service 与当前授权数据 | 允许实现为新的只读 Gateway façade；仅已授权工单，不使用未认领摘要作为枚举入口 | 客服域负责人 + 安全复审人 |

未在上表明确准入的工具不得注册到 V7 Harness。当前聊天功能可继续使用既有路径，但不得冒充已通过 V7 Gateway。

## 8. V7-01 最小工具集与验收

V7-01 仅允许第 7 节列出的三个 L0 只读 Gateway façade；本阶段不启用 L1 Artifact 或任何既有 `ToolRegistry`/动态 MCP 工具。至少验证：

1. Profile 尝试扩大 actor 权限时被拒绝；
2. 模型构造超范围资源 ID 时被拒绝；
3. Prompt 注入不能改变 ToolSpec、风险、timeout 或工具集合；
4. 当前 Harness 的 ToolSpec 集合严格等于第 7 节三个 façade；任何 MCP 或既有本地工具注册均被拒绝。MCP 同类工具的 scope/字段一致性测试移至 V7-04 准入阶段；
5. timeout、熔断和依赖故障不泄露内部错误；
6. 输出不含 token、签名、原始 PII 或未授权字段；
7. 相同 Step 重放不重复产生 RunStep 结果或任务结果；
8. 每个调用可追溯到 actor、task、run、step、tool_version 和资源范围摘要。
9. 剩余 lease 不足覆盖 timeout 加安全余量时，Gateway 不实际调用下游依赖；失 lease 的迟到结果不能推进 Run。

## 9. 待负责人冻结

- ToolSpec 的公开 schema 版本策略；
- 三个准入 façade 的具体查询服务实现；
- L1 Artifact 的最大运行/输出限制；
- V7-03D 起 L2 action_kind、撤销语义和业务审批映射；
- MCP 供应商准入、变更复审和停用流程。
