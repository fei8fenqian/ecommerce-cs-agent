import pytest

from agent.tools.present_product_candidates import PresentProductCandidates
from agent.tools_registry import ToolContext


@pytest.mark.asyncio
async def test_present_product_candidates_accepts_only_current_server_frame():
    tool = PresentProductCandidates()
    context = ToolContext(
        user_id=1,
        role="customer",
        product_candidate_refs=frozenset({"candidate_1", "candidate_2"}),
    )

    ok = await tool.execute(
        mode="recommend",
        candidate_refs=["candidate_2", "candidate_1"],
        tool_context=context,
    )
    assert ok.is_success
    assert ok.data == {
        "mode": "recommend",
        "candidate_refs": ["candidate_2", "candidate_1"],
    }

    bad = await tool.execute(
        mode="recommend",
        candidate_refs=["candidate_99"],
        tool_context=context,
    )
    assert bad.is_success is False
    assert "当前服务端候选帧" in bad.error


@pytest.mark.asyncio
async def test_present_product_candidates_choice_mode_is_not_selection():
    tool = PresentProductCandidates()
    context = ToolContext(
        user_id=1,
        role="customer",
        product_candidate_refs=frozenset({"candidate_1", "candidate_2"}),
    )
    result = await tool.execute(
        mode="choice",
        candidate_refs=["candidate_1", "candidate_2"],
        tool_context=context,
    )
    assert result.is_success
    assert result.data["mode"] == "choice"
    assert "selected_ref" not in result.data
