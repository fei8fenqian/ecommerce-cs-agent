# order_lookup 来源审计

## 结论与来源

- 客户订单 API 和 Store 都按当前 `customer_user_id` 限制资源范围，历史未归属订单不返回。
  - Sources: `src/api/orders.py`, `src/store/order_store.py:find_orders`
- 无订单号时工具可以返回本人多个候选并要求选择。
  - Sources: `src/agent/tools/track_order.py`
- SO 订单和兼容历史订单查询的边界由 Store 定义。
  - Sources: `src/store/order_store.py`

## 未写入

- 未把手机号当作跨客户授权凭据。
