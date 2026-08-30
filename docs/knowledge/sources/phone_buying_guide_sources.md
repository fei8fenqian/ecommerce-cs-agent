# phone_buying_guide 来源审计

## 结论与来源

- 商品规格、选购建议和知识库检索由 `search_product` 分表处理；手机商品事实来自 `phone_products`。
  - Sources: `src/agent/tools/search_product.py`, `src/agent/llm/intent_router.py`
- 客户可见库存只返回有货/暂时缺货，不能公开精确库存或仓库。
  - Sources: `src/agent/tools/check_stock.py`, `src/agent/engines/loop.py:_CUSTOMER_PROMPT_APPEND`

## 未写入

- 未写具体手机型号、芯片排名、价格、库存、促销、网络参数或品牌优劣。
