from agent.goal_taxonomy import canonicalize_goal, get_goal_definition, is_canonical_goal


def test_product_natural_operation_aliases_collapse_into_small_canonical_taxonomy():
    assert canonicalize_goal("product", "recommend") == ("product", "search_product")
    assert canonicalize_goal("product", "discover") == ("product", "search_product")
    assert canonicalize_goal("product", "provide_link") == ("product", "answer")
    assert canonicalize_goal("product", "view_detail") == ("product", "answer")

    # These are boundary aliases; the canonical owner remains the small existing
    # product taxonomy rather than a new operation per customer phrase.
    assert is_canonical_goal("product", "recommend") is True
    assert get_goal_definition("product", "recommend").operation == "search_product"
