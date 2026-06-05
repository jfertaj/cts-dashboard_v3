"""Unit tests for assignment_report service — pure, no DB/network.
Run: python -m pytest backend/tests/test_assignment_report.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.assignment_report import AssignmentFilters, build_assignment_soql, _soql_str_list


class TestSoqlStrList:
    def test_escapes_single_quote(self):
        assert _soql_str_list(["O'Brien", "Safe"]) == "'O\\'Brien','Safe'"

    def test_empty(self):
        assert _soql_str_list([]) == ""


class TestBuildAssignmentSoql:
    def test_referral_and_studies_and_stages(self):
        f = AssignmentFilters(
            studies=["Baricade", "Safeguard"],
            stages=["Activated"],
            referral_only=True,
        )
        soql = build_assignment_soql(f)
        assert "FROM Assignment__c" in soql
        assert "C_Opportunity_Name__r.Name IN ('Baricade','Safeguard')" in soql
        assert "C_Assignment_Stage__c IN ('Activated')" in soql
        assert "Referral_Contact__c = true" in soql
        assert "C_Contact_Name__c != null" in soql

    def test_no_filters_still_guards_contact(self):
        soql = build_assignment_soql(AssignmentFilters())
        assert "C_Contact_Name__c != null" in soql
        assert "Referral_Contact__c = true" not in soql


from app.services.assignment_report import build_acr_index, resolve_role


def _acr(acc, con, role, direct=False, lm="2026-01-01T00:00:00Z"):
    return {"AccountId": acc, "ContactId": con, "Role__c": role,
            "IsDirect": direct, "LastModifiedDate": lm}


class TestRoleResolution:
    def test_prefers_center_pair_nonempty(self):
        idx = build_acr_index([
            _acr("ACENTER", "C1", "Investigator"),
            _acr("AOTHER", "C1", "Study Nurse"),
        ])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == "Investigator"

    def test_falls_back_to_latest_nonempty_for_contact(self):
        idx = build_acr_index([
            _acr("ACENTER", "C1", "", lm="2026-01-01T00:00:00Z"),
            _acr("AOTHER", "C1", "Study Coordinator", lm="2026-05-01T00:00:00Z"),
        ])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == "Study Coordinator"

    def test_same_timestamp_tie_breaks_deterministically_by_account_id(self):
        # Equal LastModifiedDate: AccountId secondary key (reverse-sorted) makes
        # the highest AccountId win regardless of input/SOQL order.
        recs = [
            _acr("AAAA", "C1", "Study Nurse", lm="2026-05-01T00:00:00.000Z"),
            _acr("AZZZ", "C1", "Investigator", lm="2026-05-01T00:00:00.000Z"),
        ]
        idx_forward = build_acr_index(recs)
        idx_reverse = build_acr_index(list(reversed(recs)))
        assert resolve_role(idx_forward, center_account_id=None, contact_id="C1") == "Investigator"
        assert resolve_role(idx_reverse, center_account_id=None, contact_id="C1") == "Investigator"

    def test_blank_when_no_roles(self):
        idx = build_acr_index([_acr("ACENTER", "C1", "")])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == ""

    def test_blank_when_contact_absent(self):
        idx = build_acr_index([])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="CX") == ""


from app.services.assignment_report import assemble_rows, REPORT_COLUMNS


def _assignment(con, acc, study="Baricade", stage="Activated", referral=True,
                first="Jane", last="Doe", email="j@x.org",
                city="Liege", country="Belgium", pop="Both"):
    return {
        "C_Assignment_Stage__c": stage, "Referral_Contact__c": referral,
        "C_Account__c": acc, "C_Opportunity_Name__r": {"Name": study},
        "C_Contact_Name__c": con,
        "C_Contact_Name__r": {
            "FirstName": first, "LastName": last, "Email": email,
            "Account": {"Name": "Center X", "ShippingCity": city,
                        "ShippingCountry": country, "Patient_Population__c": pop},
        },
    }


class TestAssembleRows:
    def test_basic_row_shape_and_role(self):
        assignments = [_assignment("C1", "ACENTER")]
        idx = build_acr_index([_acr("ACENTER", "C1", "Investigator")])
        out = assemble_rows(assignments, idx, AssignmentFilters())
        assert [c["key"] for c in out["columns"]] == REPORT_COLUMNS
        row = out["rows"][0]
        assert row["role"] == "Investigator"
        assert row["email"] == "j@x.org"
        assert row["study"] == "Baricade"
        assert row["city"] == "Liege"

    def test_exclude_country_drops_row(self):
        assignments = [_assignment("C1", "ACENTER", country="United Kingdom")]
        out = assemble_rows(assignments, build_acr_index([]), AssignmentFilters(exclude_countries=["United Kingdom"]))
        assert out["rows"] == []

    def test_role_filter_keeps_only_matching(self):
        assignments = [_assignment("C1", "AC1"), _assignment("C2", "AC2", email="b@x.org")]
        idx = build_acr_index([_acr("AC1", "C1", "Investigator"), _acr("AC2", "C2", "Study Nurse")])
        out = assemble_rows(assignments, idx, AssignmentFilters(roles=["Investigator"]))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["role"] == "Investigator"
