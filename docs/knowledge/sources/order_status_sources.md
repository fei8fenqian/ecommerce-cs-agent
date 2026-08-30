# order_status 来源审计

## 结论与来源

- 订单追踪工具将订单、退款和物流字段映射为未发货、运输中、已签收、不适用或未知。
  - Sources: `src/agent/tools/track_order.py:_with_delivery_facts`
- 退款成功或处理中优先覆盖普通履约解释。
  - Sources: `src/store/order_store.py:_checkout_order_to_tool_order`
- 预计发货时间能力不可用。
  - Sources: `src/agent/support_control.py:CAPABILITY_REGISTRY`

## 未写入

- 未写快递轨迹、配送时效或预计送达时间。
