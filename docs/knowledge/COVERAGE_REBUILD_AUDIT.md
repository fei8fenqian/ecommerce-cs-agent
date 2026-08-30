# Knowledge Base Coverage Rebuild 审计

## 审计口径

运行时知识只接受 T0 项目代码/配置/受控策略可证明的结论。本轮没有把品牌官网链接或模型常识改写成项目承诺；需要具体品牌、型号、政策或检测的结论保留为缺口。

## 旧知识素材分类

| 旧文件 | 结论 | 处理 |
| --- | --- | --- |
| `after_sales.md` | REWRITE | 退款部分已由首批文档覆盖；售后查询与退换能力拆入新主题。 |
| `payment.md` | REWRITE | 支付流程与异常拆入 `payment_process`、`payment_failure`。 |
| `phone_guide.md` | REWRITE | 保留“以当前目录核验”的系统边界，拆入 `phone_buying_guide`。 |
| `laptop_guide.md` | REWRITE | 保留“以当前目录核验”的系统边界，拆入 `laptop_buying_guide`。 |
| `trade_in.md` | REWRITE | 保留当前无能力结论，拆入 `trade_in_boundary`。 |
| `troubleshooting_laptop_general.md` | REWRITE | 仅保留代码已定义的低风险分诊与升级边界。 |
| 品牌专属笔记本排障 3 篇 | RETIRE | 没有逐型号、逐步骤的当前官方来源审计。 |
| 品牌/安卓手机排障 4 篇 | RETIRE | 没有逐型号、逐步骤的当前官方来源审计。 |

## 运行时范围

- KEEP：首批 `refund_request`、`refund_status`、`payment_status`、`agent_boundaries`。
- REWRITE 并上线：订单、支付、退款限制、售后、退换边界、设备安全、保修、选购、以旧换新与隐私边界新文档。
- RETIRE：旧文件仍作为素材保留在 `data/knowledge`，但不进入 `runtime_manifest.txt`。

## 明确能力缺口

- 退款 ETA、退款去向、退款失败原因、退款取消资格。
- 预计发货时间、退货状态、退货物流、仓库签收、换货资格、价保。
- 设备诊断、统一保修期限、序列号保修、维修报价与品牌型号专属维修流程。
- 以旧换新估价、回收、补贴和订单能力。
