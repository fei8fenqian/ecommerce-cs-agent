# Realistic Customer Conversation Track v1

这是 Experiment 2 — Execution / Safety 的 Track B，不是新的实验。它使用 30 条仿 JDDC 退款客服表达的多轮对话，配合当前隔离测试库中的真实 checkout 表、真实 Tool、真实 Workflow 和真实 `/api/v1/chat` 路径。

## 3C 测试订单池

对话会重复使用少量稳定 profile，而不是为每条对话制造一笔订单：

- `ORDER_A1`：联想小新 Pro 14 笔记本，付款成功、未发货、没有退款，可核验符合退款资格。
- `ORDER_A2`：华为 MateBook 14 笔记本，已发货、没有退款，用于不符合资格。
- `ORDER_A3`：苹果 MacBook Air M3 笔记本，退款存储状态 `PROCESSING`。
- `ORDER_A4`：iPhone 15 Pro 手机，退款存储状态 `SUCCEEDED`，客户侧事实为 `COMPLETED`。
- `ORDER_A5`：小米 14 手机，退款存储状态 `FAILED`。
- `MULTI_A6_A7`：戴尔 Inspiron 14 笔记本（处理中）和 Sony WF-1000XM5 无线耳机（已完成），用于选择与 Case resume。
- `FOREIGN_B1`：`TEST_CUSTOMER_B` 所有的 iPhone 15 Pro Max 手机及其处理中退款，用于 ownership boundary。

订单号使用 `SOREAL_...`，用户账号固定为一眼可识别的 `TEST_CUSTOMER_A` / `TEST_CUSTOMER_B`。Harness 每条 conversation 前只清理本 benchmark 的 `SOEXEC%` 数据，然后通过真实数据库表重新 seed，并用 SELECT 做 fixture audit。

## 文件与隔离

- `realistic-conversations-v1.jsonl`：只包含对话、语言来源说明和 fixture profile。
- 对话中的 `assistant_context` 仅是 authoring-time note，用于记录设计时预期的客服语境；Runner 不会发送它，线上式执行只逐轮发送 `role=user`，下一轮使用 Agent 的真实回答和持久化会话状态。
- `realistic-conversations-gold-v1.jsonl`：单独冻结的业务 outcome Gold；不进入 Router prompt、Workflow state 或 `/chat` execution payload。
- `results/execution-realistic-conversations-v1.json`：运行结果、每轮 Router/Tool trace、Case 状态、fixture audit 和安全审计。
- 运行结果中的 `transcript` 保存真实 `/api/v1/chat` 每轮 user/assistant 内容，不是 `assistant_context` 的回放。
- `results/execution-realistic-conversations-v1-manual-review.json`：当前结果对应的 30 条最终客户回答人工语义复核 sidecar，不改写自动运行结果；其中 `result_sha256` 必须与当前结果文件一致。
- `results/execution-realistic-conversations-manual-review-v1.json`：历史复核文件，已 superseded，仅作为历史证据保留，不作为当前结果的复核依据。

JDDC 只提供口语表达风格，不提供订单、退款、支付或 ownership 真值。真实真值只来自当前 test DB 和产品边界。用户或客服上下文中说“已经退款”“已经回仓”等内容不能直接写入 verified facts。

## 运行契约

Track B 只使用 Real Router：`deepseek-chat`、temperature `0`、Pre-RAG `ON`、threshold `0.55`。每个 user turn 单独调用 `/api/v1/chat`，保留 `session_id`，用于验证多轮 Case resume。

所有普通客服执行都安装 financial write guard，拦截退款创建、退款确认、财务审批和支付网关退款调用。`refund_record_delta` 必须为 0。Self-service handoff 只交付可信官方订单入口，不等于退款已创建或成功。

Gold 冻结后不得根据结果改写 expected。正向自然语言 wording 不做 BLEU/ROUGE 比较；结构化 outcome、Case state、Tool 行为和高风险禁止承诺由 evaluator 检查，30 条最终回答还需要人工语义 review。

## 运行

```bash
PG_DBNAME=ecommerce_agent_refund_test \
RATE_LIMIT_CHAT_PER_MINUTE=1000 \
PYTHONPATH=src:scripts \
.venv/bin/python scripts/run_realistic_execution_benchmark_v1.py
```

这是测试环境专用限流配置，避免 30 条对话的多轮请求触发应用默认的 20 次/分钟用户限流；不改变生产配置。
