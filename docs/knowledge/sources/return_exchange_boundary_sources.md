# return_exchange_boundary 来源审计

## 结论与来源

- 退货状态、退货物流、仓库签收和换货资格均注册为 `available=False`。
  - Sources: `src/agent/support_control.py:CAPABILITY_REGISTRY`
- 退款状态和退货事实在控制面是不同的事实需求。
  - Sources: `src/agent/support_control.py:_return_refund_dependency_complete`

## 未写入

- 未写取件、仓库验收、换货库存或换货改退款的自动结果。
