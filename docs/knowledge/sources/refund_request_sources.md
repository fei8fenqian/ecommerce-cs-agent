# refund_request 来源审计

审计日期：2026-08-28。运行时文档：`data/knowledge/refund_request.md`。

## 当前退款资格

Sources:

- `src/store/checkout_refund_store.py`：`_REFUND_ELIGIBILITY_QUERY`、`get_customer_refund_eligibility`、`create_customer_refund_request`。
- `src/agent/tools/check_refund_eligibility.py`：客户范围只读工具的返回结构。

结论：

- 资格只针对当前客户、应用自有 checkout 订单；查询要求订单 `PAID`、支付 `SUCCEEDED`、CNY、履约
  `PENDING_FULFILLMENT`、支付成功未超过 7 天，且支付金额与订单金额一致且为正。
- `check_refund_eligibility` 当前只返回 `refund_eligibility: true/false`，没有 `reason_code`。运行时文档不得
  推断“不通过”的具体原因。

## 自助入口与资金边界

Sources:

- `src/service/checkout_refund_service.py`：`generate_customer_refund_entry`、`request_customer_refund`、`confirm_customer_refund`。
- `src/agent/support_control.py`：`refund.request` 工作流与 `generate_refund_entry` 能力。
- `src/agent/engines/support_workflow.py`：`_read_facts`、`_append_self_service_handoff`。
- `src/api/checkout.py`：客户申请与确认退款接口。

结论：

- Agent 只在已核验资格后由服务端交付订单页入口；它不创建或确认退款。
- 客户在订单页提交申请后，确定性服务创建待确认退款；客户明确确认后才提交支付宝沙箱退款。
- 金额由服务端已核验支付交易决定，客户请求体没有金额字段。

## 尚未支持的承诺

Sources:

- `src/agent/support_control.py`：Capability Registry。

结论：退款资格没有可向客户解释的 `reason_code`；不能把内部条件逐项猜成某一订单失败原因。
