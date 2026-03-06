"""
Unit tests for members_explorer.py pure functions.
No DB, no network, no FastAPI — just Python logic.
Run: pytest backend/tests/test_members_explorer.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.routers.members_explorer import _eval_filter, _apply_filter_group, _country_norm


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _row(
    account_id="001TEST0001",
    account_name="Test Institution",
    country="DE",
    city="Berlin",
    level="Full Member",
    status="Active",
    cs=False, dxlab=False, lab=False, patient_org=False,
    val_cs=False, val_cts=False, val_dxlab=False, val_lab=False, val_patient_org=False,
    sites=0, contacts=0,
) -> dict:
    """Build a minimal member row matching the real backend shape."""
    return {
        "account_id": account_id,
        "account_name": account_name,
        "country": country,
        "city": city,
        "data": {
            "sf.C_Level_of_Membership__c": level,
            "sf.Account_Status__c": status,
            # Proposed
            "sf.Clinical_Site_CS__c": cs,
            "sf.C_Deliver_Clinical_Grade_Services__c": dxlab,
            "sf.C_Perform_Cutting_Edge__c": lab,
            "sf.C_Contribute_as_a_Patient_Organization__c": patient_org,
            # Validated
            "sf.Clinical_Site_CS_validated__c": val_cs,
            "sf.Clinical_Trial_Site_CTS_validated__c": val_cts,
            "sf.Diagnostic_Lab_DxLab_validated__c": val_dxlab,
            "sf.Research_Mechanistic_Lab_LAB_validated__c": val_lab,
            "sf.Patient_Organization_validated__c": val_patient_org,
            # Counts
            "extra.SubAccountsCount": sites,
            "extra.ContactsCount": contacts,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# _country_norm
# ──────────────────────────────────────────────────────────────────────────────

class TestCountryNorm:
    def test_iso2_returned_as_upper(self):           assert _country_norm("de") == "DE"
    def test_iso2_uppercase_passthrough(self):        assert _country_norm("DE") == "DE"
    def test_full_name_german(self):                  assert _country_norm("Germany") == "DE"
    def test_full_name_lowercase(self):               assert _country_norm("germany") == "DE"
    def test_full_name_italian(self):                 assert _country_norm("Italy") == "IT"
    def test_full_name_spanish(self):                 assert _country_norm("Spain") == "ES"
    def test_alias_nederland(self):                   assert _country_norm("nederland") == "NL"
    def test_alias_holland(self):                     assert _country_norm("holland") == "NL"
    def test_alias_uk(self):                          assert _country_norm("uk") == "GB"
    def test_alias_united_kingdom(self):              assert _country_norm("united kingdom") == "GB"
    def test_unknown_returns_original(self):          assert _country_norm("Narnia") == "Narnia"
    def test_none_returns_none(self):                 assert _country_norm(None) is None
    def test_empty_string_returns_none(self):         assert _country_norm("") is None
    def test_leading_trailing_space(self):            assert _country_norm("  Germany  ") == "DE"


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — string / equality operators
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterString:
    def _rule(self, field, op, value): return {"field": field, "operator": op, "value": value}

    def test_equals_level_match(self):
        row = _row(level="Full Member")
        assert _eval_filter(self._rule("sf.C_Level_of_Membership__c", "equals", "Full Member"), row)

    def test_equals_level_no_match(self):
        row = _row(level="Associate Member")
        assert not _eval_filter(self._rule("sf.C_Level_of_Membership__c", "equals", "Full Member"), row)

    def test_not_equals_level(self):
        row = _row(level="Associate Member")
        assert _eval_filter(self._rule("sf.C_Level_of_Membership__c", "not_equals", "Full Member"), row)

    def test_contains_account_name(self):
        row = _row(account_name="Universität München")
        assert _eval_filter(self._rule("sf.Name", "contains", "münchen"), row)

    def test_contains_case_insensitive(self):
        row = _row(account_name="University of BARCELONA")
        assert _eval_filter(self._rule("sf.Name", "contains", "barcelona"), row)

    def test_not_contains_miss(self):
        row = _row(account_name="University of Rome")
        assert _eval_filter(self._rule("sf.Name", "not_contains", "münchen"), row)

    def test_not_contains_hit(self):
        row = _row(account_name="University of München")
        assert not _eval_filter(self._rule("sf.Name", "not_contains", "münchen"), row)


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — boolean role fields
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterBoolRoles:
    def _rule(self, field, value=True):
        return {"field": field, "operator": "equals", "value": value}

    def test_cs_proposed_true(self):
        row = _row(cs=True)
        assert _eval_filter(self._rule("sf.Clinical_Site_CS__c"), row)

    def test_cs_proposed_false_no_match(self):
        row = _row(cs=False)
        assert not _eval_filter(self._rule("sf.Clinical_Site_CS__c"), row)

    def test_dxlab_proposed_true(self):
        row = _row(dxlab=True)
        assert _eval_filter(self._rule("sf.C_Deliver_Clinical_Grade_Services__c"), row)

    def test_lab_proposed_true(self):
        row = _row(lab=True)
        assert _eval_filter(self._rule("sf.C_Perform_Cutting_Edge__c"), row)

    def test_patient_org_proposed_true(self):
        row = _row(patient_org=True)
        assert _eval_filter(self._rule("sf.C_Contribute_as_a_Patient_Organization__c"), row)

    def test_val_cs_true(self):
        row = _row(val_cs=True)
        assert _eval_filter(self._rule("sf.Clinical_Site_CS_validated__c"), row)

    def test_val_cts_true(self):
        row = _row(val_cts=True)
        assert _eval_filter(self._rule("sf.Clinical_Trial_Site_CTS_validated__c"), row)

    def test_val_dxlab_true(self):
        row = _row(val_dxlab=True)
        assert _eval_filter(self._rule("sf.Diagnostic_Lab_DxLab_validated__c"), row)

    def test_val_lab_true(self):
        row = _row(val_lab=True)
        assert _eval_filter(self._rule("sf.Research_Mechanistic_Lab_LAB_validated__c"), row)

    def test_val_patient_org_true(self):
        row = _row(val_patient_org=True)
        assert _eval_filter(self._rule("sf.Patient_Organization_validated__c"), row)

    def test_filter_false_matches_false_role(self):
        row = _row(cs=False)
        assert _eval_filter({"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": False}, row)

    def test_filter_true_does_not_match_false_role(self):
        row = _row(cs=False)
        assert not _eval_filter({"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True}, row)


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — country field (uses _country_norm internally)
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterCountry:
    def _rule(self, value):
        return {"field": "site.country", "operator": "equals", "value": value}

    def test_iso2_exact_match(self):
        row = _row(country="DE")
        assert _eval_filter(self._rule("DE"), row)

    def test_iso2_match_full_name_filter(self):
        row = _row(country="DE")
        assert _eval_filter(self._rule("Germany"), row)

    def test_iso2_case_insensitive(self):
        row = _row(country="DE")
        assert _eval_filter(self._rule("de"), row)

    def test_different_country_no_match(self):
        row = _row(country="IT")
        assert not _eval_filter(self._rule("DE"), row)

    def test_italy_iso2(self):
        row = _row(country="IT")
        assert _eval_filter(self._rule("IT"), row)

    def test_spain_full_name(self):
        row = _row(country="ES")
        assert _eval_filter(self._rule("Spain"), row)


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — city field
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterCity:
    def test_city_equals_match(self):
        row = _row(city="Munich")
        assert _eval_filter({"field": "site.city", "operator": "equals", "value": "Munich"}, row)

    def test_city_equals_case_insensitive(self):
        row = _row(city="Munich")
        assert _eval_filter({"field": "site.city", "operator": "equals", "value": "munich"}, row)

    def test_city_contains(self):
        row = _row(city="Munich")
        assert _eval_filter({"field": "site.city", "operator": "contains", "value": "mun"}, row)

    def test_city_no_match(self):
        row = _row(city="Rome")
        assert not _eval_filter({"field": "site.city", "operator": "equals", "value": "Munich"}, row)


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — numeric (SubAccountsCount / ContactsCount)
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterNumeric:
    def test_sites_gt_pass(self):
        row = _row(sites=5)
        assert _eval_filter({"field": "extra.SubAccountsCount", "operator": ">", "value": 3}, row)

    def test_sites_gt_fail(self):
        row = _row(sites=2)
        assert not _eval_filter({"field": "extra.SubAccountsCount", "operator": ">", "value": 3}, row)

    def test_sites_gte_equal(self):
        row = _row(sites=3)
        assert _eval_filter({"field": "extra.SubAccountsCount", "operator": ">=", "value": 3}, row)

    def test_sites_lt_pass(self):
        row = _row(sites=1)
        assert _eval_filter({"field": "extra.SubAccountsCount", "operator": "<", "value": 3}, row)

    def test_contacts_lte_equal(self):
        row = _row(contacts=5)
        assert _eval_filter({"field": "extra.ContactsCount", "operator": "<=", "value": 5}, row)

    def test_contacts_eq(self):
        row = _row(contacts=3)
        assert _eval_filter({"field": "extra.ContactsCount", "operator": "equals", "value": "3"}, row)


# ──────────────────────────────────────────────────────────────────────────────
# _eval_filter — is_empty / is_not_empty
# ──────────────────────────────────────────────────────────────────────────────

class TestEvalFilterEmpty:
    def test_is_empty_none_field(self):
        row = _row()
        # status field is "Active" — not empty
        assert not _eval_filter({"field": "sf.Account_Status__c", "operator": "is_empty", "value": None}, row)

    def test_is_not_empty_with_value(self):
        row = _row(level="Full Member")
        assert _eval_filter({"field": "sf.C_Level_of_Membership__c", "operator": "is_not_empty", "value": None}, row)

    def test_unknown_operator_permissive(self):
        row = _row(level="Full Member")
        assert _eval_filter({"field": "sf.C_Level_of_Membership__c", "operator": "zap", "value": None}, row)


# ──────────────────────────────────────────────────────────────────────────────
# _apply_filter_group — AND / OR / nested logic
# ──────────────────────────────────────────────────────────────────────────────

class TestApplyFilterGroup:
    def test_and_all_match(self):
        row = _row(country="DE", cs=True)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "DE"},
                {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
            ],
        }
        assert _apply_filter_group(group, row)

    def test_and_one_fails(self):
        row = _row(country="DE", cs=False)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "DE"},
                {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
            ],
        }
        assert not _apply_filter_group(group, row)

    def test_or_one_matches(self):
        row = _row(country="DE", cs=False)
        group = {
            "logic": "OR",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "IT"},  # fails
                {"field": "site.country", "operator": "equals", "value": "DE"},  # passes
            ],
        }
        assert _apply_filter_group(group, row)

    def test_or_none_match(self):
        row = _row(country="FR")
        group = {
            "logic": "OR",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "IT"},
                {"field": "site.country", "operator": "equals", "value": "DE"},
            ],
        }
        assert not _apply_filter_group(group, row)

    def test_empty_rules_passes_all(self):
        row = _row()
        group = {"logic": "AND", "rules": []}
        assert _apply_filter_group(group, row)

    def test_nested_groups(self):
        # (country=DE AND cs=True) OR (country=IT AND val_dxlab=True)
        row_de_cs = _row(country="DE", cs=True)
        row_it_dxlab = _row(country="IT", val_dxlab=True)
        row_no_match = _row(country="ES")
        group = {
            "logic": "OR",
            "rules": [
                {
                    "logic": "AND",
                    "rules": [
                        {"field": "site.country", "operator": "equals", "value": "DE"},
                        {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
                    ],
                },
                {
                    "logic": "AND",
                    "rules": [
                        {"field": "site.country", "operator": "equals", "value": "IT"},
                        {"field": "sf.Diagnostic_Lab_DxLab_validated__c", "operator": "equals", "value": True},
                    ],
                },
            ],
        }
        assert _apply_filter_group(group, row_de_cs)
        assert _apply_filter_group(group, row_it_dxlab)
        assert not _apply_filter_group(group, row_no_match)

    def test_country_and_validated_cts(self):
        row = _row(country="DE", val_cts=True)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "DE"},
                {"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True},
            ],
        }
        assert _apply_filter_group(group, row)

    def test_country_and_validated_cts_wrong_country(self):
        row = _row(country="IT", val_cts=True)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "DE"},
                {"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True},
            ],
        }
        assert not _apply_filter_group(group, row)

    def test_all_proposed_roles_true(self):
        row = _row(cs=True, dxlab=True, lab=True, patient_org=True)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
                {"field": "sf.C_Deliver_Clinical_Grade_Services__c", "operator": "equals", "value": True},
                {"field": "sf.C_Perform_Cutting_Edge__c", "operator": "equals", "value": True},
                {"field": "sf.C_Contribute_as_a_Patient_Organization__c", "operator": "equals", "value": True},
            ],
        }
        assert _apply_filter_group(group, row)

    def test_all_validated_roles_true(self):
        row = _row(val_cs=True, val_cts=True, val_dxlab=True, val_lab=True, val_patient_org=True)
        group = {
            "logic": "AND",
            "rules": [
                {"field": "sf.Clinical_Site_CS_validated__c", "operator": "equals", "value": True},
                {"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True},
                {"field": "sf.Diagnostic_Lab_DxLab_validated__c", "operator": "equals", "value": True},
                {"field": "sf.Research_Mechanistic_Lab_LAB_validated__c", "operator": "equals", "value": True},
                {"field": "sf.Patient_Organization_validated__c", "operator": "equals", "value": True},
            ],
        }
        assert _apply_filter_group(group, row)

    def test_level_filter_full_member(self):
        row_full = _row(level="Full Member")
        row_assoc = _row(level="Associate Member")
        rule = {"field": "sf.C_Level_of_Membership__c", "operator": "equals", "value": "Full Member"}
        group = {"logic": "AND", "rules": [rule]}
        assert _apply_filter_group(group, row_full)
        assert not _apply_filter_group(group, row_assoc)

    def test_name_contains_filter(self):
        row_munich = _row(account_name="Universität München")
        row_rome = _row(account_name="University of Rome")
        group = {
            "logic": "AND",
            "rules": [{"field": "sf.Name", "operator": "contains", "value": "münchen"}],
        }
        assert _apply_filter_group(group, row_munich)
        assert not _apply_filter_group(group, row_rome)

    def test_sites_gt_zero_filter(self):
        row_with_sites = _row(sites=3)
        row_no_sites = _row(sites=0)
        group = {
            "logic": "AND",
            "rules": [{"field": "extra.SubAccountsCount", "operator": ">", "value": 0}],
        }
        assert _apply_filter_group(group, row_with_sites)
        assert not _apply_filter_group(group, row_no_sites)
