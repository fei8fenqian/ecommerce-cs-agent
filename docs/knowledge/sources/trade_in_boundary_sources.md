# trade_in_boundary 来源审计

## 结论与来源

- 当前 Tool 注册和 Capability Registry 中没有估价、回收、补贴核验或以旧换新订单能力。
  - Sources: `src/main.py` Tool 注册, `src/agent/support_control.py:CAPABILITY_REGISTRY`
- 日志脱敏范围覆盖密码、令牌、支付签名、手机号、邮箱、地址和证据字段。
  - Sources: `src/log_config.py:_SENSITIVE_KEY_NAMES`

## 未写入

- 未写支持品牌、估价、补贴、回收时效、地区或活动资格。
