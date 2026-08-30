# E-Commerce-Agent 架构决策与当前开发任务

请按以下架构决策继续开发当前 E-Commerce-Agent。

本说明是当前项目的统一架构约束。不要基于局部 benchmark 分数随意改变整体设计。

# 一、系统总体目标

当前项目不是单纯的客服问答机器人，而是一个：

> 能理解用户诉求、查询真实业务状态、按照 SOP 推进业务流程，并在安全边界内代办低风险业务操作的企业客服 Agent。

核心原则：

> LLM 负责理解语言，Control Plane 负责业务决策，Tool / Service 负责真实执行。

不能让 LLM 直接决定业务规则、完成条件或高风险写操作。

---

# 二、统一 Agent 路由地图

整个请求处理链统一理解为：

```text
User Message
    ↓
1. Interaction Route
    ↓
2. Semantic Router
    ↓
3. Canonical Business Goal
    ↓
4. Control Plane / Workflow Resolution
    ↓
5. Capability Planning
    ↓
6. Tool / Service Execution
    ↓
7. Decision Facts
    ↓
8. Policy / State Transition
    ↓
9. Outcome Evaluation
    ↓
10. Final Response
```

详细如下。

---

# 三、第 1 层：Interaction Route

先决定当前请求属于哪种处理范式：

```text
RAG
Agent
Plan-Execute
Safety / Human Handoff
```

例如：

```text
商品参数咨询
→ RAG

查退款状态
→ Agent

复杂多步骤售后
→ Agent / Plan-Execute

高风险或系统无法安全处理
→ Handoff
```

不要因为用户当前选择了商品就强行把所有请求改成 RAG。

商品上下文只能作为 entity/context。

例如：

```text
用户选中了 iPhone
然后说：我要退款
```

仍然应该走：

```text
Agent → refund.request
```

而不是被 selected_product_context 强制改成 RAG。

---

# 四、第 2 层：Semantic Router

Router 只负责理解用户语言。

Router 的职责：

```text
speech_act / request_mode
domain
operation
goal_modifier
entities
claims
ambiguity
multi-goal
```

Router 不负责：

```text
required_tools
business SOP
completion
permissions
write authorization
case status
```

推荐一次 LLM Router 调用输出完整结构，不要建立：

```text
Router1 LLM
→ Router2 LLM
→ Router3 LLM
```

避免多层 LLM 串行误差。

---

# 五、Router 首先需要判断“用户现在到底有没有新诉求”

当前第二批 benchmark 已暴露一个重要问题：

模型容易把：

```text
陈述事实
未来意向
结束语
已经处理完
```

误判成新的可执行退款任务。

因此 Router 需要有轻量 speech-act / request-mode 语义。

建议稳定枚举：

```text
ACTION_REQUEST
INFORMATION_QUERY
STATEMENT
ACKNOWLEDGEMENT
FUTURE_INTENTION
CLARIFICATION_NEEDED
```

例如：

```text
“我要退款”
→ ACTION_REQUEST

“退款现在什么状态”
→ INFORMATION_QUERY

“我已经申请退款了”
→ STATEMENT
如果没有进一步问题，不自动开始新流程

“好的，有需要再联系”
→ ACKNOWLEDGEMENT

“再等两天，不行我就退款”
→ FUTURE_INTENTION

“我不小心申请了退款”
→ CLARIFICATION_NEEDED
```

关键不变量：

> History 可以补充一个模糊当前意图，但不能覆盖一个明确的当前意图。

---

# 六、第 3 层：Canonical Business Goal

Router 输出稳定的：

```text
domain + canonical operation
```

例如：

```text
refund.status
refund.expected_arrival
refund.processing_time
refund.destination
refund.amount
refund.eligibility
refund.request
refund.cancel
refund.anomaly

return.refund_dependency

price_protection.refund_status
```

Routing taxonomy 和 Workflow taxonomy 不要求一一对应。

多个 Operation 可以共享基础查询 Workflow，但不能因此共享错误的 completion semantics。

例如：

```text
refund.status
refund.expected_arrival
refund.processing_time
```

可以共享：

```text
identify_order
query_refund
```

但不能共享同一个完成条件。

---

# 七、第 4 层：Control Plane

Control Plane 是 Agent 的业务中控层。

它回答：

> 用户的 Goal 已经明确以后，业务上应该怎么处理？

