"""
Direct unit tests for moby_handlers.py extracted handler functions.

These tests instantiate HandlerContext + mock ActivityTools and call the
extracted handler functions directly — verifying that Phase 3 extraction
preserved correct behavior.  No Salesforce connection required.

Run: pytest backend/tests/test_moby_handlers.py -v
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, call

from app.routers.moby_handlers import (
    HandlerContext,
    ActivityTools,
    handle_followup_activity_sponsor,
    handle_multi_activity_intersection,
    handle_assignment_sites,
    handle_activity,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FAKE_SF = object()  # non-None sentinel; handlers only check truthiness


def _ctx(query: str, **kwargs) -> HandlerContext:
    """Build a HandlerContext with sensible defaults."""
    return HandlerContext(
        query=query,
        s=query.lower(),
        sf=kwargs.get("sf", _FAKE_SF),
        last_table=kwargs.get("last_table", None),
        last_filters=kwargs.get("last_filters", None),
        has_table=kwargs.get("has_table", False),
        is_followup_prefix=kwargs.get("is_followup_prefix", False),
    )


def _tools(**overrides) -> ActivityTools:
    """Build an ActivityTools with all fields as MagicMocks."""
    defaults = dict(
        tool_sites_by_activity=MagicMock(return_value={"rows": [], "columns": []}),
        tool_sites_with_any_activity=MagicMock(return_value={"rows": [], "columns": []}),
        tool_list_all_activities=MagicMock(return_value={"rows": [], "columns": []}),
        tool_activity_country_matrix=MagicMock(return_value={"rows": [], "columns": []}),
        tool_activity_counts_by_country=MagicMock(return_value={"rows": [], "columns": []}),
        tool_activities_with_countries=MagicMock(return_value={"rows": [], "columns": []}),
        tool_activities_with_assignments_counts=MagicMock(return_value={"rows": [], "columns": []}),
        tool_activity_assignments_detailed=MagicMock(return_value={"rows": [], "columns": []}),
        tool_render_chart=MagicMock(return_value={"visualization": None}),
        pretty_label=MagicMock(side_effect=lambda x: x),
        dbg=MagicMock(),
    )
    defaults.update(overrides)
    return ActivityTools(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# handle_followup_activity_sponsor
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleFollowupActivitySponsor:
    """Handler 1: fire when prior table has activity_name column + sponsor follow-up phrase."""

    def _activity_table(self, rows=None):
        return {
            "columns": [{"key": "activity_name"}, {"key": "sites_total"}],
            "rows": rows or [{"activity_name": "Sanofi", "sites_total": 3}],
        }

    def test_fires_for_sanofi_followup(self):
        t = _tools()
        ctx = _ctx("now for Sanofi", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()
        _, kwargs = t.tool_list_all_activities.call_args
        # ctx.s is lowercased so the captured name will also be lowercase
        assert kwargs.get("account_name_like", "").lower() == "sanofi"

    def test_fires_for_what_about(self):
        t = _tools()
        ctx = _ctx("what about Lilly?", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()

    def test_fires_for_for_prefix(self):
        t = _tools()
        ctx = _ctx("for Novartis", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is not None

    def test_returns_answer_and_table(self):
        t = _tools(
            tool_list_all_activities=MagicMock(
                return_value={"rows": [{"activity_name": "Test"}], "columns": [{"key": "activity_name"}]}
            )
        )
        ctx = _ctx("now for Sanofi", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert "answer" in r
        assert "table" in r

    def test_no_fire_without_prior_activity_table(self):
        t = _tools()
        # last_table has no 'activity_name' column
        ctx = _ctx("now for Sanofi", last_table={"columns": [{"key": "country"}], "rows": []})
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None
        t.tool_list_all_activities.assert_not_called()

    def test_no_fire_when_last_table_is_none(self):
        t = _tools()
        ctx = _ctx("now for Sanofi", last_table=None)
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None

    def test_no_fire_when_no_sponsor_phrase(self):
        t = _tools()
        ctx = _ctx("list all activities", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None

    def test_no_fire_when_sf_none(self):
        t = _tools()
        ctx = _ctx("now for Sanofi", sf=None, last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None
        t.tool_list_all_activities.assert_not_called()

    def test_stopword_not_treated_as_sponsor(self):
        t = _tools()
        ctx = _ctx("what about all?", last_table=self._activity_table())
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None

    def test_column_as_string_not_dict(self):
        """Columns may be plain strings (not dicts) — handler handles both."""
        table = {"columns": ["activity_name", "sites_total"], "rows": []}
        t = _tools()
        ctx = _ctx("now for Roche", last_table=table)
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is not None


# ══════════════════════════════════════════════════════════════════════════════
# handle_assignment_sites
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleAssignmentSites:
    """Handler 2: "sites belonging to X assignment" queries."""

    def test_fires_for_belonging_to(self):
        t = _tools()
        ctx = _ctx("show sites belonging to the Beta Preserve assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is not None
        t.tool_sites_by_activity.assert_called_once()

    def test_extracts_assignment_name(self):
        t = _tools()
        ctx = _ctx("which sites are in the Safeguard assignment?")
        r = handle_assignment_sites(ctx, t)
        assert r is not None
        args, kwargs = t.tool_sites_by_activity.call_args
        name_arg = kwargs.get("name") or args[1]
        assert "safeguard" in name_arg.lower()

    def test_fires_for_of_pattern(self):
        t = _tools()
        ctx = _ctx("get all sites of the INNODIA Main assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is not None

    def test_fires_for_assignment_called(self):
        t = _tools()
        ctx = _ctx('show sites for assignment called "Beta Preserve"')
        r = handle_assignment_sites(ctx, t)
        assert r is not None

    def test_no_fire_without_assignment_keyword(self):
        t = _tools()
        ctx = _ctx("show sites in the Safeguard activity")
        r = handle_assignment_sites(ctx, t)
        assert r is None
        t.tool_sites_by_activity.assert_not_called()

    def test_no_fire_with_km(self):
        t = _tools()
        ctx = _ctx("sites within 100 km of any assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is None

    def test_no_fire_without_sites_word(self):
        t = _tools()
        # "is" and "what" are not in the sites trigger list
        # (which, all, show, see, get, ver, todos, sites, sitios, centros ARE)
        ctx = _ctx("is Beta Preserve an assignment?")
        r = handle_assignment_sites(ctx, t)
        assert r is None

    def test_no_fire_when_sf_none(self):
        t = _tools()
        ctx = _ctx("show sites in the Safeguard assignment", sf=None)
        r = handle_assignment_sites(ctx, t)
        assert r is None
        t.tool_sites_by_activity.assert_not_called()

    def test_returns_answer_and_table(self):
        rows = [{"account_name": "Site A"}, {"account_name": "Site B"}]
        t = _tools(
            tool_sites_by_activity=MagicMock(return_value={"rows": rows, "columns": []})
        )
        ctx = _ctx("show sites belonging to the Safeguard assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is not None
        assert "answer" in r
        assert "table" in r
        assert "2" in r["answer"]  # "Found 2 site(s)"

    def test_empty_results_message(self):
        t = _tools(
            tool_sites_by_activity=MagicMock(return_value={"rows": [], "columns": []})
        )
        ctx = _ctx("show sites belonging to the UnknownXYZ assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is not None
        assert "No sites found" in r["answer"]

    def test_exception_falls_through(self):
        t = _tools(
            tool_sites_by_activity=MagicMock(side_effect=RuntimeError("SOQL failed"))
        )
        ctx = _ctx("show sites belonging to the Safeguard assignment")
        r = handle_assignment_sites(ctx, t)
        assert r is None  # exception → None → LLM fallback

    def test_stopword_name_ignored(self):
        t = _tools()
        ctx = _ctx("show sites in the assignment")  # "the" is a stopword
        r = handle_assignment_sites(ctx, t)
        assert r is None


# ══════════════════════════════════════════════════════════════════════════════
# handle_activity — _act_intent gate
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleActivityIntentGate:
    """Verify _act_intent gating — no activity keyword → None."""

    def test_no_activity_keyword_returns_none(self):
        t = _tools()
        ctx = _ctx("show me all sites in Spain")
        r = handle_activity(ctx, t)
        assert r is None

    def test_activity_keyword_triggers(self):
        t = _tools()
        ctx = _ctx("list all activities")
        r = handle_activity(ctx, t)
        assert r is not None

    def test_km_guard_suppresses_english(self):
        """'within 100 km of X activity' → _act_intent suppressed."""
        t = _tools()
        ctx = _ctx("sites within 100 km of the Safeguard activity")
        r = handle_activity(ctx, t)
        assert r is None

    def test_spanish_actividad_does_not_trigger_activity_handler(self):
        """'actividad' does not contain 'activit' → _act_intent=False."""
        t = _tools()
        ctx = _ctx("dame sitios a 100 km de la actividad Safeguard")
        r = handle_activity(ctx, t)
        assert r is None

    def test_no_sf_returns_login_message(self):
        t = _tools()
        ctx = _ctx("list all activities", sf=None)
        r = handle_activity(ctx, t)
        assert r is not None
        assert "answer" in r
        assert "Salesforce" in r["answer"]


# ══════════════════════════════════════════════════════════════════════════════
# handle_activity — sub-path routing
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleActivitySubPaths:
    """Verify each sub-path routes to the correct tool."""

    # ── 0) activities with assignments ──────────────────────────────────────

    def test_activities_with_assignments_count(self):
        t = _tools()
        ctx = _ctx("show activities with assignments")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activities_with_assignments_counts.assert_called_once()
        t.tool_activity_assignments_detailed.assert_not_called()

    def test_activities_with_assignments_detailed(self):
        t = _tools()
        ctx = _ctx("show activities with assignments detailed")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activity_assignments_detailed.assert_called_once()

    def test_activities_with_assignments_last_30_days(self):
        t = _tools()
        ctx = _ctx("show activities with assignments in last 30 days")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activities_with_assignments_counts.assert_called_once()
        _, kwargs = t.tool_activities_with_assignments_counts.call_args
        assert kwargs.get("last_n_days") == 30

    # ── 1a) sponsor filter ──────────────────────────────────────────────────

    def test_sponsor_filter_routes_to_list_all(self):
        t = _tools()
        # Path 1a fires: "sponsor" present, pattern matches "where sanofi is the sponsor"
        # Note: ctx.s is lowercased → captured name is "sanofi" (lowercase)
        ctx = _ctx("activities where Sanofi is the sponsor")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()
        _, kwargs = t.tool_list_all_activities.call_args
        assert kwargs.get("account_name_like", "").lower() == "sanofi"

    def test_sponsored_by(self):
        t = _tools()
        # \bsponsor\b matches in "sponsor" but NOT in "sponsored" (word boundary)
        # Use "with X as the sponsor" phrasing to hit path 1a
        ctx = _ctx("activities with Novartis as the sponsor")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()
        _, kwargs = t.tool_list_all_activities.call_args
        assert kwargs.get("account_name_like", "").lower() == "novartis"

    # ── 1b) list activities ──────────────────────────────────────────────────

    def test_list_all_activities(self):
        t = _tools()
        ctx = _ctx("list all activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()

    def test_show_activities_no_location(self):
        t = _tools()
        ctx = _ctx("show all activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()

    def test_what_activities_exist(self):
        t = _tools()
        ctx = _ctx("what activities exist?")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()

    def test_list_with_name_filter(self):
        t = _tools()
        ctx = _ctx("list all Safeguard activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_list_all_activities.assert_called_once()
        args, kwargs = t.tool_list_all_activities.call_args
        name_arg = kwargs.get("name_like") or (args[1] if len(args) > 1 else None)
        assert name_arg == "Safeguard"

    # ── 2) summary ──────────────────────────────────────────────────────────

    def test_activity_summary(self):
        t = _tools()
        # Avoid "all/show/list/what/which" list words — they trigger is_list_activities=True
        # and route to path 1b before path 2. Use bare "activity summary".
        ctx = _ctx("activity summary")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activity_country_matrix.assert_called_once_with(_FAKE_SF, stacked=False)

    def test_activity_overview(self):
        t = _tools()
        # Same: avoid list words
        ctx = _ctx("activity overview")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activity_country_matrix.assert_called()

    # ── 3) assignments + sites count join ───────────────────────────────────

    def test_assignments_count_and_sites(self):
        t = _tools()
        # Path 0 captures "activities with assignments" — must avoid that phrasing.
        # Use a query that has assignments + count + sites without "activities with assignments".
        ctx = _ctx("assignment count and sites per activity")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activities_with_assignments_counts.assert_called()
        t.tool_activity_country_matrix.assert_called()

    # ── 4) countries involved ───────────────────────────────────────────────

    def test_countries_involved_in_activities(self):
        t = _tools()
        # Path 4 regex: r"countries?\s+involv(ed|idos)" — requires "countries" immediately
        # before "involv". "countries are involved" has "are" in between → no match.
        # Use "countries involved in activities" (direct adjacency).
        ctx = _ctx("countries involved in activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activities_with_countries.assert_called_once()

    def test_country_breakdown(self):
        t = _tools()
        ctx = _ctx("country breakdown of activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activities_with_countries.assert_called_once()

    # ── 5) by country aggregation ────────────────────────────────────────────

    def test_activities_by_country(self):
        t = _tools()
        ctx = _ctx("show activities by country")
        r = handle_activity(ctx, t)
        # Hits path 8 ("activities by country" explicit phrasing) before path 5
        assert r is not None

    def test_activities_per_country(self):
        t = _tools()
        ctx = _ctx("how many activities per country?")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activity_counts_by_country.assert_called()

    # ── 9) stacked barchart follow-up ────────────────────────────────────────

    def test_stacked_barchart_followup(self):
        t = _tools()
        # Must avoid "show/list/all/what/which" list words (they trigger is_list_activities=True)
        ctx = _ctx("stacked bar chart of activities")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_activity_country_matrix.assert_called_with(_FAKE_SF, stacked=True)

    # ── 10) quoted name → by-name ────────────────────────────────────────────

    def test_quoted_activity_name(self):
        t = _tools(
            tool_sites_by_activity=MagicMock(
                return_value={"rows": [{"account_name": "Site A"}], "columns": []}
            )
        )
        ctx = _ctx('sites participating in activity "Beta Preserve"')
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_sites_by_activity.assert_called_once()

    # ── 11) chart intent fallback ─────────────────────────────────────────────

    def test_chart_intent_fallback(self):
        t = _tools()
        # Must avoid "show/list/all/what/which" (trigger is_list_activities=True)
        # "Germany" doesn't match the location group pattern, so uses bare query
        ctx = _ctx("chart of activities in Germany")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_render_chart.assert_called()

    # ── 12) unquoted belong to X activity ────────────────────────────────────

    def test_sites_in_safeguard_activity(self):
        rows = [{"account_name": "Site A"}]
        t = _tools(
            tool_sites_by_activity=MagicMock(return_value={"rows": rows, "columns": []})
        )
        ctx = _ctx("sites in the Safeguard activity")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_sites_by_activity.assert_called()
        args, kwargs = t.tool_sites_by_activity.call_args
        name_arg = kwargs.get("name") or (args[1] if len(args) > 1 else None)
        assert "safeguard" in (name_arg or "").lower()

    def test_sites_belonging_to_activity(self):
        rows = [{"account_name": "Site B"}]
        t = _tools(
            tool_sites_by_activity=MagicMock(return_value={"rows": rows, "columns": []})
        )
        ctx = _ctx("show sites belonging to the INNODIA Main activity")
        r = handle_activity(ctx, t)
        assert r is not None

    # ── 13) fallthrough: sites with any activity ──────────────────────────────

    def test_fallthrough_to_sites_with_any_activity(self):
        """No specific sub-path matches → fallthrough to tool_sites_with_any_activity."""
        t = _tools()
        # "in germany" so mentions_group_or_location=True → not list_activities
        # no match for paths 2–12 → path 13
        ctx = _ctx("which sites have activities in Germany?")
        r = handle_activity(ctx, t)
        assert r is not None
        t.tool_sites_with_any_activity.assert_called_once()

    def test_fallthrough_returns_answer(self):
        t = _tools(
            tool_sites_with_any_activity=MagicMock(
                return_value={"rows": [{"account_id": "001", "account_name": "Site A"}], "columns": []}
            )
        )
        ctx = _ctx("activities in Spain")
        r = handle_activity(ctx, t)
        assert r is not None
        assert "answer" in r


# ══════════════════════════════════════════════════════════════════════════════
# Priority order — handlers must not bleed into each other
# ══════════════════════════════════════════════════════════════════════════════

class TestHandlerPriority:
    """
    Verify that Handler 1 fires before Handler 2, and Handler 2 fires before
    Handler 3, given queries that could match multiple handlers.
    """

    def _activity_table(self):
        return {"columns": [{"key": "activity_name"}], "rows": [{"activity_name": "Test"}]}

    def test_handler1_fires_before_handler3(self):
        """Follow-up 'now for Sanofi' with an activity table → Handler 1 fires, Handler 3 skipped."""
        t = _tools()
        ctx = _ctx("now for Sanofi", last_table=self._activity_table())
        r1 = handle_followup_activity_sponsor(ctx, t)
        assert r1 is not None, "Handler 1 should fire"
        # If Handler 1 fires, Handler 3 should not be called
        t.tool_activity_country_matrix.assert_not_called()

    def test_handler2_fires_before_handler3(self):
        """Assignment query → Handler 2 fires, Handler 3 skipped."""
        t = _tools()
        ctx = _ctx("show sites belonging to the Safeguard assignment")
        r2 = handle_assignment_sites(ctx, t)
        assert r2 is not None
        # Handler 3 (activity handler) checks for "activit" — not present here
        r3 = handle_activity(ctx, t)
        assert r3 is None  # "assignment" query without "activit" → not caught by handler 3

    def test_handler3_skips_when_no_activity_intent(self):
        """Country query → handler 3 returns None (no _act_intent)."""
        t = _tools()
        ctx = _ctx("sites in Spain with Stage 1 > 5")
        r = handle_activity(ctx, t)
        assert r is None

    def test_handler1_does_not_fire_for_assignment_query(self):
        """Assignment query without activity-table context → Handler 1 passes."""
        t = _tools()
        # No last_table → Handler 1 returns None
        ctx = _ctx("show sites belonging to the Safeguard assignment")
        r = handle_followup_activity_sponsor(ctx, t)
        assert r is None


# ══════════════════════════════════════════════════════════════════════════════
# Return contract — all handlers must return Optional[dict]
# ══════════════════════════════════════════════════════════════════════════════

class TestReturnContract:
    """All handlers must return None or a dict with at least 'answer'."""

    @pytest.mark.parametrize("query,handler", [
        ("list all activities", handle_activity),
        ("show activities by country", handle_activity),
        ("which countries are involved in activities?", handle_activity),
        ("sites with any activities in France", handle_activity),
    ])
    def test_always_returns_dict_with_answer(self, query, handler):
        t = _tools()
        ctx = _ctx(query)
        r = handler(ctx, t)
        if r is not None:
            assert isinstance(r, dict)
            assert "answer" in r

    @pytest.mark.parametrize("query", [
        "sites in Spain",
        "which countries have Stage 2 sites",
        "count ND sites per country",
        "actividad Safeguard a 50 km",  # Spanish km-of-assignment — never hits activity handler
    ])
    def test_non_activity_queries_return_none(self, query):
        t = _tools()
        r1 = handle_followup_activity_sponsor(_ctx(query), t)
        r2 = handle_assignment_sites(_ctx(query), t)
        r3 = handle_activity(_ctx(query), t)
        assert r1 is None
        assert r2 is None
        assert r3 is None
        # None of the tools should have been called
        t.tool_list_all_activities.assert_not_called()
        t.tool_sites_by_activity.assert_not_called()
        t.tool_sites_with_any_activity.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# handle_multi_activity_intersection  (GQ15 — Step 3)
# ══════════════════════════════════════════════════════════════════════════════

class TestHandleMultiActivityIntersection:
    """
    Handler 1b: "sites in BOTH X AND Y" — must fire before handle_assignment_sites.

    The critical constraint: tool_sites_by_activity must be called TWICE (once
    per activity), never with an AND condition on two names in one call.
    """

    def _tbl(self, rows):
        return {"columns": [{"key": "account_id"}, {"key": "site"}, {"key": "country"}, {"key": "city"}], "rows": rows}

    def _sites_detect(self):
        return self._tbl([
            {"account_id": "A1", "site": "Site Alpha", "country": "DE", "city": "Berlin"},
            {"account_id": "A2", "site": "Site Beta",  "country": "IT", "city": "Rome"},
            {"account_id": "A3", "site": "Site Gamma", "country": "FR", "city": "Paris"},
        ])

    def _sites_fabulinus(self):
        return self._tbl([
            {"account_id": "A2", "site": "Site Beta",  "country": "IT", "city": "Rome"},
            {"account_id": "A4", "site": "Site Delta", "country": "ES", "city": "Madrid"},
        ])

    # ── detection fires ────────────────────────────────────────────────────────

    @pytest.mark.parametrize("query", [
        "Which sites are in both the DETECT activity and Fabulinus?",
        "which sites are in both detect and fabulinus?",
        "show sites participating in both Beta Preserve and Barricade Delay",
        "sites in DETECT that also participate in Fabulinus",
        "which sites are in DETECT and also in Fabulinus?",
    ])
    def test_fires_for_intersection_queries(self, query):
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        r = handle_multi_activity_intersection(_ctx(query), t)
        assert r is not None, f"Handler should fire for: {query!r}"

    def test_calls_tool_twice_not_once(self):
        """Core contract: two separate calls, not one AND query."""
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        handle_multi_activity_intersection(
            _ctx("Which sites are in both DETECT and Fabulinus?"), t
        )
        assert mock_tsba.call_count == 2

    def test_extracts_correct_names(self):
        """Names passed to tool must be DETECT and Fabulinus, not combined."""
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        handle_multi_activity_intersection(
            _ctx("which sites are in both DETECT and Fabulinus?"), t
        )
        names_called = {call_args[1]["name"] for call_args in mock_tsba.call_args_list}
        assert "detect" in names_called or any("detect" in n.lower() for n in names_called)
        assert "fabulinus" in names_called or any("fabulinus" in n.lower() for n in names_called)

    def test_returns_intersection_not_union(self):
        """Only A2 (Site Beta) is in both — result must be exactly 1 row."""
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        r = handle_multi_activity_intersection(
            _ctx("which sites are in both DETECT and Fabulinus?"), t
        )
        rows = r["table"]["rows"]
        assert len(rows) == 1
        assert rows[0]["account_id"] == "A2"

    def test_zero_intersection_returns_correct_answer(self):
        """No common sites → return 0-row table with informative message, not wrong data."""
        sites_no_overlap = self._tbl([
            {"account_id": "X1", "site": "Site X", "country": "PL", "city": "Warsaw"},
        ])
        mock_tsba = MagicMock(side_effect=[sites_no_overlap, self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        r = handle_multi_activity_intersection(
            _ctx("which sites are in both Alpha Study and Fabulinus?"), t
        )
        assert r is not None
        assert len(r["table"]["rows"]) == 0
        assert "no site" in r["answer"].lower() or "share no common" in r["answer"].lower()

    def test_answer_mentions_both_names(self):
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        r = handle_multi_activity_intersection(
            _ctx("sites in both detect and fabulinus"), t
        )
        ans_lower = r["answer"].lower()
        assert "detect" in ans_lower
        assert "fabulinus" in ans_lower

    # ── detection does NOT fire ────────────────────────────────────────────────

    @pytest.mark.parametrize("query", [
        "which sites are in DETECT?",                         # single activity
        "show sites participating in Beta Preserve",          # single activity
        "sites in Germany",                                   # no activity
        "how many activities are there?",                     # listing, no intersection
        "sites in DETECT and Germany",                        # country AND activity, not two activities
    ])
    def test_does_not_fire_for_single_or_irrelevant_queries(self, query):
        t = _tools()
        r = handle_multi_activity_intersection(_ctx(query), t)
        assert r is None, f"Handler should NOT fire for: {query!r}"
        t.tool_sites_by_activity.assert_not_called()

    def test_no_sf_returns_none(self):
        t = _tools()
        r = handle_multi_activity_intersection(_ctx("sites in both DETECT and Fabulinus", sf=None), t)
        assert r is None
        t.tool_sites_by_activity.assert_not_called()

    def test_priority_fires_before_handle_assignment_sites(self):
        """Intersection handler must intercept before single-activity handler."""
        mock_tsba = MagicMock(side_effect=[self._sites_detect(), self._sites_fabulinus()])
        t = _tools(tool_sites_by_activity=mock_tsba)
        query = "which sites are in both DETECT and Fabulinus?"
        r_multi = handle_multi_activity_intersection(_ctx(query), t)
        # If multi fires and returns a result, handle_assignment_sites should not be needed
        assert r_multi is not None
        # handle_assignment_sites would also fire but gets pre-empted in _try_planner
        assert mock_tsba.call_count == 2  # two calls, not one
