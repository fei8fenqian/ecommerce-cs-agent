# payment_status 来源审计

审计日期：2026-08-28。运行时文档：`data/knowledge/payment_status.md`。

## 当前支付接入

Sources:

- `src/config.py`：支付宝沙箱配置项。
- `src/service/checkout_service.py`：`AlipaySandboxClient`、checkout 创建、支付查询与异步回调处理。
- `src/api/payments.py`：支付宝沙箱回调入口。

结论：当前项目代码接入支付宝沙箱 checkout。没有发现微信支付、云闪付、银行卡快捷支付、分期或对公转账的
支付适配器、API 或客户工具，因此运行时文档不承诺这些能力。

## 支付状态判定

Sources:

- `src/service/checkout_service.py`：`refresh_customer_payment_status`、`process_alipay_callback`。
- `src/agent/tools/check_payment_status.py`。
- `src/api/checkout.py`：`POST /orders/{order_no}/refresh-payment`。

结论：

- 支付状态查询限定当前客户自己的 `SO` 商城订单，并验证商户交易号、金额与本地订单匹配。
- 浏览器回跳不是支付成功依据；异步回调需要验签，并核验应用、商户和金额。
- 渠道结果不可确认时，服务返回不可确认错误，不能把订单写成成功或失败。

## Agent 边界

Sources:

- `src/api/chat.py`：客户聊天只暴露 `check_payment_status` 等只读工具。
- `src/agent/tools/check_payment_status.py`。

结论：Agent 查询支付状态，但不创建支付、修改金额或直接调用支付网关。