Control Plane 负责：

```text
Workflow Registry
Required Decision Facts
Capability Registry
Dependencies
Readiness
Completion Criteria
Action Policy
Confirmation Boundary
Case State
Unsupported / Unavailable handling
```

核心原则：

> LLM 识别业务目标，系统根据 SOP 推进业务流程。

不是：

> LLM 生成业务流程，系统照做。

---

# 八、Workflow 必须显式覆盖

任何业务 SupportRequest 必须属于三类之一：

## A. 有 Workflow

正常处理。

## B. 明确的 conversation/no-op

例如：

```text
ACKNOWLEDGEMENT
```

不进入业务执行。

## C. Unsupported

明确返回：

```text
workflow_unsupported
coverage_complete = false
```

禁止：

```text
resolve_workflow() is None
→ continue
→ plan=[]
→ coverage_complete=True
```

没有 Workflow 不等于执行成功。

---

# 九、clarify 和 acknowledgement 不得混为一类

这是当前需要继续修复的重点。

## acknowledgement

例如：

```text
好的，谢谢
有需要再联系
```

属于：

```text
NO_OP
```

可以结束当前交互。

## clarify

例如：

```text
我不小心申请退款了
```

无法确定用户是：

```text
想取消
想查询
还是只是陈述
```

因此应该：

```text
ASK_CLARIFICATION
→ AWAITING_CUSTOMER
```

不能：

```text
conversation-only
→ all([])
→ completion=True
```

必须避免 `all([]) == True` 导致 clarify 被误判完成。

---

# 十、多 Goal completion 必须覆盖所有 Goal

例如：

```text
refund.status
+
refund.destination
```

如果系统只获得：

```text
refund_status = PROCESSING
```

只能完成：

```text
refund.status
```

不能整个 Case completion=True。

规则：

> 所有需要业务处理的 request 都必须被明确解析并满足自己的 completion。

任何：

```text
unsupported request
missing workflow
unresolved goal
```

都必须阻止整体 completion。

---

# 十一、Goal-specific Completion

不同 Operation 必须拥有自己的完成语义。

## refund.status

所需：

```text
order_identified
refund_status
```

已知即可完成。

即使：

```text
refund_status = PROCESSING
```

用户问的是状态，因此 Goal 已回答。

---

## refund.expected_arrival

只知道：

```text
refund_status = PROCESSING
```

不能完成。

还需要：

```text
expected_arrival_time
```

如果真实系统无法提供 ETA：

```text
query_refund_expected_arrival
available=False
```

应该明确暴露 capability/data gap。

不能编造 ETA。

---

## refund.processing_time

需要：

```text
refund_processing_sla
```

或可靠处理阶段时间。

没有真实 SLA：

```text
available=False
```

不能把 status 冒充 processing time。

---

## refund.anomaly

需要：

```text
refund_status
refund_failure_reason / anomaly_reason
```

如果只能确认 FAILED 而不知道原因：

不能假装已经解释异常原因。

---

## refund.amount

需要：

```text
refund_amount
```

必要时还包括：

```text
refund_partial
promotion_allocation
fee_deduction
```

---

## refund.destination

需要：

```text
refund_destination
payment_method / original_payment_channel
```

不能仅凭常识承诺“原路返回”。

必须来自可信业务数据或规则。

---

# 十二、Capability Registry

每个 Workflow 应声明：

```text
required facts
capabilities
dependencies
availability
```

如果能力不存在：

```text
available=False
```

显式报告：

```text
capability_unavailable
```

不要为了 benchmark 建假 Tool。

当前允许明确缺口，例如：

```text
refund ETA
refund processing SLA
refund failure reason
refund destination
refund eligibility
refund cancel eligibility
return warehouse receipt
price protection status
```

只有真实系统能够提供以后才把能力设为 available=True。

---

# 十三、blocked 不等于 AWAITING_CUSTOMER

需要区分：

```text
谁能够解除当前阻塞？
```

建议引入轻量语义：

```text
next_actor = CUSTOMER
next_actor = SYSTEM
next_actor = STAFF
next_actor = NONE
```

例如：

```text
clarification_needed
→ CUSTOMER

confirmation_required
→ CUSTOMER

tool transient failure
→ SYSTEM

manual approval required
→ STAFF

capability_unavailable
→ NONE / STAFF

workflow_unsupported
→ NONE / STAFF
```

