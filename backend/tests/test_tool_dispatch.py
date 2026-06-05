"""
Tests for moby_tools.py — tool dispatch registry.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from app.routers.moby_tools import (
    TOOL_DISPATCH,
    ToolContext,
    ToolResult,
    dispatch_tool,
    register_tool,
)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

EXPECTED_TOOLS = [
    "sql_query",
    "salesforce_query",
    "soql_query",              # alias for salesforce_query (new TOOLS_SPEC name)
    "salesforce_account_extras",
    "explorer_set_filters",
    "salesforce_account_contacts",
    "salesforce_assignments",
    "rank_sites",
    "rank_sites_by_group",     # alias for rank_sites (merged)
    "group_count",
    "group_count_agg",         # alias for group_count (merged)
    "group_count_sf",          # alias for group_count (merged)
    "list_activities",
    "activities_with_countries",
    "activity_counts_by_country",
    "activity_country_matrix",
    "activities_sites",
    "activities_by_name",
    "sf_aggregate",            # merged group_agg_sf + time_series_sf
    "group_agg_sf",            # alias for sf_aggregate
    "time_series_sf",          # alias for sf_aggregate
    "activities_with_assignments_counts",
    "activity_assignments_detailed",
    "sql_query_fill_sf",
    "contacts_by_group",
    "study_coordinators_with_activities",
    "assignment_contact_report",
    "qual_search",
    "manipulate_data",
    "render_chart",
    "explorer_search",
    "nearest_filtered_sites",
    "explorer_within_drive_km",
    "members_search",
]


def test_all_expected_tools_registered():
    """Every tool from the original if/elif chain must be in TOOL_DISPATCH."""
    for name in EXPECTED_TOOLS:
        assert name in TOOL_DISPATCH, f"Tool '{name}' missing from TOOL_DISPATCH"


def test_tool_count():
    """Exactly 34 tools registered (includes merged aliases)."""
    assert len(TOOL_DISPATCH) == len(EXPECTED_TOOLS)


def test_no_extra_tools():
    """No unexpected tools in the registry."""
    extras = set(TOOL_DISPATCH.keys()) - set(EXPECTED_TOOLS)
    assert not extras, f"Unexpected tools in registry: {extras}"


def test_all_handlers_are_callable():
    for name, handler in TOOL_DISPATCH.items():
        assert callable(handler), f"Handler for '{name}' is not callable"


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------

def _make_ctx(**overrides) -> ToolContext:
    """Create a minimal ToolContext for testing."""
    defaults = dict(
        db=MagicMock(),
        request=MagicMock(),
        sf=None,
        last_table=None,
        last_visualization=None,
        last_explorer_filters=None,
        msgs=[],
        args={},
        tool_call_id="test-call-id",
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_dispatch_unknown_tool():
    """Unknown tool name appends an error message."""
    ctx = _make_ctx()
    result = dispatch_tool("nonexistent_tool_xyz", ctx)
    assert len(ctx.msgs) == 1
    msg = ctx.msgs[0]
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "test-call-id"
    content = json.loads(msg["content"])
    assert "error" in content
    assert "Unknown tool" in content["error"]


def test_dispatch_returns_tool_result():
    """dispatch_tool returns a ToolResult instance."""
    ctx = _make_ctx()
    result = dispatch_tool("nonexistent_tool_xyz", ctx)
    assert isinstance(result, ToolResult)


# ---------------------------------------------------------------------------
# Individual tool handler smoke tests (no real SF/DB — just verify structure)
# ---------------------------------------------------------------------------

def test_sql_query_handler_error_path():
    """sql_query handler catches exceptions and appends error msg."""
    ctx = _make_ctx(args={"sql": "SELECT 1"})
    # tool_sql_query will fail because db is a mock
    with patch("app.routers.moby_tools.handle_sql_query.__module__"):
        result = dispatch_tool("sql_query", ctx)
    assert isinstance(result, ToolResult)
    assert len(ctx.msgs) >= 1
    last_msg = ctx.msgs[-1]
    assert last_msg["role"] == "tool"


def test_salesforce_query_no_session():
    """salesforce_query with sf=None should produce an error message."""
    ctx = _make_ctx(sf=None, args={"soql": "SELECT Id FROM Account"})
    result = dispatch_tool("salesforce_query", ctx)
    assert len(ctx.msgs) == 1
    content = json.loads(ctx.msgs[0]["content"])
    assert "error" in content
    assert "Salesforce session" in content["error"]


def test_salesforce_account_extras_no_session():
    """salesforce_account_extras with sf=None should produce an error message."""
    ctx = _make_ctx(sf=None, args={"account_id": "001xxx"})
    result = dispatch_tool("salesforce_account_extras", ctx)
    assert len(ctx.msgs) == 1
    content = json.loads(ctx.msgs[0]["content"])
    assert "error" in content


def test_explorer_set_filters_calls_tool():
    """explorer_set_filters dispatches and appends a msg."""
    with patch("app.routers.ai_chat.tool_explorer_set_filters", return_value={"ok": True}):
        ctx = _make_ctx(args={"filters": {"logic": "AND", "rules": []}})
        result = dispatch_tool("explorer_set_filters", ctx)
        assert len(ctx.msgs) == 1
        content = json.loads(ctx.msgs[0]["content"])
        assert content == {"ok": True}


def test_salesforce_account_contacts_no_session():
    ctx = _make_ctx(sf=None, args={"account_id": "001abc"})
    result = dispatch_tool("salesforce_account_contacts", ctx)
    assert len(ctx.msgs) == 1
    content = json.loads(ctx.msgs[0]["content"])
    assert "error" in content


def test_activities_with_countries_no_session():
    ctx = _make_ctx(sf=None)
    result = dispatch_tool("activities_with_countries", ctx)
    assert len(ctx.msgs) == 1
    content = json.loads(ctx.msgs[0]["content"])
    assert "error" in content


def test_manipulate_data_preserves_state():
    """manipulate_data handler should carry forward last_table/last_visualization."""
    with patch("app.routers.ai_chat.tool_manipulate_data", return_value={"rows": [{"a": 1}]}):
        existing_table = {"columns": [{"key": "a", "label": "A"}], "rows": [{"a": 1}, {"a": 2}]}
        existing_viz = {"type": "bar", "xKey": "a", "yKeys": ["a"]}
        ctx = _make_ctx(
            last_table=existing_table,
            last_visualization=existing_viz,
            args={"operation": "group_others"},
        )
        result = dispatch_tool("manipulate_data", ctx)
        # Should have updated last_table from the manipulated result
        assert result.last_table is not None
        assert result.last_table.get("rows") == [{"a": 1}]


def test_render_chart_uses_last_table_when_no_data():
    """render_chart falls back to last_table rows when args.data is empty."""
    fake_viz = {"type": "bar", "data": [{"x": 1}]}
    with patch("app.routers.ai_chat.tool_render_chart", return_value={"visualization": fake_viz}):
        with patch("app.routers.ai_chat._pretty_label", side_effect=lambda x: x):
            existing_table = {"columns": [], "rows": [{"country": "Spain", "count": 5}]}
            ctx = _make_ctx(
                last_table=existing_table,
                args={"kind": "bar", "xKey": "country", "yKeys": ["count"], "data": []},
            )
            result = dispatch_tool("render_chart", ctx)
            assert result.last_visualization == fake_viz


def test_explorer_within_drive_km_no_base_id():
    """explorer_within_drive_km with no base_account_id and no last_table appends error."""
    with patch("app.routers.ai_chat._first_account_id_from_table", return_value=None):
        ctx = _make_ctx(last_table=None, args={})
        result = dispatch_tool("explorer_within_drive_km", ctx)
        assert len(ctx.msgs) == 1
        content = json.loads(ctx.msgs[0]["content"])
        assert "error" in content
        assert "base_account_id" in content["error"]
