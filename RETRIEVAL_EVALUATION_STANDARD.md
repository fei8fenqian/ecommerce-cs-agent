# 检索评测标准口径

## 当前已测结果

- 语料：2,818 条商品与知识文档。
- 测试问题：130 条，来源为 `data/test_questions.json`。
- 预热本地离线基准：向量检索延迟 P95 为 45.31ms；RRF 融合并 Reranker 精排的全链路延迟 P95 为 1.23s。
- 当前的 97.69% 仅为向量检索的**关键词 Hit@5**。判定条件是 Top-5 中至少一篇文档包含题目配置的任一预期关键词。
- 另有 `data/relevance_labels_pilot_v1.json`：由单一标注者根据当前语料复核的 27 题试点，排除 3 道无可确认目标文档的问题。运行 `python scripts/eval_relevance_pilot.py` 的结果为 Recall@5 96.30%、MRR 0.9167、nDCG@5 0.9084、P95 44.21ms；该试点不是双人复核金标。

它用于发现检索链路的明显回归；不等同于标准 Recall@5，也不能用于宣称人工标注准确率。

## 金标格式

每道题应独立保存如下记录：

```json
{
  "question_id": 1,
  "query": "联想拯救者用的什么CPU？",
  "source": "laptop_products",
  "relevant_doc_ids": ["<真实文档ID>"],
  "annotation_status": "reviewed",
  "annotators": ["annotator_a", "annotator_b"],
  "reviewed_at": "2026-08-20"
}
```

`relevant_doc_ids` 的判断标准是：该文档本身能够支持回答问题，而不是只含有某个相同关键词。允许多篇相关文档。

## 标注流程

1. 对每题从对应数据源导出向量、BM25 和混合检索的 Top-10 候选，候选排序不作为相关性结论。
2. 两名标注者独立标记每篇候选为相关、不相关或不确定；必要时在数据源中补充搜索，避免候选池遗漏唯一正确文档。
3. 分歧题由复核者决定，冻结 `relevant_doc_ids`；问题、语料或标签变化后都要递增数据集版本。
4. 仅对 `annotation_status=reviewed` 的题目计算指标，并在报告中记录题数、数据集版本、代码 commit、模型版本与评测日期。

## 指标定义

- Recall@5：每题 Top-5 是否包含至少一个 `relevant_doc_ids` 中的文档，再对全部题目求平均。
- MRR：第一篇相关文档排名的倒数平均值，衡量正确结果是否靠前。
- nDCG@5：可在相关性分级（如强相关/相关）后衡量前五名的排序质量。
- P50/P95：预热后逐请求端到端耗时的分位数；必须区分是否包含 Reranker。

在金标完成前，简历和报告只能使用“关键词 Hit@5”这一名称。
