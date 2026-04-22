"""Unit tests for moby_tool_policy — pure functions, no network, no DB."""
from __future__ import annotations


def test_table_returning_tools_constant_has_four_entries():
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert TABLE_RETURNING_TOOLS == frozenset({
        "explorer_search",
        "nearest_filtered_sites",
        "study_coordinators_with_activities",
        "members_search",
    })


def test_table_returning_tools_is_frozenset():
    from backend.app.routers.moby_tool_policy import TABLE_RETURNING_TOOLS
    assert isinstance(TABLE_RETURNING_TOOLS, frozenset)


def test_has_tabular_intent_real_regression_queries_en():
    """The 3 flaky regressions + 3 pattern-1 regressions must all be detected."""
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    queries = [
        "Which sites in France have HLA typing capability available?",          # SC06
        "Show me all sites where profiling form has been uploaded to the database",  # SS01
        "List sites where C_Profiling_Complete is true",                          # SS03
        "Show me all CTS sites in Germany, Italy, and Belgium",                   # SC01
        "Show all sites that have NOT yet completed profiling",                   # SC09
        "Find sites that are CTS-validated but not yet active in any assignment", # SS04
    ]
    for q in queries:
        assert has_tabular_intent(q), f"Expected tabular for: {q!r}"


def test_has_tabular_intent_synthetic_imperatives_en():
    from backend.app.routers.moby_tool_policy import has_tabular_intent
    for q in [
        "give me all members",
        "display sites in Spain",
        "get me the coordinators",
        "fetch the activities",
        "return the members with no sites",
        "search for sites near Paris",
        "pull up the CTS list",
        "how many sites are there?",
        "which members are in Germany?",
        "a table of sites",
        "list of countries",
        "report on qualification status",
    ]:
        assert has_tabular_intent(q), f"Expected tabular for: {q!r}"