不能把所有：

```text
blocked
unresolved
```

统一落成：

```text
AWAITING_CUSTOMER
```

因为系统自己没有 ETA Tool，不是用户多说一句话就能解决。

---

# 十四、pending_command 只能用于真实写操作

当前需要继续修复：

普通：

```text
clarify
customer choice
capability unavailable
workflow unsupported
```

不能生成：

```text
PROPOSED_NOT_EXECUTED
```

pending_command 只应该存在于：

```text
真正的业务写操作
+
Control Plane 确认需要 confirmation / execution
```

例如：

```text
create_ticket
assign_ticket
update_ticket_priority
```

未来才需要：

```text
pending_command
```

普通只读和澄清：

```text
pending_command = {}
```

---

# 十五、Agent 代办能力的总体政策

当前产品原则：

> Agent 可以代办流程，但不能代办资金。

不能简单按照 read/write 一刀切，而按照业务风险分类。

推荐最小 ActionPolicy：

```text
READ
AUTO_LOW_RISK_WRITE
CONFIRM_LOW_RISK_WRITE
SELF_SERVICE_ONLY
HUMAN_ONLY
```

不需要建立复杂 Risk Engine。

一个枚举 + server-side allowlist 即可。

---

# 十六、允许自动执行的能力

## READ

例如：

```text
query_order
query_refund_status
query_payment_status
query_logistics
query_stock
query_after_sales
```

允许自动执行。

---

## AUTO_LOW_RISK_WRITE

可以由 Agent 代办，例如：

```text
create_support_ticket
generate_weekly_report
save_report
add_case_note
add_ticket_tag
assign_ticket_to_queue
```

仍然必须：

```text
权限校验
审计日志
幂等
服务端 allowlist
```

但不一定需要每次用户再次确认。

---

## CONFIRM_LOW_RISK_WRITE

例如：

```text
close_ticket
change_ticket_priority
change_nonfinancial_case_state
```

可以：

```text
Agent 提议
→ 用户确认
→ deterministic executor
```

---

# 十七、资金相关操作禁止 Agent 代办

包括：

```text
退款
扣款
转账
充值
提现
修改支付账户
改变真实资金归属
```

统一：

```text
SELF_SERVICE_ONLY
```

或者：

```text
HUMAN_ONLY
```

Agent 不能直接执行。

即使项目中已经存在：

```text
request_customer_refund()
confirm_customer_refund()
```

也不要把它注册成 LLM Tool，也不要让 Command Executor 自动替客户调用。

---

# 十八、refund.request 当前最终产品决策

`refund.request` 不做 Agent 自动退款。

标准 Workflow：

```text
用户：我要退款
↓
Router
refund.request
↓
Control Plane
↓
identify order
query existing refund
check eligibility
↓
eligible?
↓
是
↓
generate official self-service refund entry
↓
Agent 返回安全退款入口
↓
用户在官方业务页面自行确认和提交
```

Agent 的职责：

```text
理解
查询
判断
解释
生成/获取官方入口
引导
```

业务页面职责：

```text
强身份认证
最终确认
真实退款写操作
事务
资金系统
失败恢复
```

---

# 十九、refund.request 的 Outcome 语义

如果 Agent 给出了官方退款入口：

不能写成：

```text
REFUND_CREATED
REFUND_SUCCESS
```

正确应该是：

```text
SELF_SERVICE_HANDOFF
```

或者：

```text
REFUND_ENTRY_DELIVERED
```

表示：

> Agent 已经成功把用户送到退款申请入口。

不代表退款已经提交成功。

用户之后问：

```text
我刚申请了，成功了吗？
```

再通过：

```text
query_refund_status
```

读取真实业务系统。

---

# 二十、退款入口必须由后端产生

禁止 LLM 自己拼 URL。

正确：

```text
Control Plane
↓
generate_refund_entry(user_id, order_no)
↓
后端生成可信入口
↓
Agent 返回
```

最好具备：

```text
user binding
order binding
short-lived token
expiration
authorization
```

如果项目当前没有真实退款页面：

不要伪造 URL。

可以暂时：

```text
capability available=False
```

或者创建人工工单。

---

# 二十一、refund.eligibility

下一步应该优先补一个可信的只读：

```text
check_refund_eligibility
```

当前真实 refund service 已经存在退款资格规则。

