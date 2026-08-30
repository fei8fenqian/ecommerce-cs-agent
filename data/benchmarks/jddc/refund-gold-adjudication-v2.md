# JDDC 退款 Intent Gold adjudication v2

基线 Gold `refund-gold-100.jsonl` 保持不变，用于 P0/P2/P3/P5 的历史可比性。本文件记录 v2 的人工复审决定；v2 仅改动 case 004 和 case 045。

## Case 004 — DISPUTED / LOW-CONFIDENCE ADJUDICATION

原始文本：“上次不是已经说明已经退款了吗，为什么还会有电话打过来”。

- v1：`refund.delivery_after_refund`
- v2：`general.answer`，无 Workflow

理由：`refund.delivery_after_refund` 仅处理退款后配送、签收/拒收及物流状态交叉。普通来电没有足够证据表明是配送问题。当前 taxonomy 缺少“退款完成后的异常联系/通知”这一独立 Goal，`general.answer` 是现有 taxonomy 下的最小兜底，不是强语义真值。

禁止为此 case 增加“电话 + 退款”生产规则。未来若 taxonomy 新增更贴切的通知/联系 Goal，再重新 adjudicate。

## Case 045 — HIGH-CONFIDENCE ADJUDICATION

原始文本：“昨晚申请的退款，没有入账。刚刚又申请了一笔退款马上就入了账。”

- v1：`STATEMENT`，无 request
- v2：`INFORMATION_QUERY` + `refund.anomaly`

理由：客服上一轮明确询问需处理的问题；用户说明第一笔退款未入账而另一笔立即到账，语用上是在报告第一笔退款异常，而非单纯 FYI。

## Confirmed unchanged

- Case 018 保持 `INFORMATION_QUERY + refund.request`：用户反馈退款申请入口不可用，但仍要完成申请；这不是一般流程咨询。
- Case 093 保持 `ACKNOWLEDGEMENT` 且无 request：用户在复述客服刚说明的到仓处理条件。即便模型预测为 `STATEMENT`，也只是 exact speech-act 差异，不应为提高分数修改 Gold。
