# V7-00-04：上下文、记忆、Artifact 与评测契约

状态：架构草案，待负责人冻结
依赖：PLAN_V7.md、V7-00-01_AGENT_PRODUCT_TASK_CATALOG.md、V7-00-02_HARNESS_RUN_STATE_CONTRACT.md、V7-00-03_TOOL_GATEWAY_RISK_APPROVAL_CONTRACT.md
范围：冻结 Harness 的上下文装配、长期记忆、产物、数据留存、评测与运行观测；不实现对象存储、报表或评测代码。

## 1. 上下文装配

上下文由服务端按 Task、actor、scope 和 ToolSpec 组装。模型输入分为以下受控区段：

| 区段 | 允许内容 | 禁止内容 |
|---|---|---|
| 系统策略 | 固定安全规则、风险边界、输出 schema | 用户自报角色、外部指令覆盖安全规则 |
| 任务摘要 | task_kind、允许目标、预算、当前状态 | 原始 token、密码、密钥、任意业务 payload |
| actor/scope 摘要 | 当前允许工具、资源和字段范围 | 完整用户档案或超范围资源 |
| 事实引用 | 知识版本、业务资源、ReportSnapshot、事件引用 | 未过滤支付/物流原始报文 |
| 工具结果摘要 | ToolSpec 输出白名单和引用 | 大量原始行、完整 PII、签名 |
| 历史运行摘要 | 已完成 Step、未知项、审批与 checkpoint | 未经批准的完整聊天或模型思维过程 |

RAG 是知识投影，不是订单、价格、库存、支付、履约或退款事实来源。业务事实必须由对应只读数据服务
提供，所有引用可回溯到资源版本、知识版本、事件或快照。

## 2. 记忆规则

首版只允许显式、结构化、可删除的偏好或工作配置，例如报告模板偏好、默认展示语言和已保存查询视图。
模型不得从聊天自动沉淀客户身份、资金状态、政策结论、订单事实或跨任务业务判断。

| 记忆类别 | 是否允许 | 规则 |
|---|---|---|
| 用户偏好 | 允许 | 显式确认、最小字段、可查看删除 |
| 工作配置 | 允许 | 绑定 owner 与组织范围，版本化 |
| Run checkpoint | 允许 | 只保存恢复所需引用和受控摘要 |
| 业务事实 | 不允许作为记忆 | 必须每次从可信服务读取 |
| 完整聊天/模型上下文 | 默认不允许 | 只有另行批准的数据治理流程可开启 |

## 3. Artifact 契约

Artifact 是确定性服务生成或登记的受控产物。LLM 可以请求生成、解释或撰写展示摘要，但不得自行拼接
权威数字或直接写对象存储。

| 字段 | 规则 |
|---|---|
| artifact_id、artifact_type、schema_version | 不可枚举 ID、受控类型、版本化格式 |
| producer | Report Service、草稿服务或其他获准服务身份 |
| owner_scope | 可下载的 actor、部门、案件或资源范围 |
| source_refs | MetricDefinition、ReportSnapshot、知识或业务资源引用 |
| as_of、completeness | 数据截止时间与 COMPLETE/PARTIAL 状态 |
| content_hash、size_bytes、content_type | 完整性、大小和受控 MIME 类型 |
| storage_ref | 私有 ArtifactStore 引用，不暴露底层对象路径 |
| expires_at、retention_policy | 访问和保留边界 |

开发环境使用隔离 S3-compatible ArtifactStore；生产使用受管私有对象存储。禁止将可下载产物长期放在
应用本地磁盘。下载必须经过当前 actor/scope 授权，使用短期单资源令牌或受控下载端点。

首版只允许服务端模板生成 CSV、XLSX、PDF，最大 20 MiB，不接受外部文件上传。未来接入上传前必须先
定义隔离、病毒扫描和人工或策略放行。

## 4. 报表完整性与迟到事实

所有事件以 UTC 持久化；首版“昨天/本周”等业务自然日按 Asia/Shanghai 解释，时间窗使用 [start, end)。

