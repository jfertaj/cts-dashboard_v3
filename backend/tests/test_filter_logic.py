"""
Unit tests for the filter logic expression evaluator in salesforce_explorer.py.
No DB, no network — pure Python logic.
Run: pytest backend/tests/test_filter_logic.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.salesforce_explorer import _is_logic_expr, _eval_logic_expr_be


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