不要复制两套互相漂移的业务规则。

建议抽取可复用业务规则：

```text
RefundEligibilityService
```

提供：

```text
check(...)
→ eligible
→ reason
```

Agent 查询用它。

真实退款业务页面/真实 write service 执行退款时仍然必须再次做权威校验。

即：

```text
read-time eligibility
= Agent 决策辅助

write-time eligibility
= 真正安全校验
```

不能因为之前查过 eligible 就跳过真实写入时的校验。

---

# 二十二、refund.cancel

当前项目没有可靠真实退款取消业务 service 时：

不要假实现。

Workflow 可以存在，但：

```text
cancel capability available=False
```

根据产品设计：

```text
SELF_SERVICE_ONLY
```

或：

```text
HUMAN_ONLY
```

不要让 Agent 宣称已经取消。

---

# 二十三、Command Executor 的最终定位

Command Executor 不用于资金操作。

它主要负责：

> 安全执行 Agent 被允许代办的低风险业务写操作。

未来适合：

```text
create_ticket
assign_ticket
add_case_note
update_ticket_priority
save_report
close_ticket
```

结构：

```text
LLM
↓
Goal
↓
Control Plane
↓
Action Policy
↓
pending_command
↓
Command Executor
↓
low-risk Business Service
```

Command Executor 必须：

```text
server-side allowlist
authorization
case ownership
expected case status
confirmation if required
version check
idempotency
audit
result normalization
```

资金操作不要加入 allowlist。

---

# 二十四、Tool / Command Executor 的关系

Tool 是：

> 系统提供的业务能力。

Command Executor 是：

> 高于普通 Tool 的受控写操作入口。

只读：

```text
LLM
→ approved read Tool
```

低风险业务写：

```text
LLM
→ Goal
→ Control Plane
→ Command Executor
→ Business Service
```

高风险资金写：

```text
LLM
→ Goal
→ Control Plane
→ SELF_SERVICE / HUMAN
```

绝不：

```text
LLM
→ refund write Tool
```

---

# 二十五、SupportCase 状态原则

继续保留：

```text
ACTIVE
AWAITING_CUSTOMER
AWAITING_STAFF
COMPLETED
FAILED
CANCELLED
```

语义：

## ACTIVE

系统还有确定性步骤可以执行。

## AWAITING_CUSTOMER

只有客户才能提供下一信息，例如：

```text
clarification
selection
confirmation
```

## AWAITING_STAFF

需要人工：

```text
manual approval
cross-system verification
unsupported high-risk operation
```

## COMPLETED

当前用户 Goal 已经被解决。

注意：

```text
refund business status = PROCESSING
```

不代表 Case 不能 COMPLETED。

如果用户只问：

```text
退款现在是什么状态？
```

Agent 已准确回答“PROCESSING”，那这个客服 Goal 可以 COMPLETED。

## FAILED

真实业务执行失败且无安全恢复路径。

---

# 二十六、Decision Facts

ToolResult 必须统一：

```text
Tool
↓
Raw ToolResult
↓
DecisionFactNormalizer
↓
verified facts
↓
Control Plane / Evaluator
```

LLM 不应该直接把自己的猜测写进 verified_facts。

继续保证：

```text
False 是已知事实
```

例如：

```text
refund_eligibility=False
```

不是 missing fact。

---

# 二十七、下一阶段实施顺序

不要继续调当前 100 条 benchmark 的 Router keyword。

接下来按照下面顺序执行。

## Phase A：收尾当前 P0

修复：

```text
1. refund.clarify 不得 completion=True
2. blocked/capability_unavailable 不得自动 AWAITING_CUSTOMER
3. 非写操作不得生成 pending_command
```

新增测试。

---

## Phase B：Refund Read Capability

实现可信：

```text
check_refund_eligibility
```

优先复用/抽取现有 refund business rules。

不要复制规则。

---

## Phase C：Self-Service Refund Entry

如果业务系统有真实退款页面：

实现：

```text
generate_refund_entry
```

它是：

```text
SELF_SERVICE capability
```

不是资金 write Tool。

然后：

```text
refund.request
→ eligibility
→ refund entry
→ SELF_SERVICE_HANDOFF
```

---

## Phase D：低风险 Command Executor

第一批只支持低风险动作。

建议从：

```text
create_support_ticket
```

开始。

