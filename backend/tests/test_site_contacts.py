"""Unit tests for site_contacts service — pure, no DB/network.

Covers both Moby join tools:
- site_contacts_report  (contact-grain table)
- site_role_presence    (site-grain has_role flag, present/absent modes)

Run: python -m pytest backend/tests/test_site_contacts.py -v
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.site_contacts import (
    SiteContactFilters, SiteRolePresenceFilters,
    _soql_str_list, build_site_soql, build_study_accounts_soql,
    build_site_index, build_acr_by_account,
    assemble_contact_rows, assemble_presence_rows,
    fetch_site_contacts, fetch_site_role_presence,
    CONTACT_COLUMNS, PRESENCE_COLUMNS,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

def _site(aid, name="Site X", country="Belgium", city="Liege"):
    return {"Id": aid, "Name": name, "ShippingCountry": country, "ShippingCity": city}


def _acr(acc, con, role, first="Jane", last="Doe", email="j@x.org", title="Dr"):
    return {
        "AccountId": acc, "ContactId": con, "Role__c": role,
        "Contact": {"FirstName": first, "LastName": last, "Email": email, "Title": title},
    }


class _FakeSF:
    """Records SOQL and returns canned {'records': [...]} per call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.queries = []

    def query_all(self, soql):
        self.queries.append(soql)
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# SOQL builders
# ---------------------------------------------------------------------------

class TestSoqlStrList:
    def test_escapes_single_quote(self):
        assert _soql_str_list(["O'Brien", "Safe"]) == "'O\\'Brien','Safe'"

    def test_empty(self):
        assert _soql_str_list([]) == ""


class TestBuildSiteSoql:
    def test_site_set_clause_and_geo_filters(self):
        soql = build_site_soql(["Spain", "Italy"], ["Madrid"])
        assert "FROM Account" in soql
        assert "RecordType.DeveloperName = 'SubAccount'" in soql
        assert "C_Type__c = 'Clinical'" in soql
        assert "Account_Inactive__c = false OR Account_Inactive__c = null" in soql
        assert "ShippingCountry IN ('Spain','Italy')" in soql
        assert "ShippingCity IN ('Madrid')" in soql

    def test_no_geo_filters_still_keeps_site_clause(self):
        soql = build_site_soql([], [])
        assert "RecordType.DeveloperName = 'SubAccount'" in soql
        assert "ShippingCountry IN" not in soql
        assert "ShippingCity IN" not in soql

    def test_study_accounts_soql_keys_off_center_account(self):
        soql = build_study_accounts_soql(["Baricade", "Safeguard"])
        assert "FROM Assignment__c" in soql
        assert "C_Account__c" in soql
        assert "C_Opportunity_Name__r.Name IN ('Baricade','Safeguard')" in soql


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

class TestIndexing:
    def test_site_index_preserves_order_and_dedupes(self):
        idx = build_site_index([_site("A1", "First"), _site("A2", "Second"), _site("A1", "Dup")])
        assert list(idx.keys()) == ["A1", "A2"]
        assert idx["A1"]["site"] == "First"   # first wins
        assert idx["A2"]["country"] == "Belgium"

    def test_acr_by_account_groups_multiple_contacts(self):
        idx = build_acr_by_account([
            _acr("A1", "C1", "Investigator"),
            _acr("A1", "C2", "Study Coordinator"),
            _acr("A2", "C3", "Study Nurse"),
        ])
        assert len(idx["A1"]) == 2
        assert len(idx["A2"]) == 1


# ---------------------------------------------------------------------------
# Tool 1 — contact-grain assembly
# ---------------------------------------------------------------------------

