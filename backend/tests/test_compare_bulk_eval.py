# backend/tests/test_compare_bulk_eval.py
"""Tests for scripts/compare_bulk_eval.py — 7 classification rules."""
import sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from compare_bulk_eval import classify  # noqa: E402


def _raw(status="OK", rows=10, has_table=True):
    return {"status": status, "rows": rows, "has_table": has_table, "answer_preview": ""}


def _base(cat, observed="10 rows", output_type="table"):
    return {"category": cat, "observed_behavior": observed, "output_type": output_type}


def test_pass_now_preserved_same_row_count():
    assert classify(_base("PASS_NOW"), _raw(rows=10)) == "PRESERVED"


def test_pass_now_regression_status_flip():
    assert classify(_base("PASS_NOW"), _raw(status="ERROR", rows=0)) == "REGRESSION"


def test_pass_now_regression_row_drop_order_of_magnitude():
    # baseline says "79 rows", new returns 3 rows → >1 order of magnitude drop
    b = _base("PASS_NOW", observed="79 rows, 3 countries correctly resolved")
    assert classify(b, _raw(rows=3)) == "REGRESSION"


def test_pass_now_not_regression_small_drop():
    # baseline says "79 rows", new returns 77 rows → normal variance
    b = _base("PASS_NOW", observed="79 rows, 3 countries correctly resolved")
    assert classify(b, _raw(rows=77)) == "PRESERVED"


def test_known_gap_candidate_improvement():
    assert classify(_base("KNOWN_GAP"), _raw(status="OK", rows=10, has_table=True)) == "CANDIDATE_IMPROVEMENT"


def test_known_gap_unchanged_still_failing():
    assert classify(_base("KNOWN_GAP"), _raw(status="ERROR", rows=0, has_table=False)) == "UNCHANGED"


def test_future_feature_always_unchanged():
    assert classify(_base("FUTURE_FEATURE"), _raw(status="OK", rows=5)) == "UNCHANGED"


def test_ambiguous_always_skipped():
    assert classify(_base("AMBIGUOUS"), _raw(status="OK", rows=5)) == "SKIPPED"


def test_missing_raw_returns_missing():
    assert classify(_base("PASS_NOW"), None) == "MISSING"
