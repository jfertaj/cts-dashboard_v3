"""
Unit tests for salesforce_explorer.py pure functions.
No DB, no network, no FastAPI — just Python logic.
Run: pytest backend/tests/test_salesforce_explorer.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.filter_engine import _eval_qual_rule, _qual_get


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


# ──────────────────────────────────────────────────────────────────────────────
# _qual_get — reverse fallback: bare key → unambiguous section-prefixed stored key
# (fix/moby-qual-key-resolution). The knowledge-index alias path historically
# emitted bare keys (e.g. "overnight_stay") that never resolved because the JSONB
# stores section-prefixed keys ("3_5_2__overnight_stay"). The reverse fallback
# resolves a bare key ONLY when exactly one stored key has it as the suffix after
# "__"; ambiguous matches must NOT silently pick a field.
# ──────────────────────────────────────────────────────────────────────────────

class TestQualGetReverseFallback:
    def test_bare_overnight_resolves_to_unique_section_key(self):
        # Only one stored key ends in "__overnight_stay" → resolves
        d = {"3_5_2__overnight_stay": "Yes"}
        assert _qual_get(d, "overnight_stay") == "Yes"

    def test_bare_hla_resolves_to_unique_section_key(self):
        d = {"3_8__can_you_do_hla_typing": "No"}
        # Bare alias that already strips/contains the suffix exactly
        assert _qual_get(d, "can_you_do_hla_typing") == "No"

    def test_bare_key_ambiguous_returns_none(self):
        # TWO stored keys share the suffix "__overnight_stay" → must NOT guess.
        d = {
            "3_5__overnight_stay": "No",     # a DIFFERENT field (36 sites on prod)
            "3_5_2__overnight_stay": "Yes",  # the intended field (48 sites on prod)
        }
        assert _qual_get(d, "overnight_stay") is None

    def test_reverse_fallback_does_not_override_exact(self):
        # Exact key still wins over any reverse suffix match.
        d = {
            "overnight_stay": "Exact",
            "3_5_2__overnight_stay": "Prefixed",
        }
        assert _qual_get(d, "overnight_stay") == "Exact"

    def test_reverse_fallback_dot_subcode_stored(self):
        # Stored with dotted subcode; bare lookup still resolves uniquely.
        d = {"3.5.2__overnight_stay": "Yes"}
        assert _qual_get(d, "overnight_stay") == "Yes"

    def test_reverse_fallback_no_match_returns_none(self):
        d = {"3_8__can_you_do_hla_typing": "Yes"}
        assert _qual_get(d, "overnight_stay") is None

    def test_section_prefixed_lookup_unaffected_by_reverse(self):
        # The normal forward path (prefixed lookup → stored prefixed) is untouched.
        d = {"3_5_2__overnight_stay": "Yes"}
        assert _qual_get(d, "3_5_2__overnight_stay") == "Yes"


# ──────────────────────────────────────────────────────────────────────────────
# pass_account expression-mode (regression for bug fixed 2026-03-18)
# Verify that account rules respect expression logic strings, not just AND/OR.
# We test this via explorer_search's internal pass_account by calling it through
# a lightweight integration path using the filter_engine helpers directly.
# ──────────────────────────────────────────────────────────────────────────────

from app.routers.filter_engine import _eval_qual_rule, _is_logic_expr, _eval_logic_expr_be


class TestPassAccountExpressionMode:
    """Unit-test the fixed pass_account logic in isolation."""

    def _pass_account_fixed(self, account_rules, account_rule_indices,
                             vals_by_aid, aid, logic_str):
        """Mirror the fixed pass_account() logic without importing the full endpoint."""
        from app.routers.filter_engine import _eval_qual_rule, _is_logic_expr, _eval_logic_expr_be
        if not account_rules:
            return True
        vals = dict(vals_by_aid.get(str(aid), {}))
        vals.setdefault("Id", str(aid))
        res = [_eval_qual_rule(vals.get(ar["field"]), ar["operator"], ar["value"])
               for ar in account_rules]
        if _is_logic_expr(logic_str):
            expr_results = {account_rule_indices[i]: res[i] for i in range(len(account_rules))}
            return _eval_logic_expr_be(logic_str, expr_results)
        glue_and = logic_str == "AND"
        return all(res) if glue_and else any(res)

    def test_expression_1_and_2_both_pass(self):
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]  # global rule indices
        vals = {"Name": "Acme", "BillingCity": "Berlin"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "1 AND 2") is True

    def test_expression_1_and_2_first_fails(self):
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]
        vals = {"Name": "Other", "BillingCity": "Berlin"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "1 AND 2") is False

    def test_expression_1_or_2_first_passes(self):
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]
        vals = {"Name": "Acme", "BillingCity": "Paris"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "1 OR 2") is True

    def test_expression_with_nested_parens(self):
        # "(1 OR 2) AND 3" — rules at global positions 1, 2, 3
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "Name", "operator": "equals", "value": "Beta"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2, 3]
        vals = {"Name": "Beta", "BillingCity": "Berlin"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "(1 OR 2) AND 3") is True

    def test_expression_old_bug_and_treated_as_or(self):
        """Before fix, ANY expression string was treated as OR (== "AND" is False).
        After fix, "1 AND 2" is correctly evaluated as AND (both must pass).
        This test fails on the old code and passes on the fixed code."""
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]
        # Only name passes, city fails
        vals = {"Name": "Acme", "BillingCity": "Paris"}
        # With old bug: "1 AND 2" != "AND" → glue_and=False → OR → True (WRONG)
        # With fix: expression evaluated → 1=True AND 2=False → False (CORRECT)
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "1 AND 2") is False

    def test_no_rules_returns_true(self):
        assert self._pass_account_fixed([], [], {}, "acc1", "AND") is True

    def test_plain_and_logic_still_works(self):
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]
        vals = {"Name": "Acme", "BillingCity": "Berlin"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "AND") is True

    def test_plain_or_logic_still_works(self):
        rules = [
            {"field": "Name", "operator": "equals", "value": "Acme"},
            {"field": "BillingCity", "operator": "equals", "value": "Berlin"},
        ]
        indices = [1, 2]
        vals = {"Name": "Other", "BillingCity": "Berlin"}
        assert self._pass_account_fixed(rules, indices, {"acc1": vals}, "acc1", "OR") is True


# ──────────────────────────────────────────────────────────────────────────────
# _build_sf_where — SOQL builder (regression for bug fixed 2026-04-20)
# Salesforce SOQL does NOT accept `field NOT LIKE 'x'`. Must use `NOT (field LIKE 'x')`.
# ──────────────────────────────────────────────────────────────────────────────

from app.routers.filter_engine import Rule, FilterQuery
from app.routers.salesforce_explorer import _build_sf_where
import app.routers.salesforce_explorer as _sfx_for_build_tests


@pytest.fixture(autouse=True)
def _build_sf_where_permissive(request):
    """
    Tests in this file use synthetic Account.* fields (e.g. CTU_Status__c)
    that aren't in MIN_ALLOWED. `_safe_field` now validates Account.* against
    the real describe, so without permissive mode those tests fail. Mirror
    prod's boot-time permissive fallback for the duration of each test.
    """
    if request.node.get_closest_marker("no_permissive"):
        yield
        return
    _orig_perm = _sfx_for_build_tests._DESCRIBE_PERMISSIVE
    _sfx_for_build_tests._DESCRIBE_PERMISSIVE = True
    try:
        yield
    finally:
        _sfx_for_build_tests._DESCRIBE_PERMISSIVE = _orig_perm


class TestBuildSfWhereNotContains:
    def test_not_contains_uses_logical_not_not_raw_not_like(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.CTU_Status__c", operator="not_contains", value="profilation")
        ])
        out = _build_sf_where(q)
        assert "NOT LIKE" not in out, (
            f"SOQL must not contain raw 'NOT LIKE' (invalid syntax). Got: {out}"
        )
        assert "NOT Account.CTU_Status__c LIKE '%profilation%'" in out

    def test_not_contains_includes_null_safety(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.CTU_Status__c", operator="not_contains", value="profilation")
        ])
        out = _build_sf_where(q)
        assert "Account.CTU_Status__c = null" in out, (
            f"NULL records should match 'not_contains'. Got: {out}"
        )

    def test_contains_still_uses_plain_like(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.Name", operator="contains", value="foo")
        ])
        out = _build_sf_where(q)
        assert "Account.Name LIKE '%foo%'" in out
        assert "NOT" not in out


# ──────────────────────────────────────────────────────────────────────────────
# SOQL LIKE-value escaping (regression 2026-04-20)
# A user value containing `'`, `%`, `\`, or `_` must be escaped before being
# embedded in a LIKE pattern, otherwise we either break the query or change its
# semantics (e.g. `%` matches everything).
# ──────────────────────────────────────────────────────────────────────────────

from app.utils.soql_helpers import soql_escape_like_value


class TestSoqlEscapeLikeValue:
    def test_single_quote_escaped(self):
        # "L'Oréal" must not break the query
        assert soql_escape_like_value("L'Oréal") == "L\\'Oréal"

    def test_percent_escaped(self):
        # user-typed '%' must not act as a wildcard
        assert soql_escape_like_value("50%") == "50\\%"

    def test_underscore_escaped(self):
        assert soql_escape_like_value("foo_bar") == "foo\\_bar"

    def test_backslash_escaped_first(self):
        # literal backslash must be doubled; it must happen BEFORE other escapes
        # so the inserted escape-backslashes aren't re-escaped.
        assert soql_escape_like_value("a\\b") == "a\\\\b"

    def test_combined(self):
        assert soql_escape_like_value("L'O%al_\\x") == "L\\'O\\%al\\_\\\\x"

    def test_empty_string(self):
        assert soql_escape_like_value("") == ""


class TestBuildSfWhereLikeEscaping:
    def test_contains_escapes_percent(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.Name", operator="contains", value="50%")
        ])
        out = _build_sf_where(q)
        # Expect '%50\%%' — the user's `%` is escaped, the wrapping `%` are wildcards
        assert "LIKE '%50\\%%'" in out, f"got: {out}"

    def test_contains_escapes_single_quote(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.Name", operator="contains", value="L'Oréal")
        ])
        out = _build_sf_where(q)
        # Must contain the escaped form \' (not a bare ')
        assert "L\\'Oréal" in out, f"got: {out}"
        assert "L'Oréal" not in out, f"unescaped value leaked into SOQL: {out}"

    def test_not_contains_escapes_percent(self):
        q = FilterQuery(logic="AND", rules=[
            Rule(field="Account.Name", operator="not_contains", value="50%")
        ])
        out = _build_sf_where(q)
        assert "50\\%" in out, f"got: {out}"


# ──────────────────────────────────────────────────────────────────────────────
# _safe_field Account.* validation (regression 2026-04-20)
# Before: any "Account.X" passed through without checking the SF Account
# describe, so typos reached SOQL and came back as a generic SF 400.
# After: we validate against _ACC_FIELD_SET (or pass through in permissive mode).
# ──────────────────────────────────────────────────────────────────────────────

import app.routers.salesforce_explorer as sfx
from fastapi import HTTPException as _HTTPException


class TestSafeFieldAccountValidation:
    def setup_method(self):
        # Capture originals
        self._orig_acc = sfx._ACC_FIELD_SET
        self._orig_perm = sfx._DESCRIBE_PERMISSIVE

    def teardown_method(self):
        sfx._ACC_FIELD_SET = self._orig_acc
        sfx._DESCRIBE_PERMISSIVE = self._orig_perm

    def test_valid_account_field_passes(self):
        sfx._ACC_FIELD_SET = {"Name", "CTU_Status__c"}
        sfx._DESCRIBE_PERMISSIVE = False
        assert sfx._safe_field("Account.CTU_Status__c") == "Account.CTU_Status__c"

    def test_unknown_account_field_raises_with_field_name(self):
        sfx._ACC_FIELD_SET = {"Name"}
        sfx._DESCRIBE_PERMISSIVE = False
        with pytest.raises(_HTTPException) as exc:
            sfx._safe_field("Account.BullingCity")  # typo of BillingCity
        assert "BullingCity" in exc.value.detail
        assert exc.value.status_code == 400

    def test_permissive_mode_accepts_unknown_account_field(self):
        # When SF describe failed at boot, we must stay permissive to avoid
        # locking users out of all Account.* filters.
        sfx._ACC_FIELD_SET = set()
        sfx._DESCRIBE_PERMISSIVE = True
        assert sfx._safe_field("Account.Whatever__c") == "Account.Whatever__c"


# ──────────────────────────────────────────────────────────────────────────────
# _column_key_to_sf_field — SELECT builder de /api/explorer/search (bug 2026-07-14)
#
# El frontend QUITA el prefijo "sf." antes del POST /search (lib/api.ts:347), pero
# el constructor del SELECT sólo aceptaba claves con "sf.". Resultado: ningún campo
# sf.* de Opportunity entraba en el SOQL y las ~360 claves volvían a null.
# El resolvedor debe aceptar AMBAS formas y seguir dejando que Account./qual./
# extra./site.* los sirva su propia rama del row builder.
# ──────────────────────────────────────────────────────────────────────────────

_SCREENED = "C_Number_of_Individuals_screened_intotal__c"


class TestColumnKeyToSfField:
    def setup_method(self):
        self._orig_opp = sfx._OPP_FIELD_SET
        self._orig_acc = sfx._ACC_FIELD_SET
        self._orig_perm = sfx._DESCRIBE_PERMISSIVE
        sfx._OPP_FIELD_SET = {"Id", "Name", "AccountId", _SCREENED}
        sfx._ACC_FIELD_SET = {"Id", "Name", "ShippingCountry"}
        sfx._DESCRIBE_PERMISSIVE = False

    def teardown_method(self):
        sfx._OPP_FIELD_SET = self._orig_opp
        sfx._ACC_FIELD_SET = self._orig_acc
        sfx._DESCRIBE_PERMISSIVE = self._orig_perm

    # -- forma prefijada (la que manda /columns/fill) --
    def test_prefixed_key_resolves_to_bare_field(self):
        assert sfx._column_key_to_sf_field(f"sf.{_SCREENED}") == _SCREENED

    # -- forma pelada (la que manda /search) --
    def test_bare_key_on_opportunity_resolves(self):
        assert sfx._column_key_to_sf_field(_SCREENED) == _SCREENED

    def test_bare_key_not_in_catalog_is_ignored(self):
        # "MemberName" es una columna virtual del frontend (sf.MemberName), no un
        # campo de Opportunity: si entra en el SELECT, el SOQL revienta con INVALID_FIELD.
        assert sfx._column_key_to_sf_field("MemberName") is None

    def test_bare_unknown_key_is_ignored_and_does_not_raise(self):
        assert sfx._column_key_to_sf_field("Totally_Made_Up__c") is None

    def test_bare_key_rejected_even_in_permissive_mode(self):
        # En modo permisivo _exists_on_opportunity() dice True a todo; el gate del
        # catálogo curado es lo único que impide que una clave basura llegue al SOQL.
        sfx._DESCRIBE_PERMISSIVE = True
        assert sfx._column_key_to_sf_field("MemberName") is None

    # -- las otras familias las sirve su propia rama: nunca van al SELECT --
    def test_account_key_is_not_an_opportunity_field(self):
        # Es lo que produce el strip de "sf.Account.Name" → lo sirve acc_map.
        assert sfx._column_key_to_sf_field("Account.Name") is None

    def test_qual_key_is_not_an_opportunity_field(self):
        assert sfx._column_key_to_sf_field("qual.2_2__personal_conversation") is None

    def test_extra_key_is_not_an_opportunity_field(self):
        assert sfx._column_key_to_sf_field("extra.AssignmentsNames") is None

    def test_site_key_is_not_an_opportunity_field(self):
        assert sfx._column_key_to_sf_field("site.country") is None

    # -- el prefijo "sf." sigue mandando sobre el resto (comportamiento de hoy) --
    def test_prefixed_account_key_keeps_its_relationship_form(self):
        assert sfx._column_key_to_sf_field("sf.Account.Name") == "Account.Name"
