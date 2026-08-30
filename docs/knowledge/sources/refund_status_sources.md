# refund_status 来源审计

审计日期：2026-08-28。运行时文档：`data/knowledge/refund_status.md`。

## 客户可见状态

Sources:

- `src/service/checkout_refund_service.py`：`customer_visible_refund_status`、`refresh_customer_refund_status`。
- `src/agent/tools/query_refund_status.py`：客户范围退款记录查询。
- `src/api/checkout.py`：客户订单和退款响应投影。

结论：

- `PENDING_FINANCE_APPROVAL` 对客户投影为 `PENDING_MERCHANT_REVIEW`。
- `SUCCEEDED`/`SUCCESS`/`REFUND_SUCCESS` 对客户投影为 `COMPLETED`；`REJECTED`/`REFUND_FAIL` 对客户投影为 `FAILED`。
- `PENDING_CONFIRMATION` 与 `PROCESSING` 按原值保留；没有记录时工具返回 `refund_lookup: no_record`。

## 查询与刷新边界

Sources:

- `src/agent/tools/query_refund_status.py`。
- `src/service/checkout_refund_service.py`：`refresh_customer_refund_status`。
- `src/api/checkout.py`：`POST /refunds/{refund_id}/refresh`。

结论：

- Agent 的退款状态工具只读本地记录，不会刷新支付宝、创建退款或修改金额。
- 客户订单页可刷新处于 `PROCESSING` 的退款；该服务只查询支付宝结果，不会再次提交退款。

## 明确不可查询的事实

Sources:

- `src/agent/support_control.py`：`query_refund_expected_arrival`、`query_refund_destination`、
  `query_refund_processing_sla`、`query_refund_failure_reason` 均为 `available=False`。

结论：不写退款 ETA、去向、处理时效或失败原因。
