"""
Unit tests for moby_planner.py — Phase 1 + 2 Structured Query Planner.
No DB, no network, no FastAPI — pure Python logic.

Run: pytest backend/tests/test_moby_planner.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.moby_planner import (
    parse_query_plan,
    plan_to_system_hint,
    can_short_circuit,
    plan_to_filter_group,
    _resolve_countries,
    _extract_filters,
    _extract_catalog_filters,
    _infer_filter_from_context,
    can_followup_merge,
    merge_with_last_filters,
    build_clarification,
    CLARIFICATION_TEMPLATES,
    validate_filter_group,
)

# ── Minimal kindex fixture ─────────────────────────────────────────────────────
# Mirrors the structure returned by _build_knowledge_index() in ai_chat.py.
# Keys are normalized aliases (lowercase, spaces→underscores stripped of separators).
_KINDEX: dict = {
    "stage 1":         {"source": "sf",   "field": "C_Number_of_Stage1_Individuals_followed__c"},
    "stage 2":         {"source": "sf",   "field": "C_Number_of_Stage2_Individuals_followed__c"},
    "stage1":          {"source": "sf",   "field": "C_Number_of_Stage1_Individuals_followed__c"},
    "stage2":          {"source": "sf",   "field": "C_Number_of_Stage2_Individuals_followed__c"},
    "newly diagnosed": {"source": "sf",   "field": "C_Number_of_new_T1D_diagnosed_O_18__c",
                        "label": "Newly Diagnosed T1D ≥18"},
    "nd":              {"source": "sf",   "field": "C_Number_of_new_T1D_diagnosed_O_18__c"},
    "pharmacy":        {"source": "qual", "key": "3_6__is_your_pharmacy_on_site_or_off_campus",
                        "label": "Pharmacy on-site or off-campus"},
    "overnight":       {"source": "qual", "key": "3_5_2__overnight_stay",
                        "label": "Overnight stay"},
    "hla":             {"source": "sf",   "field": "C_HLA_Typing_Available__c", "label": "HLA Typing"},
    "pi":              {"source": "sf",   "field": "C_PI_Name__c", "label": "Principal Investigator"},
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def plan(text, last_filters=None, last_table=None):
    """Convenience wrapper."""
    return parse_query_plan(text, _KINDEX, last_filters=last_filters, last_table=last_table)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Country resolution
# ══════════════════════════════════════════════════════════════════════════════

class TestCountryResolution:
    def test_single_country_english(self):
        p = plan("show sites in Italy with stage 2 > 0")
        assert "IT" in p["countries"]

    def test_multi_country(self):
        p = plan("sites in Italy and Spain")
        assert "IT" in p["countries"]
        assert "ES" in p["countries"]

    def test_country_adjective(self):
        p = plan("show all italian sites")
        assert "IT" in p["countries"]

    def test_spanish_country_name(self):
        p = plan("sitios en España con stage 1")
        assert "ES" in p["countries"]

    def test_no_country_mentioned(self):
        p = plan("show all sites with stage 2 > 5")
        assert p["countries"] == []

    def test_iso2_not_doubled(self):
        # "Italy" and "italian" should not both add IT twice
        p = plan("show italian sites in Italy")
        assert p["countries"].count("IT") == 1

    def test_direct_helper(self):
        countries = _resolve_countries("sites in Germany and France")
        assert "DE" in countries
        assert "FR" in countries


# ══════════════════════════════════════════════════════════════════════════════
# 2. Filter extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestFilterExtraction:
    def test_stage1_default_gt0(self):
        filters, unresolved = _extract_filters("show sites with stage 1")
        fields = [f["field"] for f in filters]
        assert "sf.C_Number_of_Stage1_Individuals_followed__c" in fields
        field = next(f for f in filters if "Stage1" in f["field"])
        assert field["operator"] == ">"
        assert field["value"] == 0

    def test_stage2_with_comparator(self):
        filters, _ = _extract_filters("sites where stage 2 >= 5")
        field = next((f for f in filters if "Stage2" in f["field"]), None)
        assert field is not None
        assert field["operator"] == ">="
        assert field["value"] == 5

    def test_nd_no_value(self):
        filters, _ = _extract_filters("newly diagnosed sites")
        nd = next((f for f in filters if "new_T1D" in f["field"]), None)
        assert nd is not None
        assert nd["operator"] == ">"
        assert nd["value"] == 0

    def test_nd_with_comparator(self):
        filters, _ = _extract_filters("sites with newly diagnosed > 10")
        nd = next((f for f in filters if "new_T1D" in f["field"]), None)
        assert nd is not None
        assert nd["operator"] == ">"
        assert nd["value"] == 10

    def test_nd_under_18_no_spurious_count_filter(self):
        # Regression: "newly diagnosed <18" was being parsed as "ND≥18 < 18"
        # (count comparator on the wrong field), producing 35 IT sites instead
        # of all 70.
        filters, _ = _extract_filters(
            "total number of newly diagnosed <18 in italian clinical sites"
        )
        for f in filters:
            if "new_T1D" not in f["field"]:
                continue
            spurious = (
                f["field"] == "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
                and f["operator"] == "<"
                and f["value"] == 18
            )
            assert not spurious, f"spurious O_18 < 18 filter: {f}"

    def test_nd_under_18_age_no_threshold_skips_filter(self):
        # When the user mentions only the age qualifier (no count threshold)
        # we keep the country scope wide and let aggregation happen on the
        # full result set — no ND filter at all.
        filters, _ = _extract_filters("total newly diagnosed <18")
        nd_filters = [f for f in filters if "new_T1D" in f["field"]]
        assert nd_filters == []

    def test_nd_under_18_with_threshold_uses_u18_field(self):
        filters, _ = _extract_filters(
            "newly diagnosed <18 with more than 100"
        )
        nd = next((f for f in filters if "new_T1D" in f["field"]), None)
        assert nd is not None
        assert nd["field"] == "sf.C_Number_of_new_T1D_diagnosed_U_18__c"
        assert nd["operator"] == ">"
        assert nd["value"] == 100

    def test_nd_over_18_uses_o18_field(self):
        filters, _ = _extract_filters(
            "newly diagnosed >=18 with more than 50"
        )
        nd = next((f for f in filters if "new_T1D" in f["field"]), None)
        assert nd is not None
        assert nd["field"] == "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
        assert nd["operator"] == ">"
        assert nd["value"] == 50

    def test_nd_under_18_words_picks_u18(self):
        # "under 18" / "pediatric" should also map to U_18 field.
        filters, _ = _extract_filters("newly diagnosed under 18 > 30")
        nd = next((f for f in filters if "new_T1D" in f["field"]), None)
        assert nd is not None
        assert nd["field"] == "sf.C_Number_of_new_T1D_diagnosed_U_18__c"
        assert nd["value"] == 30

    def test_pharmacy_on_site_resolved(self):
        filters, unresolved = _extract_filters("sites with pharmacy on-site")
        ph = next((f for f in filters if "pharmacy" in f["field"]), None)
        assert ph is not None
        assert ph["value"] == "On-site"
        assert not unresolved

    def test_pharmacy_bare_unresolved(self):
        # "pharmacy" without on-site/off-campus qualifier → unresolved
        filters, unresolved = _extract_filters("sites that have a pharmacy")
        ph_filters = [f for f in filters if "pharmacy" in f["field"]]
        assert ph_filters == []
        assert any("pharmacy" in u for u in unresolved)

    def test_overnight_stay(self):
        filters, _ = _extract_filters("sites with overnight stay")
        ov = next((f for f in filters if "overnight" in f["field"]), None)
        assert ov is not None
        assert ov["value"] == "Yes"

    def test_multi_filter(self):
        filters, _ = _extract_filters("sites with stage 1 and overnight stay")
        fields = [f["field"] for f in filters]
        assert any("Stage1" in f for f in fields)
        assert any("overnight" in f for f in fields)

    def test_no_known_filters(self):
        filters, _ = _extract_filters("tell me about the sites")
        assert filters == []


# ══════════════════════════════════════════════════════════════════════════════
# 3. Table query with column requests
# ══════════════════════════════════════════════════════════════════════════════

class TestTableWithColumns:
    def test_intent_is_table_query(self):
        p = plan("show me all sites with stage 1 and stage 2")
        assert p["intent"] == "table_query"
        assert p["output_mode"] == "table"

    def test_filters_populated(self):
        p = plan("show me all sites with stage 1 and stage 2")
        fields = [f["field"] for f in p["filters"]]
        assert any("Stage1" in f for f in fields)
        assert any("Stage2" in f for f in fields)

    def test_confidence_positive(self):
        p = plan("show me all sites with stage 1 and stage 2")
        assert p["confidence"] > 0.4

    def test_multi_country_and_filter_high_confidence(self):
        p = plan("show sites in Italy and Spain with stage 2 > 0")
        assert "IT" in p["countries"]
        assert "ES" in p["countries"]
        assert any("Stage2" in f["field"] for f in p["filters"])
        # Countries + filters + no unresolved → should hit high confidence
        assert p["confidence"] >= 0.70

    def test_nd_under_18_with_country_short_circuits(self):
        # Regression: "total newly diagnosed <18 in italian sites" should
        # short-circuit so the planner can return the table + aggregation
        # without paying for an LLM call. The age qualifier surfaces the
        # U_18 column to lift confidence to the short-circuit threshold.
        p = plan(
            "Can you tell me the total number of newly diagnosed <18 "
            "in all italian clinical sites"
        )
        assert "IT" in p["countries"]
        assert "sf.C_Number_of_new_T1D_diagnosed_U_18__c" in p["requested_columns"]
        # No spurious ND filter
        for f in p["filters"]:
            if "new_T1D" in f["field"]:
                assert not (
                    f["field"] == "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
                    and f["operator"] == "<"
                    and f["value"] == 18
                ), "spurious ND≥18 < 18 filter still present"
        assert can_short_circuit(p), (
            f"expected short-circuit, got confidence={p['confidence']}, "
            f"intent={p['intent']}, output_mode={p['output_mode']}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Export CSV intent
# ══════════════════════════════════════════════════════════════════════════════

class TestExportCSV:
    def test_export_intent(self):
        p = plan("export sites in France as CSV")
        assert p["intent"] == "export_csv"
        assert p["needs_export"] is True
        assert "FR" in p["countries"]

    def test_descargar_spanish(self):
        p = plan("descargar lista de sitios en España")
        assert p["needs_export"] is True

    def test_export_confidence(self):
        p = plan("export all sites in Germany with stage 1 as CSV")
        assert p["needs_export"] is True
        assert p["confidence"] >= 0.60


# ══════════════════════════════════════════════════════════════════════════════
# 5. Ambiguous / unresolved terms
# ══════════════════════════════════════════════════════════════════════════════

class TestUnresolvedTerms:
    def test_bare_pharmacy_unresolved(self):
        p = plan("show sites with a pharmacy")
        assert any("pharmacy" in u for u in p["unresolved_terms"])

    def test_unresolved_lowers_confidence(self):
        p_clean    = plan("show sites with stage 1 in Italy")
        p_unresolved = plan("show sites with a pharmacy in Italy")
        assert p_clean["confidence"] > p_unresolved["confidence"]

    def test_multiple_unresolved(self):
        p = plan("show sites with a pharmacy and good infrastructure")
        assert len(p["unresolved_terms"]) >= 1

    def test_no_unresolved_for_clear_query(self):
        p = plan("show sites with stage 2 > 0 in Spain")
        assert p["unresolved_terms"] == []


# ══════════════════════════════════════════════════════════════════════════════
# 6. Followup intent and last_filters reuse
# ══════════════════════════════════════════════════════════════════════════════

class TestFollowupIntent:
    _LAST_FILTERS = {
        "logic": "AND",
        "rules": [{"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": 0}]
    }
    _LAST_TABLE = {"rows": [{"account_name": "Site A", "sf.Stage1": 5}]}

    def test_followup_detected_with_context(self):
        p = plan("and also show pharmacy", last_filters=self._LAST_FILTERS)
        assert p["intent"] == "followup"

    def test_last_filters_reused_flag(self):
        # No new filters in the query → last_filters_reused should be True
        p = plan("and what about France?", last_filters=self._LAST_FILTERS)
        assert p["last_filters_reused"] is True

    def test_no_followup_without_context(self):
        # Same phrasing, no last_filters → should not be followup
        p = plan("and also show me the sites", last_filters=None)
        assert p["intent"] != "followup"


# ══════════════════════════════════════════════════════════════════════════════
# 7. Fallback on bad input
# ══════════════════════════════════════════════════════════════════════════════

class TestFallback:
    def test_empty_string(self):
        p = plan("")
        assert p["intent"] == "other"
        assert p["confidence"] == 0.0
        assert p["filters"] == []
        assert p["countries"] == []

    def test_none_like_whitespace(self):
        p = plan("   ")
        assert p["intent"] == "other"
        assert p["confidence"] == 0.0

    def test_garbage_input(self):
        p = plan("aaaaa bbbbbb ccccc dddd")
        assert p["intent"] == "other"
        assert p["confidence"] <= 0.2

    def test_always_returns_dict(self):
        # Passing a kindex that would cause attribute errors — must not raise
        p = parse_query_plan("show me sites in Italy", kindex={})
        assert isinstance(p, dict)
        assert "intent" in p


# ══════════════════════════════════════════════════════════════════════════════
# 8. plan_to_system_hint
# ══════════════════════════════════════════════════════════════════════════════

class TestPlanToSystemHint:
    def test_other_intent_returns_empty(self):
        p = plan("aaaa bbbbb")
        assert plan_to_system_hint(p) == ""

    def test_hint_contains_intent(self):
        p = plan("show sites in Italy with stage 2 > 0")
        hint = plan_to_system_hint(p)
        assert "table query" in hint.lower() or "intent:" in hint.lower()

    def test_hint_contains_countries(self):
        p = plan("show sites in France and Germany")
        hint = plan_to_system_hint(p)
        assert "FR" in hint or "DE" in hint

    def test_hint_contains_filters(self):
        p = plan("show sites with stage 1 > 0")
        hint = plan_to_system_hint(p)
        assert "Stage1" in hint

    def test_hint_contains_unresolved_warning(self):
        p = plan("show sites with a pharmacy in Italy")
        hint = plan_to_system_hint(p)
        if p["unresolved_terms"]:
            assert "unresolved" in hint.lower() or "ambiguous" in hint.lower()

    def test_export_flagged_in_hint(self):
        p = plan("export sites in Spain as CSV")
        hint = plan_to_system_hint(p)
        assert "export" in hint.lower() or "csv" in hint.lower()


# ══════════════════════════════════════════════════════════════════════════════
# 9. can_short_circuit and plan_to_filter_group
# ══════════════════════════════════════════════════════════════════════════════

class TestShortCircuit:
    def test_high_confidence_no_unresolved_triggers_shortcircuit(self):
        p = plan("show all sites in Italy with stage 2 > 0")
        # Must have high confidence + no unresolved + table output
        if p["confidence"] >= 0.80 and not p["unresolved_terms"]:
            assert can_short_circuit(p) is True

    def test_unresolved_blocks_shortcircuit(self):
        p = plan("show sites with a pharmacy in Spain")
        # pharmacy without on-site qualifier → unresolved → no short-circuit
        if p["unresolved_terms"]:
            assert can_short_circuit(p) is False

    def test_other_intent_blocks_shortcircuit(self):
        p = plan("aaaa bbbbb")
        assert can_short_circuit(p) is False

    def test_filter_group_single_country(self):
        p = plan("show sites in France with stage 1 > 0")
        fg = plan_to_filter_group(p)
        assert fg["logic"] == "AND"
        country_rules = [r for r in fg["rules"] if r.get("field") == "site.country"]
        assert any(r["value"] == "FR" for r in country_rules)

    def test_filter_group_multi_country_nested_or(self):
        p = plan("show sites in Italy and Spain with stage 2 > 0")
        fg = plan_to_filter_group(p)
        # Multi-country → nested OR sub-group
        sub_groups = [r for r in fg["rules"] if isinstance(r.get("rules"), list)]
        if sub_groups:
            assert sub_groups[0]["logic"] == "OR"
            iso2s = {r["value"] for r in sub_groups[0]["rules"]}
            assert "IT" in iso2s
            assert "ES" in iso2s

    def test_filter_group_no_country(self):
        p = plan("show sites with stage 1 > 5")
        fg = plan_to_filter_group(p)
        country_rules = [r for r in fg["rules"] if r.get("field") == "site.country"]
        assert country_rules == []
        stage_rules = [r for r in fg["rules"] if "Stage1" in r.get("field", "")]
        assert stage_rules


# ══════════════════════════════════════════════════════════════════════════════
# 10. Output mode detection
# ══════════════════════════════════════════════════════════════════════════════

class TestOutputMode:
    def test_chart_intent(self):
        p = plan("show a bar chart of sites by country")
        assert p["intent"] == "chart"
        assert p["output_mode"] == "chart"

    def test_map_intent(self):
        p = plan("show sites on a map")
        assert p["output_mode"] == "map"

    def test_default_table(self):
        p = plan("list all sites with stage 1 in Germany")
        assert p["output_mode"] == "table"


# ══════════════════════════════════════════════════════════════════════════════
# 11. Phase 2 — _infer_filter_from_context
# ══════════════════════════════════════════════════════════════════════════════

class TestInferFilterFromContext:
    def test_with_prefix_returns_is_not_empty(self):
        spec = _infer_filter_from_context("hla", "sf.C_HLA_Typing_Available__c", "sites with ", "")
        assert spec is not None
        assert spec["operator"] == "is_not_empty"
        assert spec["field"] == "sf.C_HLA_Typing_Available__c"

    def test_without_prefix_returns_is_empty(self):
        spec = _infer_filter_from_context("hla", "sf.C_HLA_Typing_Available__c", "sites without ", "")
        assert spec is not None
        assert spec["operator"] == "is_empty"

    def test_comparator_in_post(self):
        spec = _infer_filter_from_context("stage 1", "sf.Stage1__c", "show sites where ", " > 5 in italy")
        assert spec is not None
        assert spec["operator"] == ">"
        assert spec["value"] == 5

    def test_is_available_suffix(self):
        spec = _infer_filter_from_context("hla", "sf.C_HLA_Typing_Available__c", "sites where ", " is available")
        assert spec is not None
        assert spec["operator"] == "is_not_empty"

    def test_no_context_returns_none(self):
        spec = _infer_filter_from_context("hla", "sf.C_HLA_Typing_Available__c", "tell me about ", " in germany")
        assert spec is None

    def test_having_prefix_returns_is_not_empty(self):
        spec = _infer_filter_from_context("hla", "sf.C_HLA_Typing_Available__c", "sites having ", "")
        assert spec is not None
        assert spec["operator"] == "is_not_empty"


# ══════════════════════════════════════════════════════════════════════════════
# 12. Phase 2 — _extract_catalog_filters
# ══════════════════════════════════════════════════════════════════════════════

_KINDEX_EXTENDED: dict = dict(_KINDEX, **{
    # Long aliases (≥5 chars) that the catalog extractor can match
    "hla typing":             {"source": "sf", "field": "C_HLA_Typing_Available__c", "label": "HLA Typing"},
    "principal investigator": {"source": "sf", "field": "C_PI_Name__c", "label": "Principal Investigator"},
})


class TestExtractCatalogFilters:
    def test_hla_with_context(self):
        filters, _ = _extract_catalog_filters("show sites with hla typing", _KINDEX_EXTENDED, set())
        hla = next((f for f in filters if "HLA" in f["field"]), None)
        assert hla is not None
        assert hla["operator"] == "is_not_empty"

    def test_pi_with_context(self):
        filters, _ = _extract_catalog_filters(
            "sites with principal investigator", _KINDEX_EXTENDED, set()
        )
        pi = next((f for f in filters if "PI_Name" in f["field"]), None)
        assert pi is not None

    def test_short_aliases_skipped(self):
        # "hla" (3 chars) and "pi" (2 chars) are below the 5-char threshold → no filter
        filters, _ = _extract_catalog_filters("show sites with hla and pi", _KINDEX, set())
        assert filters == []

    def test_skip_already_resolved_fields(self):
        # If hla field already in skip_fields, should not be added again
        skip = {"sf.C_HLA_Typing_Available__c"}
        filters, _ = _extract_catalog_filters("show sites with hla typing", _KINDEX_EXTENDED, skip)
        assert not any("HLA" in f["field"] for f in filters)

    def test_no_false_positive_without_context(self):
        # "hla typing" mentioned in a non-filter context → no filter produced
        filters, _ = _extract_catalog_filters("what is hla typing in general", _KINDEX_EXTENDED, set())
        # No "with / without / > N" context → should not produce a filter
        assert isinstance(filters, list)

    def test_catalog_never_produces_unresolved(self):
        _, unresolved = _extract_catalog_filters(
            "sites with hla typing and principal investigator", _KINDEX_EXTENDED, set()
        )
        assert unresolved == []


# ══════════════════════════════════════════════════════════════════════════════
# 13. Phase 2 — can_followup_merge and merge_with_last_filters
# ══════════════════════════════════════════════════════════════════════════════

class TestFollowupMerge:
    _LAST_IT = {
        "logic": "AND",
        "rules": [{"field": "site.country", "operator": "equals", "value": "IT"}],
    }
    _LAST_STAGE = {
        "logic": "AND",
        "rules": [{"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": 0}],
    }
    _LAST_MULTI = {
        "logic": "AND",
        "rules": [
            {"logic": "OR", "rules": [
                {"field": "site.country", "operator": "equals", "value": "IT"},
                {"field": "site.country", "operator": "equals", "value": "ES"},
            ]},
        ],
    }

    def _followup_plan(self, countries=None, filters=None, unresolved=None):
        return {
            "intent": "followup",
            "countries": countries or [],
            "filters": filters or [],
            "unresolved_terms": unresolved or [],
            "output_mode": "table",
            "needs_export": False,
            "requested_columns": [],
            "confidence": 0.60,
            "raw_text": "",
            "last_filters_reused": False,
        }

    def test_can_merge_new_country(self):
        p = self._followup_plan(countries=["FR"])
        assert can_followup_merge(p, self._LAST_IT) is True

    def test_cannot_merge_without_last_filters(self):
        p = self._followup_plan(countries=["FR"])
        assert can_followup_merge(p, None) is False

    def test_cannot_merge_with_unresolved(self):
        p = self._followup_plan(countries=["FR"], unresolved=["pharmacy (on-site or off-campus not specified)"])
        assert can_followup_merge(p, self._LAST_IT) is False

    def test_cannot_merge_non_followup_intent(self):
        p = self._followup_plan(countries=["FR"])
        p["intent"] = "table_query"
        assert can_followup_merge(p, self._LAST_IT) is False

    def test_cannot_merge_nothing_new(self):
        # No new countries or filters
        p = self._followup_plan()
        assert can_followup_merge(p, self._LAST_IT) is False

    def test_merge_adds_new_country_as_or_group(self):
        p = self._followup_plan(countries=["FR"])
        merged = merge_with_last_filters(p, self._LAST_IT)
        assert merged["logic"] == "AND"
        # Should have an OR sub-group for countries
        sub_groups = [r for r in merged["rules"] if isinstance(r.get("rules"), list)]
        assert sub_groups, "Expected OR sub-group for countries"
        iso2s = {r["value"] for r in sub_groups[0]["rules"]}
        assert "IT" in iso2s
        assert "FR" in iso2s

    def test_merge_deduplicates_existing_country(self):
        p = self._followup_plan(countries=["IT"])
        merged = merge_with_last_filters(p, self._LAST_IT)
        # IT already present — no duplicate
        all_country_values = []
        for r in merged["rules"]:
            if r.get("field") == "site.country":
                all_country_values.append(r["value"])
            for sr in r.get("rules", []):
                if sr.get("field") == "site.country":
                    all_country_values.append(sr["value"])
        assert all_country_values.count("IT") == 1

    def test_merge_adds_filter_to_country_result(self):
        stage_filter = {"field": "sf.C_Number_of_Stage2_Individuals_followed__c", "operator": ">", "value": 0, "raw_term": "stage 2"}
        p = self._followup_plan(countries=["FR"], filters=[stage_filter])
        merged = merge_with_last_filters(p, self._LAST_IT)
        stage_rules = [r for r in merged["rules"] if isinstance(r.get("field"), str) and "Stage2" in r["field"]]
        assert stage_rules, "Stage2 filter should be in merged result"

    def test_merge_into_existing_or_group(self):
        # last_filters already has a multi-country OR group; add ES
        p = self._followup_plan(countries=["DE"])
        merged = merge_with_last_filters(p, self._LAST_MULTI)
        sub_groups = [r for r in merged["rules"] if isinstance(r.get("rules"), list)]
        assert sub_groups
        iso2s = {r["value"] for r in sub_groups[0]["rules"]}
        assert "IT" in iso2s and "ES" in iso2s and "DE" in iso2s

    def test_merge_preserves_existing_non_country_rules(self):
        p = self._followup_plan(countries=["FR"])
        merged = merge_with_last_filters(p, self._LAST_STAGE)
        stage_rules = [r for r in merged["rules"] if isinstance(r.get("field"), str) and "Stage1" in r["field"]]
        assert stage_rules, "Existing stage1 rule should be preserved"


# ══════════════════════════════════════════════════════════════════════════════
# 14. Phase 2 — build_clarification
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildClarification:
    def _plan_with_unresolved(self, terms):
        return {
            "intent": "table_query",
            "countries": ["IT"],
            "filters": [],
            "unresolved_terms": terms,
            "output_mode": "table",
            "needs_export": False,
            "requested_columns": [],
            "confidence": 0.45,
            "raw_text": "show sites with a pharmacy in Italy",
            "last_filters_reused": False,
        }

    def test_pharmacy_returns_clarify(self):
        p = self._plan_with_unresolved(["pharmacy (on-site or off-campus not specified)"])
        result = build_clarification(p)
        assert result is not None
        assert "clarify" in result
        assert "options" in result["clarify"]
        assert len(result["clarify"]["options"]) >= 2

    def test_no_unresolved_returns_none(self):
        p = self._plan_with_unresolved([])
        assert build_clarification(p) is None

    def test_unknown_unresolved_returns_none(self):
        p = self._plan_with_unresolved(["unknown field xyz"])
        assert build_clarification(p) is None

    def test_clarify_preserves_plan_countries(self):
        p = self._plan_with_unresolved(["pharmacy (on-site or off-campus not specified)"])
        result = build_clarification(p)
        assert result["clarify"]["plan_countries"] == ["IT"]

    def test_clarify_options_have_label_and_query(self):
        tmpl = CLARIFICATION_TEMPLATES["pharmacy (on-site or off-campus not specified)"]
        for opt in tmpl["options"]:
            assert "label" in opt
            assert "query" in opt


# ── Phase 4: validate_filter_group ─────────────────────────────────────────────

class TestValidateFilterGroup:
    """Tests for validate_filter_group() — pure validation, no network."""

    # ── Valid cases ────────────────────────────────────────────────────────────

    def test_valid_simple_and(self):
        fg = {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": "ES"},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_simple_or(self):
        fg = {"logic": "OR", "rules": [
            {"field": "site.country", "operator": "equals", "value": "ES"},
            {"field": "site.country", "operator": "equals", "value": "IT"},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_nested_or_group(self):
        """Multi-country OR sub-group inside AND — canonical Moby format."""
        fg = {"logic": "AND", "rules": [
            {"logic": "OR", "rules": [
                {"field": "site.country", "operator": "equals", "value": "ES"},
                {"field": "site.country", "operator": "equals", "value": "IT"},
            ]},
            {"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes"},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_complex_grouped(self):
        """(DE AND overnight) OR (FR AND is_not_empty) — Phase 4 target format."""
        fg = {"logic": "OR", "rules": [
            {"logic": "AND", "rules": [
                {"field": "site.country", "operator": "equals", "value": "DE"},
                {"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes"},
            ]},
            {"logic": "AND", "rules": [
                {"field": "site.country", "operator": "equals", "value": "FR"},
                {"field": "qual.3_5_2__overnight_stay", "operator": "is_not_empty"},
            ]},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_expression_logic(self):
        """Numbered expression: '1 AND (2 OR 3)'."""
        fg = {"logic": "1 AND (2 OR 3)", "rules": [
            {"field": "site.country", "operator": "equals", "value": "ES"},
            {"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes"},
            {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": 0},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_sf_field(self):
        fg = {"logic": "AND", "rules": [
            {"field": "sf.C_Number_of_Stage2_Individuals_followed__c", "operator": ">", "value": 10},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_qual_field(self):
        fg = {"logic": "AND", "rules": [
            {"field": "qual.3_6__is_your_pharmacy_on_site_or_off_campus", "operator": "equals", "value": "On-site"},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_is_empty_no_value_required(self):
        """is_empty and is_not_empty don't need a value."""
        fg = {"logic": "AND", "rules": [
            {"field": "qual.3_5_2__overnight_stay", "operator": "is_empty"},
            {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": "is_not_empty"},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_between_operator(self):
        fg = {"logic": "AND", "rules": [
            {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": "between", "value": [10, 50]},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_in_operator(self):
        fg = {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "in", "value": ["ES", "IT", "FR"]},
        ]}
        assert validate_filter_group(fg) == []

    def test_valid_empty_rules(self):
        """Empty rules list is valid (no-op filter)."""
        fg = {"logic": "AND", "rules": []}
        assert validate_filter_group(fg) == []

    # ── Invalid cases ──────────────────────────────────────────────────────────

    def test_invalid_not_a_dict(self):
        errors = validate_filter_group("bad")
        assert len(errors) >= 1
        assert "dict" in errors[0]

    def test_invalid_logic_string(self):
        fg = {"logic": "MAYBE", "rules": []}
        errors = validate_filter_group(fg)
        assert len(errors) >= 1
        assert any("logic" in e for e in errors)

    def test_invalid_unknown_field_prefix(self):
        fg = {"logic": "AND", "rules": [
            {"field": "badprefix.SomeField", "operator": "equals", "value": "x"},
        ]}
        errors = validate_filter_group(fg)
        assert len(errors) >= 1
        assert any("badprefix" in e or "prefix" in e for e in errors)

    def test_invalid_unknown_operator(self):
        fg = {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "LIKE", "value": "Spain"},
        ]}
        errors = validate_filter_group(fg)
        assert len(errors) >= 1
        assert any("LIKE" in e or "operator" in e for e in errors)

    def test_invalid_missing_field(self):
        fg = {"logic": "AND", "rules": [
            {"operator": "equals", "value": "ES"},
        ]}
        errors = validate_filter_group(fg)
        assert any("field" in e for e in errors)

    def test_invalid_missing_operator(self):
        fg = {"logic": "AND", "rules": [
            {"field": "site.country", "value": "ES"},
        ]}
        errors = validate_filter_group(fg)
        assert any("operator" in e for e in errors)

    def test_invalid_empty_value_for_equals(self):
        fg = {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": ""},
        ]}
        errors = validate_filter_group(fg)
        assert any("empty" in e or "value" in e for e in errors)

    def test_invalid_null_value_for_gt(self):
        fg = {"logic": "AND", "rules": [
            {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": None},
        ]}
        errors = validate_filter_group(fg)
        assert any("value" in e for e in errors)

    def test_invalid_nested_has_errors(self):
        fg = {"logic": "AND", "rules": [
            {"logic": "OR", "rules": [
                {"field": "BADFIELD", "operator": "equals", "value": "x"},
            ]},
        ]}
        errors = validate_filter_group(fg)
        assert len(errors) >= 1

    def test_invalid_too_deeply_nested(self):
        # Build depth-6 nesting
        innermost = {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": "ES"}
        ]}
        fg = innermost
        for _ in range(6):
            fg = {"logic": "AND", "rules": [fg]}
        errors = validate_filter_group(fg)
        assert any("nested" in e or "depth" in e for e in errors)

    # ── plan_to_filter_group produces valid output ─────────────────────────────

    def test_plan_to_filter_group_single_country_valid(self):
        from app.routers.moby_planner import MobyPlan
        plan: MobyPlan = {
            "intent": "table_query", "countries": ["ES"], "filters": [],
            "requested_columns": [], "output_mode": "table", "needs_export": False,
            "unresolved_terms": [], "confidence": 0.9, "raw_text": "", "last_filters_reused": False,
        }
        fg = plan_to_filter_group(plan)
        assert validate_filter_group(fg) == []

    def test_plan_to_filter_group_multi_country_valid(self):
        from app.routers.moby_planner import MobyPlan
        plan: MobyPlan = {
            "intent": "table_query", "countries": ["ES", "IT", "FR"], "filters": [
                {"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes", "raw_term": "overnight"},
            ],
            "requested_columns": [], "output_mode": "table", "needs_export": False,
            "unresolved_terms": [], "confidence": 0.85, "raw_text": "", "last_filters_reused": False,
        }
        fg = plan_to_filter_group(plan)
        assert validate_filter_group(fg) == [], f"Errors: {validate_filter_group(fg)}"
