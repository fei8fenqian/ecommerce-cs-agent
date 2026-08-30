# customer_data_boundary 来源审计

## 结论与来源

- 订单、支付、退款和售后工具均以认证的 `ToolContext` 和客户归属为边界。
  - Sources: `src/agent/tools/track_order.py`, `check_payment_status.py`, `query_refund_status.py`, `check_after_sales.py`
- 日志会对密码、令牌、支付签名、手机号、邮箱、地址和多类内容字段脱敏。
  - Sources: `src/log_config.py`
- 客户回答不得输出精确库存、仓库、内部队列、数据库、工具调用或内部账号。
  - Sources: `src/agent/engines/loop.py:_CUSTOMER_PROMPT_APPEND`

## 未写入

- 未把日志脱敏描述为客户可以提交敏感数据的授权。
