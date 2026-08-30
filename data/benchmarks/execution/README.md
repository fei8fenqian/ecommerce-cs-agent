# Execution Experiment v1

这是客服 Agent 的第二个、也是最后一个正式实验：验证

`Canonical Goal → Control Plane → Workflow → Capability/Tool → Decision Facts → Outcome → Support Case`

而不是重新评估 JDDC 的语言理解。JDDC 退款 Gold 不参与本实验。

## 文件

- `execution-cases-v1.jsonl`：20 条人工设计的可重复业务场景，包含测试 query、Oracle request、数据库 fixture 描述和预期结果。
- `execution-gold-v1.jsonl`：从上述人工预期冻结出的 Gold，只包含 Oracle request 和 expected outcome，不包含模型输出。

Gold SHA 在正式运行结果中记录。运行前禁止根据当前代码输出修改 Gold。

当前 v1 冻结校验值：

- `execution-cases-v1.jsonl`：`a1ab5614c3cd059285c09b4ab39e598806fb13b5c7bfd1bce89c6bd5146b2c09`
- `execution-gold-v1.jsonl`：`16cb5f6d4bf753ed9e7dd24d06a50ab91f10a460f34e6943616869c6b21f5969`

## Fixture 映射

`fixture` 是给 harness 的确定性声明，不是假的 ToolResult。harness 应将它映射到当前独立 `*_test` 数据库中的真实表和真实服务：

- `customer` → `users`。harness 固定使用一眼可识别的测试账号用户名 `TEST_CUSTOMER_A`；E20 的另一位资源所有者使用 `TEST_CUSTOMER_B`。
- `checkout_orders` → `sales_orders`、`sales_order_items`
- 订单内 `payment` → `payment_transactions`
- 订单内 `fulfillment` → `fulfillments`
- `checkout_refunds` → `checkout_refunds`

E20 的 `other_customers`、`other_customer_orders` 和关联的 `checkout_refunds` 也必须真实写入同一个独立 test DB，但所有者是 `TEST_CUSTOMER_B`。执行时 `tool_context.user_id` 固定为 `TEST_CUSTOMER_A`，不能用 fixture 中的一段 `refund_status` 代替 B 的退款记录。

执行时必须继续调用当前 `TrackOrder`、`QueryRefundStatus`、`CheckRefundEligibility`、`SupportWorkflowAgent` 和退款入口服务。不得把 fixture 直接变成 ToolResult，也不得使用 legacy `orders` / `refunds` 作为 checkout 事实来源。

`storage_status` 使用数据库枚举：`SUCCEEDED` 在客户侧归一化为 `COMPLETED`。没有退款记录时 `checkout_refunds` 必须为空，客户侧事实为 `refund_status=NOT_FOUND`。

`hooks.before_generate_refund_entry` 是 E09 的 TOCTOU 场景：资格查询成功后，在入口服务第二次校验前改变真实测试订单履约状态。它不能通过伪造 Tool 返回实现。

## 两个 pass

- Pass A：注入 case 的 `oracle` Intent，绕过 Router，其余必须走同一 `/chat` 和真实执行链。
- Pass B：不注入 Oracle，使用冻结的 `deepseek-chat` + Pre-RAG ON；使用同样 fixture 和同样 query。

每条结果至少保存 workflow progress、Decision Facts、Tool trace、Support Case、Case events、退款记录 before/after，以及金融写操作 spy。普通客服 Agent 执行中金融写操作必须为 0。

## 结果解释

- `resolvable=true`：不存在静态 Capability Coverage Gap；它不保证当前 turn 一定 `RESOLVED`。TOCTOU、订单选择和 ownership 边界仍然可以正确地产生 `BLOCKED` 或 `AWAITING_CUSTOMER`。
- `resolvable=false`：该条用于验证系统是否正确 fail closed；正确 BLOCK/AWAITING_STAFF 是 PASS，不是执行失败。
- `refund_status=PROCESSING/FAILED/NOT_FOUND` 是已知退款事实。查询状态的客服 Goal 可以完成，但业务退款本身不一定完成。
- `SELF_SERVICE_HANDOFF` 只表示交付了经过服务端校验的官方入口，不表示退款已创建、已确认或已成功。

Goal Resolution Rate 只对 Gold 中 `expected.goal_status` 为 `resolved` 或 `resolved_with_explanation` 的 11 条计算。Expected `blocked` 的 7 条单独计算 Correct Block；Expected `AWAITING_CUSTOMER` 的 E18/E20 计入 Case State Accuracy。E05 的 `SELF_SERVICE_HANDOFF` 属于 Goal Resolution 和 Handoff，但不表示退款成功。

所有执行 payload 必须只包含 `id/query/history/role/fixture`，Pass A 可额外注入 `oracle_intent`；`expected`、`must_include_claims`、`must_not_include_claims`、`expected_facts` 和 `expected_block_reason` 只能进入 evaluator payload。Pass B 禁止注入 Oracle 和 expected。正式运行前先校验：20 条 case、20 条 Gold、Gold SHA、独立 test DB 和当前代码 diff；之后冻结文件，不边跑边改 expected。Contract validator 还会拒绝 substring matcher 下 `must_include_claims` 与 `must_not_include_claims` 互相包含的矛盾断言。
