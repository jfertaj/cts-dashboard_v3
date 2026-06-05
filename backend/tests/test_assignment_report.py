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

    def test_blank_when_no_roles(self):
        idx = build_acr_index([_acr("ACENTER", "C1", "")])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="C1") == ""

    def test_blank_when_contact_absent(self):
        idx = build_acr_index([])
        assert resolve_role(idx, center_account_id="ACENTER", contact_id="CX") == ""