class TestAssembleContactRows:
    def test_one_row_per_site_contact_with_role(self):
        sites = build_site_index([_site("A1", "CS-Leuven", "Belgium", "Leuven")])
        acr = build_acr_by_account([
            _acr("A1", "C1", "Investigator", first="Ana", last="Lopez", email="a@x.org", title="PI"),
            _acr("A1", "C2", "Study Coordinator", first="Bob", last="King", email="b@x.org"),
        ])
        out = assemble_contact_rows(sites, acr, SiteContactFilters())
        assert [c["key"] for c in out["columns"]] == CONTACT_COLUMNS
        assert len(out["rows"]) == 2
        r0 = out["rows"][0]
        assert r0["site"] == "CS-Leuven"
        assert r0["city"] == "Leuven"
        assert r0["first_name"] == "Ana"
        assert r0["title"] == "PI"
        assert r0["role"] == "Investigator"

    def test_site_with_no_contacts_yields_no_rows(self):
        sites = build_site_index([_site("A1")])
        out = assemble_contact_rows(sites, build_acr_by_account([]), SiteContactFilters())
        assert out["rows"] == []

    def test_role_filter_post_join(self):
        sites = build_site_index([_site("A1"), _site("A2", "Site2")])
        acr = build_acr_by_account([
            _acr("A1", "C1", "Investigator"),
            _acr("A2", "C2", "Study Nurse"),
        ])
        out = assemble_contact_rows(sites, acr, SiteContactFilters(roles=["Investigator"]))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["role"] == "Investigator"

    def test_role_filter_is_case_insensitive(self):
        sites = build_site_index([_site("A1")])
        acr = build_acr_by_account([_acr("A1", "C1", "Study Coordinator")])
        out = assemble_contact_rows(sites, acr, SiteContactFilters(roles=["study coordinator"]))
        assert len(out["rows"]) == 1

    def test_limit_caps_rows(self):
        sites = build_site_index([_site("A1")])
        acr = build_acr_by_account([_acr("A1", f"C{i}", "Investigator") for i in range(10)])
        out = assemble_contact_rows(sites, acr, SiteContactFilters(limit=3))
        assert len(out["rows"]) == 3


# ---------------------------------------------------------------------------
# Tool 2 — site-grain presence / left-anti
# ---------------------------------------------------------------------------

class TestAssemblePresenceRows:
    def _sites(self):
        return build_site_index([
            _site("A1", "Has-PI", "Belgium", "Liege"),
            _site("A2", "No-PI", "Spain", "Madrid"),
        ])

    def _acr(self):
        return build_acr_by_account([
            _acr("A1", "C1", "Principal Investigator", first="Ann", last="Pi", email="pi@x.org"),
            _acr("A2", "C2", "Study Nurse"),
        ])

    def test_mode_all_flags_every_site(self):
        out = assemble_presence_rows(self._sites(), self._acr(),
                                     SiteRolePresenceFilters(role="Investigator", mode="all"))
        assert [c["key"] for c in out["columns"]] == PRESENCE_COLUMNS
        flags = {r["site"]: r["has_role"] for r in out["rows"]}
        assert flags == {"Has-PI": True, "No-PI": False}

    def test_present_only_keeps_sites_with_role(self):
        out = assemble_presence_rows(self._sites(), self._acr(),
                                     SiteRolePresenceFilters(role="Investigator", mode="present"))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["site"] == "Has-PI"
        assert out["rows"][0]["last_name"] == "Pi"   # matching contact attached

    def test_absent_only_is_left_anti(self):
        out = assemble_presence_rows(self._sites(), self._acr(),
                                     SiteRolePresenceFilters(role="Investigator", mode="absent"))
        assert len(out["rows"]) == 1
        assert out["rows"][0]["site"] == "No-PI"
        assert out["rows"][0]["has_role"] is False

    def test_role_substring_match(self):
        # "Investigator" must match "Principal Investigator" and "Sub-Investigator"
        sites = build_site_index([_site("A1", "S1"), _site("A2", "S2")])
        acr = build_acr_by_account([
            _acr("A1", "C1", "Principal Investigator"),
            _acr("A2", "C2", "Sub-Investigator"),
        ])
        out = assemble_presence_rows(sites, acr,
                                     SiteRolePresenceFilters(role="Investigator", mode="present"))
        assert len(out["rows"]) == 2


# ---------------------------------------------------------------------------
# Orchestrators (end-to-end with _FakeSF)
# ---------------------------------------------------------------------------