ReportSnapshot 必须保存：MetricDefinition 版本、输入范围、as-of watermark、完整性、结果哈希和事实引用。
同一指标版本、范围、as-of 与完整性状态必须可确定性重生成，不能因 LLM 运行而改变数字。

迟到 callback、补录事件或对账更正不得篡改已交付快照；系统创建新快照或对账调整，并在 Artifact 中标注
版本和数据截止时间。PARTIAL 报告必须明确原因，Agent 不得把缺失项补零或表述为已完成。

## 5. 数据分级与留存

| 数据 | 默认处理 |
|---|---|
| 模型、Prompt、Profile、ToolSpec、知识版本 | 可保存版本与哈希 |
| task/run/step 元数据 | 保存受控状态、错误类别、时间和引用 |
| 工具参数与结果 | 保存摘要与引用，不保存未白名单原文 |
| 订单、聊天、客户描述、支付/物流 payload | 仅当前 scope 内临时使用，默认不持久化到 Run |
| token、密码、密钥、签名、完整证据 | 禁止进入上下文、Artifact、普通日志和运行记录 |

V7-01 已冻结：Harness 只支持单组织 scope；Run、Step、审批和调用审计仅保存脱敏元数据、受控摘要与引用，`RUN_RETENTION_DAYS=30`。默认留存不包含完整 Prompt、聊天、PII、token、签名、支付/物流报文或证据原文。

法律或合规留存例外仅能通过显式 `retention_hold` 表达：只有受权管理员能设置或解除，并必须审计设置人、时间、理由码、原到期时间和解除人。`retention_hold` 尚未实现前，不得宣称具备法律留存能力，也不得由 Agent、worker、Profile、普通用户或请求参数延长记录保留。

ArtifactStore 账户与访问策略、报告模板目录和 Artifact 保留期属于 V7-03C 前置项；未来上传文件流程属于单独安全设计前置项。

在未形成受控规则前，不实现自动清理。证据仍遵守 S0 D05 的关闭后 180 天保留规则。

## 6. EvaluationCase 契约

EvaluationCase 是固定、可版本化的评测输入，不等同于生产任务或真实客户数据。

| 字段 | 规则 |
|---|---|
| case_id、case_version、task_kind | 固定标识和目标任务 |
| fixture_refs | 合成或已批准脱敏事实引用 |
| actor_profile_scope | 明确的最小角色和资源范围 |
| allowed_tools | 白名单 ToolSpec 版本 |
| expected_facts、expected_unknowns | 必须引用或必须声明未知的项目 |
| forbidden_actions | 绝不能调用的工具、字段或 L3/L4 行为 |
| expected_artifacts | 允许的产物类型、as-of、完整性 |
| scoring_rules | 完成、事实忠实、工具选择、安全、恢复与成本断言 |

回放含义是使用固定任务输入、模型/Prompt/工具/知识版本重新运行，并比较结构化评分、事实引用、安全断言
和副作用；不要求模型逐字复现自然语言文本。

V7-01 冻结跨角色通用 fixture、工具轨迹、评分与安全断言骨架。V7-05 四类能力各至少补充 15 个案例，
但不得另造不兼容的格式或评分体系。

## 7. 运行观测

必须记录成功率、人工接管率、工具失败、重试、审批等待、恢复失败、Token/费用、端到端耗时及
Artifact 生成失败。标签不得包含用户 ID、订单号、原始错误、完整 Prompt 或 PII。

每次任务至少可关联 actor、task、run、step、Profile、模型、Prompt、工具、知识、Artifact 和安全
引用版本。可观测记录用于排障和评测，不改变业务事实或权限。

## 8. V7-01 最小验收与未决项

V7-01 至少验证：scope 外事实不会进入上下文；Prompt 注入不能改变允许工具；Run 记录不保存敏感原文；
同一固定 case 可重跑并得到可评分结果；LLM 故障仍能交付人工可读失败摘要；所有工具调用可关联到版本和
引用。

待负责人冻结：`retention_hold` 的管理员授权模型、理由码目录和实现验收；V7-03C 的 ArtifactStore 开发环境账户与访问策略、报告模板目录、Artifact 留存和跨组织报告范围；以及未来用户上传文件的安全流程。
