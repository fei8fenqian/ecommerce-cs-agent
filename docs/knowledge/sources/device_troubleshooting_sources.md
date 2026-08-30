# device_troubleshooting 来源审计

## 结论与来源

- 无危险迹象时的低风险初查限定为检查电源/充电器/指示灯、拔外设和长按电源键 10–15 秒。
  - Sources: `src/service/customer_support_policy.py:warranty_troubleshooting_answer`
- 进水、摔碰、充电异常、黑屏和电池危险迹象均在客服策略的设备症状范围内。
  - Sources: `src/service/customer_support_policy.py:_DEVICE_SYMPTOM_MARKERS`, `_DEVICE_DANGER_MARKERS`
- 未恢复、明确报修或危险迹象按确定性策略升级。
  - Sources: `src/service/customer_support_policy.py:decide_customer_support_action`

## 未写入

- 未写拆机、烘烤、刷机、BIOS、恢复出厂或任何品牌按键组合。
