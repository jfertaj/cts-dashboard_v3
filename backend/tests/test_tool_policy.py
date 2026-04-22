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
