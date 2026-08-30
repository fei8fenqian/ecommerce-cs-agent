# warranty_process 来源审计

## 结论与来源

- 当前客服策略要求收集型号、症状、购买线索与风险背景，先做低风险初查再决定报修升级。
  - Sources: `src/service/customer_support_policy.py:warranty_troubleshooting_answer`, `decide_customer_support_action`
- 当前能力注册表没有保修期限、序列号、维修报价或检测结论查询。
  - Sources: `src/agent/support_control.py:CAPABILITY_REGISTRY`

## 未写入

- 未写统一保修期、免费维修、换新、维修时效或费用。
