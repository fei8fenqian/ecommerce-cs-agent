# refund_limitations 来源审计

## 结论与来源

- 退款 ETA、去向、处理 SLA、失败原因、取消资格均注册为 `available=False`。
  - Sources: `src/agent/support_control.py:CAPABILITY_REGISTRY`
- 退款取消属于资金状态变更，当前控制面没有可信客户自助能力。
  - Sources: `src/agent/support_control.py:_refund_cancel_complete`

## 未写入

- 未写到账天数、原路退回渠道、失败原因或可取消结论。
