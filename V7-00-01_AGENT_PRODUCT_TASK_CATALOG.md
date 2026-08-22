# V7-00-01：Agent 产品与任务目录

状态：架构草案，待负责人冻结
依赖：PLAN_V7.md、S0_DECISION_RECORD.md、S0-03_API_SLO_DATA_CLASSIFICATION.md
范围：定义首版 Harness 的内部任务契约；不实现 Agent、工具、数据库、报表、支付或物流。

## 1. 目标与非目标

V7 的 Agent 负责拆解内部任务、选择已获准工具、整理事实并交付带引用的结果。金额、账龄、指标、订单、
支付、库存、履约、售后和退款事实都由确定性服务决定；模型推测永远不是业务事实。

每个首版任务必须有：明确请求人和角色、受控输入与资源范围、可枚举产物、事实引用、失败人工出口；
任何副作用还必须有确定性服务、幂等、审计与恢复语义。

不在范围：客户直接交易 Agent、自动退款、自动资金确认、任意 SQL、任意文件上传、广告 ROI、利润、
税务、总账或跨租户分析。

## 2. 共同任务模型

| 字段 | 规则 |
|---|---|
| task_id | 服务端生成的不可枚举内部 ID |
| requester | 当前认证操作者；不接受客户端自报角色或用户 ID |
| profile_id | 已发布 AgentProfile 版本；只能收缩权限 |
| task_kind | 本文定义的受控枚举 |
| input | 对应任务类别的版本化 DTO，不使用任意领域字典 |
| resource_scope | 服务端解析并校验的资源、字段、部门与时间范围；V7-01 固定为 task 所属单一组织，禁止跨组织聚合或引用 |
| constraints | 受控的预算、产物、只读和审批限制 |
| idempotency_key | 与调用者、task_kind、scope 和输入摘要绑定 |
| expected_artifacts | 允许交付的摘要、草稿、报告或待办类型 |
| requested_at | 服务端 Clock 生成的 UTC 时间 |

有效权限固定为：

当前操作者权限 ∩ AgentProfile 工具范围 ∩ 当前资源范围 ∩ AgentTask 约束

AgentProfile 绝不能授权操作者原本不具备的角色、工具、字段、部门或资源。任务恢复、审批和每次工具调用
都重新计算该交集。

## 3. 首版任务目录

任务目录定义目标能力，不等于 V7-01 全部启用。V7-01 只允许通过工具盘点的客服只读任务；财务、运营
和异常任务在 canonical 事实、确定性服务及工具验收完成后启用。

| 任务类别 | 请求角色 | 最早启用 | 允许产物 | 明确禁止 |
|---|---|---:|---|---|
| SUPPORT_KNOWLEDGE_ASSIST | agent | V7-01 | 知识引用、回复草稿、未知项 | 改订单、退款、越权读取客户资源 |
| SUPPORT_ORDER_RESEARCH | agent | V7-04 后 | 订单时间线、事实摘要、回复草稿 | 从聊天或 RAG 推断支付/履约事实 |
| FINANCE_RECONCILIATION_REPORT | finance | V7-05 后 | ReportSnapshot、Artifact、核查待办 | 自行算金额、确认到账、总账凭证 |
| OPERATIONS_DAILY_REPORT | operator | V7-05 后 | 指标快照、日报周报、跟进待办 | 改价格、库存、活动或虚构 ROI |
| EXCEPTION_INVESTIGATION | agent、finance、operator | V7-05 后 | 事实时间线、未知项、建议草稿 | 写异常事实、关闭资金异常、执行 L3/L4 |

## 4. 任务卡

### 4.1 SUPPORT_KNOWLEDGE_ASSIST

| 项目 | 契约 |
|---|---|
| 输入 | 客户问题摘要、受控知识范围、可选已授权工单引用 |
| 允许工具 | 知识检索、政策摘录、已授权只读工单摘要 |
| 输出 | 回复草稿、知识引用、未知项、建议人工下一步 |
| 风险 | L0/L1；不创建业务状态 |
| 成功 | 每个政策结论有知识版本引用；信息缺失时明确未知 |
| 非目标 | 不调用支付、库存、退款或订单写服务 |

V7-01 最小演示任务是：客服询问一项公开售后政策，Harness 检索两个允许知识源；遇到冲突或缺失时返回
人工可读的未知项，不臆造规则。

### 4.2 SUPPORT_ORDER_RESEARCH

| 项目 | 契约 |
|---|---|
| 输入 | 已解析的 canonical SalesOrder 引用或受控候选范围、问题类别 |
| 允许工具 | 订单时间线、履约摘要、售后摘要、知识摘录 |
| 输出 | 事实时间线、证据来源、未知项、回复草稿 |
| 风险 | L0/L1；只能读取当前 agent scope 内资源 |
| 成功 | 每个事实可回到业务资源或事件引用 |
| 非目标 | 不认领售后、不改物流、不创建退款 |