以后可以扩：

```text
assign_ticket
add_case_note
generate/save weekly report
update nonfinancial case state
```

不要支持 refund money movement。

---

# 二十八、Phase 2 Benchmark

Control Plane 和基础 capability 稳定以后，才进入真实 Execution Benchmark。

先跑：

```text
Oracle Route
```

即直接使用 Gold canonical Goal：

```text
Gold domain/operation
→ Control Plane
→ Tool
→ Decision Facts
→ Policy
→ State
→ Outcome
```

测：

```text
Oracle-route Goal Resolution Rate
```

这回答：

> 假设 Router 100% 正确，执行系统到底能办成多少。

然后跑：

```text
Real Router
→ 相同 Control Plane / Tool / fixture
```

测：

```text
End-to-end Goal Resolution Rate
```

比较：

```text
Oracle 90%
Real 60%
→ Router 是主要损失来源

Oracle 60%
Real 55%
→ Control Plane / Capability / Tool 才是主要瓶颈
```

不要再根据猜测判断瓶颈。

---

# 二十九、Benchmark 使用原则

第一批 100：

```text
development benchmark
```

第二批 100：

```text
holdout / generalization benchmark
```

当前第二批结果显示：

```text
Goal 泛化下降
Fact / Capability / Plan 相对稳定
```

因此当前主要语义问题之一是：

```text
action request
vs
statement
vs
future intention
vs
acknowledgement
vs
clarification
```

不要继续通过：

```text
每错一条 → 加一个退款关键词
```

来提高分数。

这会造成 engineering overfitting。

---

# 三十、禁止事项

后续开发不要做：

```text
1. 不针对 benchmark case 加硬编码关键词。
2. 不让 LLM 决定 required_tools。
3. 不让 LLM 决定业务 completion。
4. 不让 LLM 直接执行退款、转账等资金操作。
5. 不建立复杂 BPM。
6. 不建立通用 DSL。
7. 不建立大型 Rule Engine。
8. 不伪造企业系统、SLA、仓库数据或 ETA。
9. 不复制已有退款资格规则形成两套逻辑。
10. 不为了 benchmark 伪造 Tool。
11. 不把所有 blocked 都转成 AWAITING_CUSTOMER。
12. 不把所有写操作全部禁止，低风险业务写应允许 Agent 代办。
```

---

# 三十一、当前最终架构原则

统一记住：

```text
LLM / Router
负责：
用户想干什么？

Control Plane
负责：
这件事业务上应该怎么处理？

Capability / Tool / Service
负责：
真实系统能做什么？

Action Policy
负责：
Agent 是否允许代办？

Command Executor
负责：
安全执行允许的低风险写操作。

Outcome Evaluator
负责：
用户 Goal 最终真的解决了吗？
```

最终产品边界：

> Agent 可以代办信息型、流程型、协作型业务；不能代办真实资金动作。

例如：

```text
查退款状态         → 自动
查物流             → 自动
生成周报           → 自动
保存周报           → 自动
创建工单           → 自动
工单加备注         → 自动
分派工单           → 自动
关闭工单           → 可确认后执行
退款               → Self-Service / Human
转账               → 禁止
扣款               → 禁止
修改支付账户       → 禁止
```

---

# 三十二、本轮 Coding Agent 的具体任务

现在只执行以下任务：

```text
1. 修 refund.clarify completion/readiness 语义。
2. 修 blocked / capability_unavailable / workflow_unsupported 的 Case 状态映射。
3. 修非写操作误生成 pending_command。
4. 为这些场景增加测试。
5. 审查现有 refund eligibility 规则，设计并实现可信 read-only check_refund_eligibility。
6. 优先复用现有真实退款业务规则，不复制规则。
7. 不实现退款 Command Executor。
8. 不让 Agent 自动执行资金退款。
9. 不改 IntentRouter benchmark-specific 规则。
10. 不开始大规模架构重构。
```

完成后报告：

```text
Changed
Tests
State transition behavior
Refund eligibility source
Remaining capability gaps
Any architectural risks found
```

并运行：

```text
pytest relevant tests
py_compile
git diff --check
```

如果完整 pytest 因测试数据库 migration 环境问题无法完成，必须明确写：

```text
FULL REGRESSION BLOCKED
```

不能写成通过。

完成上述内容后停止，不继续扩展下一阶段。
