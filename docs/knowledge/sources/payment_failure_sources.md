# payment_failure 来源审计

## 结论与来源

- 浏览器回跳不作为支付成功依据；异步通知需要验签，支付状态可按本人订单查询。
  - Sources: `src/service/checkout_service.py:process_alipay_callback`, `refresh_customer_payment_status`
- 支付查询只核验结果，不能创建支付、退款或改金额。
  - Sources: `src/agent/tools/check_payment_status.py`
- 支付/订单争议和明确人工诉求属于确定性人工升级范围。
  - Sources: `src/service/customer_support_policy.py`, `src/service/ticket_escalation.py`

## 未写入

- 未写任何网关错误的具体原因或客户款项处理结果。
