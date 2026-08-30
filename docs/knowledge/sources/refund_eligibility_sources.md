# refund_eligibility 来源审计

## 结论与来源

- 首版资格由订单归属、成功 CNY 支付、待履约、近七日支付和全额金额等 SQL 条件共同核验。
  - Sources: `src/store/checkout_refund_store.py:get_customer_refund_eligibility`
- 资格工具只返回布尔结论，不返回 reason code。
  - Sources: `src/agent/tools/check_refund_eligibility.py`
- Agent 只交付服务端再次核验后的订单页入口，不创建或确认退款。
  - Sources: `src/service/checkout_refund_service.py:generate_customer_refund_entry`, `src/agent/support_control.py`

## 未写入

- 未解释具体不符合资格原因。
