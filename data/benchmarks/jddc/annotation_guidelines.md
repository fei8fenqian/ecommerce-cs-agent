# JDDC 客服 Intent Gold 标注规范

本规范标注的是用户当前的语义目标，不标注模型预测、工具调用或业务事实。

## 核心字段

- `speech_act`：当前说话行为。
- `expected_requests`：当前需要系统理解的 canonical 业务目标；没有明确业务目标时为空数组。
- `primary_goal`：首个 request 的 `domain.operation`；无 request 时为 `null`。
- `expected_workflow`：当前 `resolve_workflow()` 的实际 Workflow key；目标合法但暂无 Workflow 时为 `null`。
- `needs_clarification`：仅当用户目标本身无法安全确定时为 `true`。

## Speech Act

- `ACTION_REQUEST`：要求现在执行或推进动作，例如“帮我申请退款”。
- `INFORMATION_QUERY`：询问状态、资格、流程、原因或处理方案。
- `STATEMENT`：只提供已发生事实，没有当前待解决问题，例如“我昨天已经申请退款了”。
- `ACKNOWLEDGEMENT`：复述、接受或确认客服刚说明的内容，例如“好的”“到仓后才能处理退款”。
- `FUTURE_INTENTION`：条件性的未来打算，例如“再等两天，不行我就退款”。
- `CLARIFICATION_NEEDED`：当前目标不足以判断，例如只说“退款”。

## 问题报告也是 INFORMATION_QUERY

客服处理语境中，用户报告仍未解决的失败、被拒、无法操作、没有入口、未到账、状态异常或反复处理，即使没有问号，也可构成隐含业务查询。

例如：

- “退款也退不了” → `INFORMATION_QUERY` + `refund.anomaly`
- “商家一直不退款” → `INFORMATION_QUERY` + `refund.anomaly`
- “昨晚退款没到账，但另一笔马上到账” → `INFORMATION_QUERY` + `refund.anomaly`

与纯陈述区分：

- “我昨天已经申请退款了” → `STATEMENT`
- “退款已经到账了” → `STATEMENT`

关键不在于是否有问号，而在于用户是否表达了一个当前仍待解决的问题。

## 退款语义边界

- `refund.procedure`：问如何申请、在哪里申请、一般流程；例如“怎么申请退款？”
- `refund.request`：当前目标是成功发起退款。既包括“帮我申请退款”，也包括“我没有退款申请入口，怎么办，我现在要退款”。它可以是 `ACTION_REQUEST` 或 `INFORMATION_QUERY`。
- `refund.delivery_after_refund`：仅限退款与配送、签收、拒收的交叉状态。普通电话、客服联系或营销通知，不因发生在退款后而归入该 Goal。
- `return.refund_dependency`：退货、拒收、回仓等节点之后，退款是否或何时触发/推进。

不要从 speech act 反推 goal；也不要因为句子出现“退款”就自动创建 request。History 仅用于消歧当前语句，不能覆盖当前已经明确的目标。
