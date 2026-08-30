# 当前知识文档审计

审计日期：2026-08-28

本表审计的是 `data/knowledge/*.md`。`runtime_manifest.txt` 是本次唯一允许进入 RAG 的清单；
未列入不代表文件被删除，而是表示其事实来源尚未完成逐条审计，不能在下一次导入时继续作为运行时依据。

| 文档 | 审计结论 | 本次处理 |
| --- | --- | --- |
| `after_sales.md` | 混合退款、保修与通用维修建议。 | 从运行时清单移除；由退款、售后、退换边界、设备安全和保修主题文档替代。 |
| `payment.md` | 支付范围、流程、异常和退款边界混在一篇文档。 | 从运行时清单移除；由 `payment_status.md`、`payment_process.md`、`payment_failure.md` 替代。 |
| `trade_in.md` | 主题有价值，但原文混有将来实现设想。 | 从运行时清单移除；仅保留当前能力边界到 `trade_in_boundary.md`。 |
| `laptop_guide.md` | 通用选购维度没有逐条 SKU/官方资料审计。 | 从运行时清单移除；以目录事实边界重写为 `laptop_buying_guide.md`。 |
| `phone_guide.md` | 通用选购维度没有逐条 SKU/官方资料审计。 | 从运行时清单移除；以目录事实边界重写为 `phone_buying_guide.md`。 |
| `troubleshooting_laptop_general.md` | 有价值但混有未审计步骤。 | 从运行时清单移除；仅保留 T0 策略支持的安全分诊到新设备文档。 |
| `troubleshooting_laptop_apple.md` | Apple 官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_laptop_huawei.md` | 华为官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_laptop_lenovo.md` | 联想官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_phone_android.md` | 混合多个品牌的通用建议，缺少逐条官方来源。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_phone_apple.md` | Apple 官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_phone_huawei.md` | 华为官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |
| `troubleshooting_phone_xiaomi.md` | 小米官方入口是 T1 候选；具体建议未完成对应页面审计。 | 保留为候选，不进入运行时清单。 |

## 本次发现的冲突

- 旧 `after_sales.md` 将退款、保修和安全分诊混在同一 RAG 文档中，无法保证每段都来自同一类型的来源。
- 旧 `payment.md` 同时承担支付方式、支付状态、退款关系和客户操作说明；新的 `payment_status.md` 只描述当前可核验的支付宝沙箱支付状态边界。
- `src/agent/support_control.py` 已明确标记退款 ETA、退款去向、退款失败原因、退货/仓储和价保等能力 `available=False`；任何文档承诺这些结果都会与当前 Capability 冲突。
- `web/src/App.tsx` 的客服入口文案仍称 Agent 会“自动创建工单”，而 `src/service/customer_support_policy.py` 的实际规则会先做退款自助引导或故障分诊。该页面文案需要在后续 UI 修复中与当前行为同步；本轮运行时知识没有沿用这项说法。

## 导入规则

`scripts/ingest/knowledge.py` 现在只读取 `data/knowledge/runtime_manifest.txt` 中列出的 Markdown。来源审计
放在 `docs/knowledge/sources/`，不会被送入 RAG。默认导入为 SAFE UPSERT：对 manifest 中的每个 source 做
增量同步（新增 INSERT、正文变化 UPDATE、删除 section 删除该 source 的 stale chunk、未变化 SKIP），而
manifest 之外的 legacy source 保留在数据库中但不参与运行时检索。只有显式 `--prune` 才会物理删除所有
不在当前 manifest 的 source。

chunk ID 使用 `source + section identity + chunk index` 的稳定键；正文修改会更新原 chunk 和 embedding，
不会追加一个新版本。新增或删除 section 只影响对应 source，完全未变化的 chunk 不重新编码。
