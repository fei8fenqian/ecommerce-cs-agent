# agent_boundaries 来源审计

审计日期：2026-08-28。运行时文档：`data/knowledge/agent_boundaries.md`。

## 客户聊天可读取的事实

Sources:

- `src/main.py`：已注册工具。
- `src/api/chat.py`：`_CUSTOMER_CHAT_READ_TOOLS` 与客户上下文。
- `src/agent/tools_registry.py`：服务端 `ToolContext` 对可见工具和阻止工具的限制。

结论：客户聊天的动态订单、库存、支付、退款与售后事实都必须通过受控工具读取；模型文本没有直接资源权限。

## 退款写边界

Sources:

- `src/agent/support_control.py`：`refund.request`、Capability Registry、完成条件。
- `src/agent/engines/support_workflow.py`：服务端生成退款入口与 self-service handoff。
- `src/service/checkout_refund_service.py` 与 `src/api/checkout.py`：退款申请、确认和状态刷新。

结论：Agent 的退款完成态是交付经过服务端校验的订单页入口；退款资金动作仅由客户订单页和确定性服务推进。

## 能力缺口与人工升级

Sources:

- `src/agent/support_control.py`：Capability Registry 中 `available=False` 的能力。
- `src/service/customer_support_policy.py`：退款引导、客户再次明确人工、设备危险信号的处理边界。

结论：退款 ETA、去向、失败原因、退货/仓储、换货和价保等能力当前不可核验；危险设备信号可以进入人工升级，
一般退款诉求先自助引导，不能由模型直接建单。
