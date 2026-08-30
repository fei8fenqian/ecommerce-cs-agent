# laptop_buying_guide 来源审计

## 结论与来源

- 笔记本参数和选购检索使用 `laptop_products`；产品比较应依赖检索结果。
  - Sources: `src/agent/tools/search_product.py`, `src/agent/llm/intent_router.py`
- Agent 系统提示要求比较产品时列出关键参数差异，实时库存应走工具查询。
  - Sources: `src/agent/engines/loop.py:DEFAULT_SYSTEM_PROMPT`

## 未写入

- 未写具体 CPU/GPU 性能结论、接口兼容承诺、型号、价格、库存或促销。
