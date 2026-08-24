"""客户 Agent 的公开信息边界与站内下一步链接测试。"""

from agent.engines.loop import AgentLoop
from agent.tools.search_product import _customer_visible_content
from agent.tools_registry import ToolRegistry
from api.chat import _customer_action_suffix


def test_customer_prompt_forbids_internal_inventory_facts():
    """客户身份必须得到不同于内部人员的模型输出约束。"""
    agent = AgentLoop(llm=object(), registry=ToolRegistry())
    prompt = agent._system_content_for(type("Context", (), {"role": "customer"})())

    assert "绝不提及仓库名称" in prompt
    assert "精确库存数量" in prompt


def test_customer_product_text_strips_internal_warehouse_details():
    """商品检索的客户视图不携带仓库和库存细节。"""
    content = _customer_visible_content("售价 4999 元。华南仓库存 18 台，现货。")

    assert "华南仓" not in content
    assert "库存" not in content
    assert "售价 4999 元" in content


def test_customer_action_links_point_to_first_party_pages():
    """购买、订单和售后诉求必须获得稳定的站内下一步。"""
    assert "?page=catalog" in _customer_action_suffix("rag", "laptop_products", "我想买这台")
    assert "?page=orders" in _customer_action_suffix("agent", "", "帮我查物流")
    assert "?page=tickets" in _customer_action_suffix("ticket", "", "我要退款")