该任务依赖 V7-03B 和 V7-04 的确定性查询服务，不能用 legacy UNMATCHED 订单伪造正式演示。

### 4.3 FINANCE_RECONCILIATION_REPORT

| 项目 | 契约 |
|---|---|
| 输入 | 业务自然日或 UTC 范围、渠道范围、报告模板版本 |
| 允许工具 | 对账工作流、差异读取、Report Service、创建核查 WorkItem |
| 输出 | ReportSnapshot 引用、Artifact、差异分类、核查待办 |
| 风险 | L0/L1，加受控 L2 WorkItem；资金结论由对账服务决定 |
| 成功 | 金额、差异、账龄与同一 ReportSnapshot 一致，标注 as-of 与完整性 |
| 非目标 | 不确认到账、不发起退款、不生成总账或税务凭证 |

对账服务返回 PARTIAL、UNKNOWN 或依赖故障时，Agent 只能生成数据不完整报告或创建核查待办；不得补算
数字、把缺失项归零或宣称无差异。

### 4.4 OPERATIONS_DAILY_REPORT

| 项目 | 契约 |
|---|---|
| 输入 | 时间范围、已发布 MetricDefinition、允许 SKU/仓库/履约范围 |
| 允许工具 | 指标快照、SKU/库存/履约分析、Report Service、创建跟进 WorkItem |
| 输出 | 日报/周报 Artifact、指标引用、风险点和跟进待办 |
| 风险 | L0/L1，加受控 L2 WorkItem |
| 成功 | 每个数字有 MetricDefinition 和 ReportSnapshot；无数据源时显示不可用 |
| 非目标 | 不改商品价格、库存、政策、活动；不生成利润、ROI、税务结论 |

### 4.5 EXCEPTION_INVESTIGATION

| 项目 | 契约 |
|---|---|
| 输入 | ExceptionCase ID、调查目标、受控时间范围 |
| 允许工具 | 订单、支付、履约、售后、事件和政策的授权摘要；创建业务 ActionProposal |
| 输出 | 事实时间线、冲突点、未知项、原因候选、受控建议草稿 |
| 风险 | L0/L1；业务 ActionProposal 为 L2；L3/L4 仍由人工与领域服务处理 |
| 成功 | 推测和事实明确分离；每条建议绑定案件、资源、证据和有效期 |
| 非目标 | 不创建异常事实、不关闭资金异常、不直接取消订单或释放库存 |

该任务只能在 V7-03D 的 ExceptionCase、WorkItem 和业务 ActionProposal 已验收后启用。

## 5. 统一输出契约

| 字段 | 含义 |
|---|---|
| outcome | SUCCEEDED、PARTIAL、NEEDS_APPROVAL、NEEDS_HUMAN 或 FAILED |
| summary | 面向当前角色的最小事实摘要 |
| facts | 受控事实、来源引用、发生时间与可信度类别 |
| unknowns | 缺失、冲突或不可访问的事实 |
| artifacts | Artifact 或草稿引用，不内嵌大文件或 PII |
| work_items | 已创建待办的安全引用；没有时为空 |
| approvals | 等待中的 RunApprovalRequest；没有时为空 |
| next_actions | 允许展示的受控人工下一步，不含任意状态字符串 |
| as_of | 聚合/报告的数据截止时间；不适用时为空 |

facts 只记录确定性工具返回内容。原因候选必须标记为 hypothesis，不能写入事实集合。confidence 只能
用于展示和排序，不能作为授权、金额、状态转换或自动放行条件。

## 6. 任务审批与业务建议的分界

V7-01 仅有 RunApprovalRequest，用来确认当前 Run 是否继续执行已定义的任务步骤；它不包含业务状态、
资金、订单或库存命令。

V7-03D 后才可创建业务 ActionProposal。它必须绑定 ExceptionCase、目标资源、证据引用、action_kind、
允许审批角色、有效期和去重键。接受建议后，明确映射的 Application Service 必须重新校验状态、权限、
金额和幂等，不能根据 Agent 自然语言猜测命令。

## 7. 启用门禁与未决项

一个任务类别进入 AgentProfile 前必须满足：

1. 对应事实和确定性服务已验收；
2. 所需工具完成 Tool Gateway 契约测试；
3. scope、字段权限和输出数据分级可验证；
4. 至少有正常、缺失事实、越权、Prompt 注入、工具失败和恢复样例；
5. 有任务 owner、人工失败出口和产物访问规则；
6. L2 以上副作用具备幂等、审计和撤销或关闭语义。

已冻结：V7-01 仅支持单组织 task scope，不支持跨组织任务、资源引用、上下文装配或结果聚合。

待负责人冻结：各任务预算和审批期限，以及 V7-03D 的 action_kind 与审批映射目录。