class TestFetchSiteContacts:
    def test_two_queries_site_then_acr(self):
        sf = _FakeSF([
            {"records": [_site("A1", "CS-Leuven")]},
            {"records": [_acr("A1", "C1", "Investigator")]},
        ])
        out = fetch_site_contacts(sf, SiteContactFilters(countries=["Belgium"]))
        assert len(sf.queries) == 2
        assert "FROM Account" in sf.queries[0]
        assert "ShippingCountry IN ('Belgium')" in sf.queries[0]
        assert "FROM AccountContactRelation" in sf.queries[1]
        assert "AccountId IN ('A1')" in sf.queries[1]
        assert out["rows"][0]["role"] == "Investigator"

    def test_no_sites_skips_acr_query(self):
        sf = _FakeSF([{"records": []}])
        out = fetch_site_contacts(sf, SiteContactFilters())
        assert len(sf.queries) == 1
        assert out["rows"] == []

    def test_study_filter_runs_assignment_query_and_intersects(self):
        sf = _FakeSF([
            {"records": [_site("A1", "In-study"), _site("A2", "Not-in-study")]},
            {"records": [{"C_Account__c": "A1"}]},           # only A1 participates
            {"records": [_acr("A1", "C1", "Investigator")]},
        ])
        out = fetch_site_contacts(sf, SiteContactFilters(studies=["Baricade"]))
        assert len(sf.queries) == 3
        assert "FROM Assignment__c" in sf.queries[1]
        # ACR query must only ask for the intersected account
        assert "AccountId IN ('A1')" in sf.queries[2]
        assert {r["site"] for r in out["rows"]} == {"In-study"}

    def test_acr_chunking_more_than_200_accounts(self):
        # 205 distinct sites -> ACR query is chunked at 200 (2 ACR calls).
        site_resp = {"records": [_site(f"A{i}") for i in range(1, 206)]}
        acr_chunk_1 = {"records": [_acr(f"A{i}", f"C{i}", "Investigator") for i in range(1, 201)]}
        acr_chunk_2 = {"records": [_acr(f"A{i}", f"C{i}", "Sub-Investigator") for i in range(201, 206)]}
        sf = _FakeSF([site_resp, acr_chunk_1, acr_chunk_2])
        out = fetch_site_contacts(sf, SiteContactFilters())
        assert len(sf.queries) == 3
        assert "FROM Account" in sf.queries[0]
        assert "FROM AccountContactRelation" in sf.queries[1]
        assert "FROM AccountContactRelation" in sf.queries[2]
        # every account id appears in exactly one ACR chunk query
        for i in range(1, 206):
            in_first = f"'A{i}'" in sf.queries[1]
            in_second = f"'A{i}'" in sf.queries[2]
            assert in_first != in_second, f"A{i} must be in exactly one chunk"
        assert len(out["rows"]) == 205


class TestFetchSiteRolePresence:
    def test_site_then_acr_and_flags(self):
        sf = _FakeSF([
            {"records": [_site("A1", "Has-PI"), _site("A2", "No-PI")]},
            {"records": [_acr("A1", "C1", "Principal Investigator")]},
        ])
        out = fetch_site_role_presence(sf, SiteRolePresenceFilters(role="Investigator", mode="all"))
        assert len(sf.queries) == 2
        flags = {r["site"]: r["has_role"] for r in out["rows"]}
        assert flags == {"Has-PI": True, "No-PI": False}

    def test_absent_only_end_to_end(self):
        sf = _FakeSF([
            {"records": [_site("A1", "Has-PI"), _site("A2", "No-PI")]},
            {"records": [_acr("A1", "C1", "Principal Investigator")]},
        ])
        out = fetch_site_role_presence(sf, SiteRolePresenceFilters(role="Investigator", mode="absent"))
        assert [r["site"] for r in out["rows"]] == ["No-PI"]

    def test_no_sites_yields_empty_table_no_acr(self):
        sf = _FakeSF([{"records": []}])
        out = fetch_site_role_presence(sf, SiteRolePresenceFilters(role="Investigator"))
        assert len(sf.queries) == 1
        assert out["rows"] == []
        assert [c["key"] for c in out["columns"]] == PRESENCE_COLUMNS
