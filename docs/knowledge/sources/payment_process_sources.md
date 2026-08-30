# payment_process 来源审计

## 结论与来源

- 当前 checkout 使用支付宝沙箱，创建、继续支付和取消均经过受控 API/Service。
  - Sources: `src/api/checkout.py`, `src/service/checkout_service.py`
- 创建支付前核验商品、库存、数量和服务端金额；付款表单或二维码不代表支付成功。
  - Sources: `src/service/checkout_service.py:create_checkout_session`, `build_alipay_qr_checkout_session`
- 取消待支付订单前会收敛支付渠道状态。
  - Sources: `src/service/checkout_service.py:cancel_checkout_session`

## 未写入

- 未写生产支付渠道、费率、限额或到账时效；当前实现是沙箱。
