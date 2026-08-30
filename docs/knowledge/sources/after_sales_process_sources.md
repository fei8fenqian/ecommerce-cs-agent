# after_sales_process 来源审计

## 结论与来源

- `check_after_sales` 返回本人售后申请摘要和人工工单摘要，明确两者不是同一状态。
  - Sources: `src/agent/tools/check_after_sales.py`
- 查询工具不创建、认领、修改或关闭申请；依赖异常不应冒充无记录。
  - Sources: `src/agent/tools/check_after_sales.py`
- 设备危险、受控排查后仍未解决、明确报修/人工和退款异常是受控升级条件。
  - Sources: `src/service/customer_support_policy.py:decide_customer_support_action`

## 未写入

- 未把任一工单状态解释成退款完成。
