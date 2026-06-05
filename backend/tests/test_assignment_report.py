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
                center="Center X", city="Liege", country="Belgium", pop="Both",
                contact_country="Belgium"):
    # Center/city/country/patient_population come from the assignment center
    # (C_Account__r). The contact's primary account (C_Contact_Name__r.Account)
    # is intentionally present-but-unused so tests can prove the report keys off
    # the center, not the contact account (Codex P1).
    return {
        "C_Assignment_Stage__c": stage, "Referral_Contact__c": referral,
        "C_Account__c": acc,
        "C_Account__r": {"Name": center, "ShippingCity": city,
                         "ShippingCountry": country, "Patient_Population__c": pop},
        "C_Opportunity_Name__r": {"Name": study},
        "C_Contact_Name__c": con,
        "C_Contact_Name__r": {
            "FirstName": first, "LastName": last, "Email": email,
            "Account": {"Name": "Contact Primary Acct",
                        "ShippingCity": "ContactCity",
                        "ShippingCountry": contact_country},
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

    def test_country_filter_uses_center_not_contact_account(self):
        # Codex P1: center is in the UK, contact's primary account is in Belgium.
        # exclude_countries=["United Kingdom"] must drop the row on the CENTER's
        # country, not the contact's unrelated primary account.
        a = _assignment("C1", "ACENTER", country="United Kingdom", contact_country="Belgium")
        out = assemble_rows([a], build_acr_index([]), AssignmentFilters(exclude_countries=["United Kingdom"]))
        assert out["rows"] == []

    def test_center_display_fields_come_from_center_account(self):
        a = _assignment("C1", "ACENTER", center="CS-Leuven", city="Leuven",
                        pop="T1D", contact_country="France")
        row = assemble_rows([a], build_acr_index([]), AssignmentFilters())["rows"][0]
        assert row["center"] == "CS-Leuven"
        assert row["city"] == "Leuven"
        assert row["patient_population"] == "T1D"

    def test_include_countries_keeps_only_matching(self):
        rows = [_assignment("C1", "AC1", country="Belgium"),
                _assignment("C2", "AC2", country="Spain", email="b@x.org")]
        out = assemble_rows(rows, build_acr_index([]), AssignmentFilters(include_countries=["Belgium"]))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["email"] == "j@x.org"

    def test_exclude_takes_precedence_over_include(self):
        # A country present in both lists is dropped (exclude wins).
        a = _assignment("C1", "ACENTER", country="United Kingdom")
        out = assemble_rows([a], build_acr_index([]), AssignmentFilters(
            include_countries=["United Kingdom"], exclude_countries=["United Kingdom"]))
        assert out["rows"] == []

    def test_role_filter_keeps_only_matching(self):
        assignments = [_assignment("C1", "AC1"), _assignment("C2", "AC2", email="b@x.org")]
        idx = build_acr_index([_acr("AC1", "C1", "Investigator"), _acr("AC2", "C2", "Study Nurse")])
        out = assemble_rows(assignments, idx, AssignmentFilters(roles=["Investigator"]))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["role"] == "Investigator"


from app.services.assignment_report import fetch_report


class _FakeSF:
    """Records SOQL and returns canned {'records': [...]} per call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.queries = []
    def query_all(self, soql):
        self.queries.append(soql)
        return self._responses.pop(0)


class TestFetchReport:
    def test_two_queries_and_join(self):
        assignment_resp = {"records": [_assignment("C1", "ACENTER")]}
        acr_resp = {"records": [_acr("ACENTER", "C1", "Investigator")]}
        sf = _FakeSF([assignment_resp, acr_resp])
        out = fetch_report(sf, AssignmentFilters(studies=["Baricade"], referral_only=True))
        assert len(sf.queries) == 2
        assert "FROM Assignment__c" in sf.queries[0]
        assert "FROM AccountContactRelation" in sf.queries[1]
        assert "ContactId IN ('C1')" in sf.queries[1]
        assert out["rows"][0]["role"] == "Investigator"

    def test_no_assignments_skips_acr_query(self):
        sf = _FakeSF([{"records": []}])
        out = fetch_report(sf, AssignmentFilters())
        assert len(sf.queries) == 1
        assert out["rows"] == []

    def test_acr_chunking_more_than_200_contacts(self):
        # 205 distinct contacts -> ACR query is chunked at 200 (2 ACR calls).
        assignment_resp = {"records": [_assignment(f"C{i}", "ACENTER") for i in range(1, 206)]}
        acr_chunk_1 = {"records": [_acr("ACENTER", f"C{i}", "Investigator") for i in range(1, 201)]}
        acr_chunk_2 = {"records": [_acr("ACENTER", f"C{i}", "Sub-Investigator") for i in range(201, 206)]}
        sf = _FakeSF([assignment_resp, acr_chunk_1, acr_chunk_2])

        out = fetch_report(sf, AssignmentFilters())

        # 1 Assignment__c query + 2 ACR queries
        assert len(sf.queries) == 3
        assert "FROM Assignment__c" in sf.queries[0]
        # contact_ids are sorted; chunk boundaries verified by membership, not order
        assert "FROM AccountContactRelation" in sf.queries[1]
        assert "FROM AccountContactRelation" in sf.queries[2]
        # every contact id appears in exactly one ACR chunk query
        for i in range(1, 206):
            in_first = f"'C{i}'" in sf.queries[1]
            in_second = f"'C{i}'" in sf.queries[2]
            assert in_first != in_second, f"C{i} must be in exactly one chunk"
        # roles from both chunks survive into the assembled rows
        roles = {row["role"] for row in out["rows"]}
        assert "Investigator" in roles
        assert "Sub-Investigator" in roles
        assert len(out["rows"]) == 205
