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
