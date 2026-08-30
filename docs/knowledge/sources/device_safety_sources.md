# device_safety 来源审计

## 结论与来源

- 冒烟、起火、烧焦、电池鼓包和漏电会触发 `DEVICE_SAFETY_RISK` 人工升级。
  - Sources: `src/service/customer_support_policy.py:_DEVICE_DANGER_MARKERS`, `decide_customer_support_action`
- 初步分诊需要设备/型号、症状、时间、摔碰、进水和异常发热事实。
  - Sources: `src/service/customer_support_policy.py:warranty_troubleshooting_answer`
- 当前没有设备诊断、维修报价或保修查询 Tool。
  - Sources: `src/main.py` Tool 注册表, `src/agent/support_control.py:CAPABILITY_REGISTRY`

## 未写入

- 未写品牌专属维修步骤或硬件故障判断。
