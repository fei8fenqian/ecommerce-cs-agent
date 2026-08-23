"""客户聊天检索速度与精度取舍的轻量回归测试。"""

from api.chat import _should_rerank


def test_product_lookup_and_budget_recommendation_skip_reranking():
    """高频单品问题优先首字速度，不调用 CPU 交叉编码精排。"""
    assert _should_rerank("预算 5000 元推荐一台笔记本", "laptop_products") is False
    assert _should_rerank("小米 17 Ultra 的参数", "phone_products") is False


def test_explicit_product_comparison_keeps_reranking():
    """明确比较需要更稳定的候选排序，保留精排。"""
    assert _should_rerank("惠普 440 和戴尔成就 5630 对比", "laptop_products") is True


def test_policy_and_component_queries_keep_reranking():
    """政策和组件兼容性回答更看重依据相关性。"""
    assert _should_rerank("退货需要什么条件", "knowledge_chunks") is True
    assert _should_rerank("适合 AM5 的内存", "component_products") is True
