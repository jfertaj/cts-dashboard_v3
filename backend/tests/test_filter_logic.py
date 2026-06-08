"""
Unit tests for the filter logic expression evaluator in salesforce_explorer.py.
No DB, no network — pure Python logic.
Run: pytest backend/tests/test_filter_logic.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.filter_engine import (
    _is_logic_expr, _eval_logic_expr_be, _filter_group_to_expr, _eval_qual_rule,
)


# ──────────────────────────────────────────────────────────────────────────────
# _is_logic_expr
# ──────────────────────────────────────────────────────────────────────────────

class TestIsLogicExpr:
    def test_simple_and(self):     assert _is_logic_expr("AND") is False
    def test_simple_or(self):      assert _is_logic_expr("OR") is False
    def test_empty(self):          assert _is_logic_expr("") is False
    def test_expr_1_and_2(self):   assert _is_logic_expr("1 AND 2") is True
    def test_expr_paren(self):     assert _is_logic_expr("(1 AND 2) OR 3") is True
    def test_single_num(self):     assert _is_logic_expr("1") is True


# ──────────────────────────────────────────────────────────────────────────────
# _eval_logic_expr_be — basic operators
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalLogicExprBasic:
    def test_single_true(self):
        assert _eval_logic_expr_be("1", {1: True}) is True

    def test_single_false(self):
        assert _eval_logic_expr_be("1", {1: False}) is False

    def test_and_both_true(self):
        assert _eval_logic_expr_be("1 AND 2", {1: True, 2: True}) is True

    def test_and_one_false(self):
        assert _eval_logic_expr_be("1 AND 2", {1: True, 2: False}) is False

    def test_or_one_true(self):
        assert _eval_logic_expr_be("1 OR 2", {1: False, 2: True}) is True

    def test_or_both_false(self):
        assert _eval_logic_expr_be("1 OR 2", {1: False, 2: False}) is False

    def test_three_rules_and(self):
        assert _eval_logic_expr_be("1 AND 2 AND 3", {1: True, 2: True, 3: True}) is True
        assert _eval_logic_expr_be("1 AND 2 AND 3", {1: True, 2: False, 3: True}) is False

    def test_three_rules_or(self):
        assert _eval_logic_expr_be("1 OR 2 OR 3", {1: False, 2: False, 3: True}) is True


# ──────────────────────────────────────────────────────────────────────────────
# _eval_logic_expr_be — parentheses and precedence
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalLogicExprParens:
    def test_paren_and_or(self):
        # (1 AND 2) OR 3 — rule 3 true → whole thing true
        assert _eval_logic_expr_be("(1 AND 2) OR 3", {1: False, 2: False, 3: True}) is True

    def test_paren_and_or_all_false(self):
        assert _eval_logic_expr_be("(1 AND 2) OR 3", {1: True, 2: False, 3: False}) is False

    def test_and_paren_or(self):
        # 1 AND (2 OR 3) — rule 1 false → false regardless
        assert _eval_logic_expr_be("1 AND (2 OR 3)", {1: False, 2: True, 3: True}) is False

    def test_and_paren_or_true(self):
        assert _eval_logic_expr_be("1 AND (2 OR 3)", {1: True, 2: False, 3: True}) is True

    def test_two_groups(self):
        # (1 AND 2) OR (3 AND 4)
        assert _eval_logic_expr_be("(1 AND 2) OR (3 AND 4)", {1: True, 2: True, 3: False, 4: False}) is True
        assert _eval_logic_expr_be("(1 AND 2) OR (3 AND 4)", {1: True, 2: False, 3: False, 4: True}) is False

    def test_nested(self):
        # ((1 OR 2) AND 3) OR (4 AND 5)
        expr = "((1 OR 2) AND 3) OR (4 AND 5)"
        # rule 1 true, 3 true → (T OR F) AND T = T → whole expr T
        assert _eval_logic_expr_be(expr, {1: True, 2: False, 3: True, 4: False, 5: False}) is True
        # none in first group: 1 false, 2 false → F AND T = F; 4 true, 5 true → T
        assert _eval_logic_expr_be(expr, {1: False, 2: False, 3: True, 4: True, 5: True}) is True
        # all false
        assert _eval_logic_expr_be(expr, {1: False, 2: False, 3: False, 4: False, 5: False}) is False

    def test_five_rules_complex(self):
        # 1 AND (2 OR 3) AND (4 OR 5)
        expr = "1 AND (2 OR 3) AND (4 OR 5)"
        assert _eval_logic_expr_be(expr, {1: True, 2: True, 3: False, 4: False, 5: True}) is True
        assert _eval_logic_expr_be(expr, {1: False, 2: True, 3: True, 4: True, 5: True}) is False


# ──────────────────────────────────────────────────────────────────────────────
# _eval_logic_expr_be — unknown indices default to True
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalLogicExprUnknown:
    def test_unknown_defaults_true_in_and(self):
        # Only site rules known; SF rule (2) unknown → True
        assert _eval_logic_expr_be("1 AND 2", {1: True}) is True

    def test_unknown_defaults_true_in_or(self):
        # Rule 2 unknown → True → whole OR is True
        assert _eval_logic_expr_be("1 OR 2", {1: False}) is True

    def test_known_false_unknown_true(self):
        # (1 AND 2) where 1=False, 2=unknown(True) → False
        assert _eval_logic_expr_be("(1 AND 2) OR 3", {1: False, 3: True}) is True


# ──────────────────────────────────────────────────────────────────────────────
# _eval_logic_expr_be — edge cases
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalLogicExprEdge:
    def test_empty_results_fails_open(self):
        # If expression fails to parse, should return True (fail open)
        assert _eval_logic_expr_be("", {}) is True

    def test_precedence_and_before_or(self):
        # 1 OR 2 AND 3 → 1 OR (2 AND 3) by precedence
        # 1=False, 2=True, 3=True → False OR True = True
        assert _eval_logic_expr_be("1 OR 2 AND 3", {1: False, 2: True, 3: True}) is True
        # 1=False, 2=True, 3=False → False OR False = False
        assert _eval_logic_expr_be("1 OR 2 AND 3", {1: False, 2: True, 3: False}) is False

    def test_lowercase_operators(self):
        assert _eval_logic_expr_be("1 and 2", {1: True, 2: True}) is True
        assert _eval_logic_expr_be("1 or 2", {1: False, 2: True}) is True


# ──────────────────────────────────────────────────────────────────────────────
# _filter_group_to_expr — nested group → expression conversion
# ──────────────────────────────────────────────────────────────────────────────

_R = lambda field: {"field": field, "operator": "equals", "value": "x"}


class TestFilterGroupToExpr:
    def test_flat_and_unchanged(self):
        """Simple AND with no sub-groups: returns as-is."""
        fg = {"logic": "AND", "rules": [_R("site.country"), _R("qual.overnight")]}
        result = _filter_group_to_expr(fg)
        assert result is fg  # same object, no conversion

    def test_flat_or_unchanged(self):
        fg = {"logic": "OR", "rules": [_R("site.country"), _R("site.country")]}
        result = _filter_group_to_expr(fg)
        assert result is fg

    def test_already_expression_unchanged(self):
        fg = {"logic": "1 AND 2", "rules": [_R("site.country"), _R("qual.overnight")]}
        result = _filter_group_to_expr(fg)
        assert result is fg

    def test_nested_or_inside_and(self):
        """(ES OR IT) AND overnight → (1 OR 2) AND 3"""
        or_group = {"logic": "OR", "rules": [_R("site.country"), _R("site.country")]}
        overnight = _R("qual.overnight")
        fg = {"logic": "AND", "rules": [or_group, overnight]}
        result = _filter_group_to_expr(fg)
        assert result["logic"] == "(1 OR 2) AND 3"
        assert len(result["rules"]) == 3
        assert result["rules"][2] is overnight

    def test_or_of_ands(self):
        """(DE AND overnight) OR (FR AND overnight) → (1 AND 2) OR (3 AND 4)"""
        de_and = {"logic": "AND", "rules": [_R("site.country"), _R("qual.overnight")]}
        fr_and = {"logic": "AND", "rules": [_R("site.country"), _R("qual.overnight")]}
        fg = {"logic": "OR", "rules": [de_and, fr_and]}
        result = _filter_group_to_expr(fg)
        assert result["logic"] == "(1 AND 2) OR (3 AND 4)"
        assert len(result["rules"]) == 4

    def test_mixed_leaf_and_subgroup(self):
        """leaf AND (sub1 OR sub2) AND leaf2 → 1 AND (2 OR 3) AND 4"""
        leaf1 = _R("sf.SomeField")
        or_group = {"logic": "OR", "rules": [_R("site.country"), _R("site.country")]}
        leaf2 = _R("qual.overnight")
        fg = {"logic": "AND", "rules": [leaf1, or_group, leaf2]}
        result = _filter_group_to_expr(fg)
        assert result["logic"] == "1 AND (2 OR 3) AND 4"
        assert len(result["rules"]) == 4
        assert result["rules"][0] is leaf1
        assert result["rules"][3] is leaf2


# ──────────────────────────────────────────────────────────────────────────────
# _eval_qual_rule — affirmative/negative ("Yes" vs True/yes/Y/X) robustness.
# The LLM emits `equals "Yes"` but qual JSONB may store the boolean-ish value as
# True, "yes", "Y", or "X" (Excel-origin checkboxes). These must all match so a
# correct filter doesn't silently return 0 rows.
# ──────────────────────────────────────────────────────────────────────────────
class TestQualBooleanMatch:
    @pytest.mark.parametrize("stored", ["Yes", "yes", "YES", "Y", "y", True, "x", "X", "true", "checked", "✓"])
    def test_equals_yes_matches_affirmative_representations(self, stored):
        assert _eval_qual_rule(stored, "equals", "Yes") is True

    @pytest.mark.parametrize("stored", ["No", "no", "N", False, "false", "off", None, ""])
    def test_equals_yes_does_not_match_negative_or_empty(self, stored):
        assert _eval_qual_rule(stored, "equals", "Yes") is False

    @pytest.mark.parametrize("stored", ["No", False, "n"])
    def test_not_equals_yes_true_for_negatives(self, stored):
        assert _eval_qual_rule(stored, "not_equals", "Yes") is True

    def test_in_yes_matches_affirmative(self):
        assert _eval_qual_rule(True, "in", ["Yes"]) is True
        assert _eval_qual_rule("yes", "in", "Yes") is True

    # Guardrails: numeric and free-text comparisons must NOT be hijacked by the
    # boolean normalization.
    def test_numeric_equals_unaffected(self):
        assert _eval_qual_rule(5, "equals", "5") is True
        assert _eval_qual_rule(1, "equals", "1") is True       # 1 is NOT treated as affirmative
        assert _eval_qual_rule("yes", "equals", "1") is False  # affirmative word != numeric 1

    def test_freetext_equals_unaffected(self):
        assert _eval_qual_rule("Both", "equals", "Both") is True
        assert _eval_qual_rule("Both", "equals", "Yes") is False

    def test_gt_unaffected(self):
        assert _eval_qual_rule(30, "gt", "20") is True
        assert _eval_qual_rule(10, "gt", "20") is False
