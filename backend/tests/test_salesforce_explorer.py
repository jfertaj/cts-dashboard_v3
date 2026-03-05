"""
Unit tests for salesforce_explorer.py pure functions.
No DB, no network, no FastAPI — just Python logic.
Run: pytest backend/tests/test_salesforce_explorer.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.salesforce_explorer import _eval_qual_rule, _qual_get


# ──────────────────────────────────────────────────────────────────────────────
# _eval_qual_rule
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalQualRule:
    # Null checks
    def test_is_null_none(self):          assert _eval_qual_rule(None, "is_null", None) is True
    def test_is_null_empty_str(self):     assert _eval_qual_rule("", "is_null", None) is True
    def test_is_null_blank(self):         assert _eval_qual_rule("  ", "is_null", None) is True
    def test_is_null_with_value(self):    assert _eval_qual_rule(5, "is_null", None) is False
    def test_is_not_null_with_value(self):assert _eval_qual_rule(5, "is_not_null", None) is True
    def test_is_not_null_none(self):      assert _eval_qual_rule(None, "is_not_null", None) is False

    # Numeric comparisons
    def test_gt_pass(self):               assert _eval_qual_rule(10, "gt", 5) is True
    def test_gt_fail(self):               assert _eval_qual_rule(3, "gt", 5) is False
    def test_gt_equal(self):              assert _eval_qual_rule(5, "gt", 5) is False
    def test_gte_equal(self):             assert _eval_qual_rule(5, "gte", 5) is True
    def test_lt_pass(self):               assert _eval_qual_rule(2, "lt", 5) is True
    def test_lte_equal(self):             assert _eval_qual_rule(5, "lte", 5) is True
    def test_equals_numeric(self):        assert _eval_qual_rule(5, "equals", 5) is True
    def test_equals_numeric_fail(self):   assert _eval_qual_rule(5, "equals", 6) is False
    def test_not_equals(self):            assert _eval_qual_rule(5, "not_equals", 6) is True

    # Operator aliases
    def test_eq_alias(self):              assert _eval_qual_rule(3, "=", 3) is True
    def test_ne_alias(self):              assert _eval_qual_rule(3, "!=", 4) is True
    def test_gt_symbol(self):             assert _eval_qual_rule(10, ">", 5) is True
    def test_gte_symbol(self):            assert _eval_qual_rule(5, ">=", 5) is True
    def test_lt_symbol(self):             assert _eval_qual_rule(2, "<", 5) is True
    def test_lte_symbol(self):            assert _eval_qual_rule(5, "<=", 5) is True

    # String coercion (qual values often come as strings from JSONB)
    def test_gt_string_coercion(self):    assert _eval_qual_rule("10", "gt", "5") is True
    def test_equals_string_int(self):     assert _eval_qual_rule("5", "=", 5) is True

    # Between
    def test_between_pass(self):          assert _eval_qual_rule(5, "between", [1, 10]) is True
    def test_between_boundary(self):      assert _eval_qual_rule(1, "between", [1, 10]) is True
    def test_between_fail(self):          assert _eval_qual_rule(11, "between", [1, 10]) is False
    def test_between_none(self):          assert _eval_qual_rule(None, "between", [1, 10]) is False
    def test_between_dotdot_str(self):    assert _eval_qual_rule(5, "between", "1..10") is True

    # In / not_in
    def test_in_present(self):            assert _eval_qual_rule("Yes", "in", ["Yes", "No"]) is True
    def test_in_absent(self):             assert _eval_qual_rule("Maybe", "in", ["Yes", "No"]) is False
    def test_not_in_absent(self):         assert _eval_qual_rule("Maybe", "not_in", ["Yes", "No"]) is True
    def test_not_in_present(self):        assert _eval_qual_rule("Yes", "not_in", ["Yes", "No"]) is False

    # String operations
    def test_contains_pass(self):         assert _eval_qual_rule("On-site pharmacy", "contains", "pharmacy") is True
    def test_contains_case_insensitive(self): assert _eval_qual_rule("On-site Pharmacy", "contains", "pharmacy") is True
    def test_contains_fail(self):         assert _eval_qual_rule("No pharmacy", "not_contains", "pharmacy") is False
    def test_not_contains_pass(self):     assert _eval_qual_rule("No facility", "not_contains", "pharmacy") is True
    def test_starts_with(self):           assert _eval_qual_rule("On-site", "starts_with", "on") is True
    def test_ends_with(self):             assert _eval_qual_rule("On-site", "ends_with", "site") is True

    # Empty / not_empty
    def test_is_empty_none(self):         assert _eval_qual_rule(None, "is_empty", None) is True
    def test_is_empty_blank(self):        assert _eval_qual_rule("   ", "is_empty", None) is True
    def test_is_not_empty_value(self):    assert _eval_qual_rule("Yes", "is_not_empty", None) is True
    def test_is_not_empty_none(self):     assert _eval_qual_rule(None, "is_not_empty", None) is False

    # Unknown operator → permissive (passes)
    def test_unknown_op(self):            assert _eval_qual_rule(5, "unknown_op", 3) is True


# ──────────────────────────────────────────────────────────────────────────────
# _qual_get — 3-fallback JSONB key lookup
# ──────────────────────────────────────────────────────────────────────────────

class TestQualGet:
    def test_exact_key(self):
        d = {"personal_conversation_with_physician": "Yes"}
        assert _qual_get(d, "personal_conversation_with_physician") == "Yes"

    def test_section_prefixed_key_exact(self):
        d = {"2_2__personal_conversation_with_physician": "Yes"}
        assert _qual_get(d, "2_2__personal_conversation_with_physician") == "Yes"

    def test_fallback_dot_to_underscore(self):
        # key stored with dot subcode but looked up with underscore
        d = {"2.2__personal_conversation_with_physician": "Yes"}
        assert _qual_get(d, "2_2__personal_conversation_with_physician") == "Yes"

    def test_fallback_strip_prefix(self):
        # Stored as base key; looked up with section prefix
        d = {"personal_conversation_with_physician": "Yes"}
        assert _qual_get(d, "2_2__personal_conversation_with_physician") == "Yes"

    def test_missing_key_returns_none(self):
        d = {"other_key": "value"}
        assert _qual_get(d, "2_2__personal_conversation_with_physician") is None

    def test_empty_dict(self):
        assert _qual_get({}, "any_key") is None

    def test_exact_takes_priority(self):
        # Both exact and base key exist — exact wins
        d = {
            "2_2__pharmacy": "On-site",
            "pharmacy": "Off-site",
        }
        assert _qual_get(d, "2_2__pharmacy") == "On-site"

    def test_numeric_zero_value(self):
        # 0 is a valid value, must not be treated as None
        d = {"ongoing_trials": 0}
        assert _qual_get(d, "ongoing_trials") == 0
