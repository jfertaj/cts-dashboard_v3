#!/usr/bin/env python3
"""
Integration tests for the Members API endpoints.

Tests /api/members/bootstrap, /api/members/search, and /api/members/{id}/detail
against a live backend (local or ECS).

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_members.py
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/test_members.py

The SF_SESSION_COOKIE must be a valid signed session value from a logged-in browser.
"""
import sys, os, json
from typing import Any, Dict, List, Optional
import urllib.request, urllib.error

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("API_BASE", "https://cts-innodia-dashboard.org").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
SKIP = "\033[93m○\033[0m"
INFO = "\033[94mℹ\033[0m"
errors: List[str] = []

# ── HTTP helper ───────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None, expect_status: int = 200) -> Any:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Cookie": f"sf_session={SESSION_COOKIE}",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == expect_status:
            return {"__status": e.code}
        raise


def get(path: str, **kw) -> Any:   return _req("GET", path, **kw)
def post(path: str, body=None, **kw): return _req("POST", path, body, **kw)

# ── Assertion helper ──────────────────────────────────────────────────────────

def check(desc: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    line = f"  {icon}  {desc}"
    if detail: line += f"  [{detail}]"
    print(line)
    if not ok:
        errors.append(desc + (f": {detail}" if detail else ""))

def section(title: str):
    print(f"\n\033[1m{title}\033[0m")

# ── Test helpers ──────────────────────────────────────────────────────────────

def check_row_shape(row: Dict) -> bool:
    """Verify a member row has all required top-level and data keys."""
    required_top = ["account_id", "account_name", "country", "city", "data"]
    required_data_keys = [
        "sf.C_Level_of_Membership__c",
        "sf.Clinical_Site_CS__c",
        "sf.C_Deliver_Clinical_Grade_Services__c",
        "sf.C_Perform_Cutting_Edge__c",
        "sf.C_Contribute_as_a_Patient_Organization__c",
        "sf.Clinical_Site_CS_validated__c",
        "sf.Clinical_Trial_Site_CTS_validated__c",
        "sf.Diagnostic_Lab_DxLab_validated__c",
        "sf.Research_Mechanistic_Lab_LAB_validated__c",
        "sf.Patient_Organization_validated__c",
        "extra.SubAccountsCount",
        "extra.ContactsCount",
    ]
    for k in required_top:
        if k not in row:
            return False
    data = row.get("data", {})
    for k in required_data_keys:
        if k not in data:
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# TEST SUITES
# ─────────────────────────────────────────────────────────────────────────────

def test_no_auth():
    section("AUTH — unauthenticated requests")

    # Without cookie → 403
    url = f"{BASE_URL}/api/members/bootstrap"
    req = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="GET")
    try:
        urllib.request.urlopen(req, timeout=10)
        check("Bootstrap without cookie returns 403", False, "got 200 instead")
    except urllib.error.HTTPError as e:
        check("Bootstrap without cookie returns 403", e.code == 403, f"got {e.code}")
    except Exception as e:
        check("Bootstrap without cookie returns 403", False, str(e))


def test_bootstrap(rows: List[Dict]):
    section("BOOTSTRAP — /api/members/bootstrap")

    check("Bootstrap returns rows list", isinstance(rows, list), f"type={type(rows).__name__}")
    check("Bootstrap returns at least 1 member", len(rows) >= 1, f"count={len(rows)}")

    if rows:
        row = rows[0]
        check("Row has correct shape", check_row_shape(row), str(list(row.keys())))
        check("account_id is non-empty string", isinstance(row.get("account_id"), str) and bool(row["account_id"]))
        check("account_name is non-empty string", isinstance(row.get("account_name"), str) and bool(row["account_name"]))
        check("country is 2-char ISO2 or non-empty", bool(row.get("country")), f"country={row.get('country')!r}")
        check("lat is None or float", row.get("lat") is None or isinstance(row["lat"], (int, float)))
        check("lng is None or float", row.get("lng") is None or isinstance(row["lng"], (int, float)))
        check("SubAccountsCount is int >= 0", isinstance(row["data"]["extra.SubAccountsCount"], int))
        check("ContactsCount is int >= 0", isinstance(row["data"]["extra.ContactsCount"], int))

        # Role fields should all be booleans
        role_fields = [
            "sf.Clinical_Site_CS__c", "sf.C_Deliver_Clinical_Grade_Services__c",
            "sf.C_Perform_Cutting_Edge__c", "sf.C_Contribute_as_a_Patient_Organization__c",
            "sf.Clinical_Site_CS_validated__c", "sf.Clinical_Trial_Site_CTS_validated__c",
            "sf.Diagnostic_Lab_DxLab_validated__c", "sf.Research_Mechanistic_Lab_LAB_validated__c",
            "sf.Patient_Organization_validated__c",
        ]
        for field in role_fields:
            val = row["data"].get(field)
            check(f"Role field {field} is bool", isinstance(val, bool), f"val={val!r}")


def test_search_no_filters(all_rows: List[Dict]):
    section("SEARCH — no filters (returns all)")

    resp = post("/api/members/search", {"filters": {"logic": "AND", "rules": []}})
    rows = resp.get("rows", [])
    check("Empty filter returns same count as bootstrap", len(rows) == len(all_rows), f"{len(rows)} vs {len(all_rows)}")


def test_search_country_filter(all_rows: List[Dict]):
    section("SEARCH — country filter")

    # Find a country that exists in bootstrap
    countries = list({r["country"] for r in all_rows if r.get("country")})
    if not countries:
        print(f"  {SKIP}  No countries found in bootstrap — skipping")
        return

    target_iso2 = countries[0]
    expected_count = sum(1 for r in all_rows if r.get("country") == target_iso2)

    resp = post("/api/members/search", {
        "filters": {
            "logic": "AND",
            "rules": [{"field": "site.country", "operator": "equals", "value": target_iso2}],
        }
    })
    rows = resp.get("rows", [])
    check(f"Country filter ({target_iso2}) returns correct count",
          len(rows) == expected_count, f"{len(rows)} vs {expected_count}")
    check(f"All returned rows have country={target_iso2}",
          all(r.get("country") == target_iso2 for r in rows),
          f"countries={[r.get('country') for r in rows]}")


def test_search_role_filter_proposed_cs(all_rows: List[Dict]):
    section("SEARCH — proposed CS role filter")

    expected = [r for r in all_rows if r["data"].get("sf.Clinical_Site_CS__c")]
    resp = post("/api/members/search", {
        "filters": {
            "logic": "AND",
            "rules": [{"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True}],
        }
    })
    rows = resp.get("rows", [])
    check("CS filter count matches expected", len(rows) == len(expected), f"{len(rows)} vs {len(expected)}")
    check("All returned rows have CS=True",
          all(r["data"].get("sf.Clinical_Site_CS__c") is True for r in rows))


def test_search_role_filter_validated_cts(all_rows: List[Dict]):
    section("SEARCH — validated CTS role filter")

    field = "sf.Clinical_Trial_Site_CTS_validated__c"
    expected = [r for r in all_rows if r["data"].get(field)]
    if not expected:
        print(f"  {SKIP}  No members have Validated CTS — skipping assertion")
        return

    resp = post("/api/members/search", {
        "filters": {
            "logic": "AND",
            "rules": [{"field": field, "operator": "equals", "value": True}],
        }
    })
    rows = resp.get("rows", [])
    check("Validated CTS filter count matches expected", len(rows) == len(expected), f"{len(rows)} vs {len(expected)}")
    check("All returned rows have Validated CTS=True",
          all(r["data"].get(field) is True for r in rows))


def test_search_combined_and(all_rows: List[Dict]):
    section("SEARCH — AND: country + proposed CS")

    countries = list({r["country"] for r in all_rows if r.get("country")})
    if not countries:
        print(f"  {SKIP}  No countries available — skipping")
        return
    target = countries[0]
    expected = [r for r in all_rows if r.get("country") == target and r["data"].get("sf.Clinical_Site_CS__c")]

    resp = post("/api/members/search", {
        "filters": {
            "logic": "AND",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": target},
                {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
            ],
        }
    })
    rows = resp.get("rows", [])
    check(f"AND (country={target} + CS=True) count correct", len(rows) == len(expected), f"{len(rows)} vs {len(expected)}")
    check("AND result rows all match both conditions",
          all(r.get("country") == target and r["data"].get("sf.Clinical_Site_CS__c") for r in rows))


def test_search_or_logic(all_rows: List[Dict]):
    section("SEARCH — OR logic (two countries)")

    countries = list({r["country"] for r in all_rows if r.get("country")})
    if len(countries) < 2:
        print(f"  {SKIP}  Need at least 2 countries — skipping")
        return
    c1, c2 = countries[0], countries[1]
    expected_count = sum(1 for r in all_rows if r.get("country") in (c1, c2))

    resp = post("/api/members/search", {
        "filters": {
            "logic": "OR",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": c1},
                {"field": "site.country", "operator": "equals", "value": c2},
            ],
        }
    })
    rows = resp.get("rows", [])
    check(f"OR ({c1}|{c2}) returns correct count", len(rows) == expected_count, f"{len(rows)} vs {expected_count}")
    check("All OR rows have one of the two countries",
          all(r.get("country") in (c1, c2) for r in rows))


def test_search_name_contains(all_rows: List[Dict]):
    section("SEARCH — name contains filter")

    if not all_rows:
        print(f"  {SKIP}  No rows — skipping")
        return
    # Pick first 3 chars of a real institution name as search term
    target_name = all_rows[0]["account_name"]
    search_term = target_name[:5].lower()
    expected = [r for r in all_rows if search_term in r["account_name"].lower()]

    resp = post("/api/members/search", {
        "filters": {
            "logic": "AND",
            "rules": [{"field": "sf.Name", "operator": "contains", "value": search_term}],
        }
    })
    rows = resp.get("rows", [])
    check(f"Name contains '{search_term}' returns >= 1 row", len(rows) >= 1, f"got {len(rows)}")
    check("All returned rows contain the search term",
          all(search_term in r["account_name"].lower() for r in rows))


def test_detail(first_id: str):
    section(f"DETAIL — /api/members/{first_id}/detail")

    detail = get(f"/api/members/{first_id}/detail")

    check("Detail has account_id", "account_id" in detail)
    check("Detail account_id matches", detail.get("account_id") == first_id)
    check("Detail has account_name", bool(detail.get("account_name")))
    check("Detail has proposed_roles dict", isinstance(detail.get("proposed_roles"), dict))
    check("Detail has validated_roles dict", isinstance(detail.get("validated_roles"), dict))
    check("Detail has member_contacts list", isinstance(detail.get("member_contacts"), list))
    check("Detail has subaccounts list", isinstance(detail.get("subaccounts"), list))

    # Validate proposed_roles shape
    pr = detail.get("proposed_roles", {})
    for key in ("cs", "dxlab", "lab", "patient_org"):
        check(f"Proposed role '{key}' is bool", isinstance(pr.get(key), bool), f"val={pr.get(key)!r}")

    # Validate validated_roles shape
    vr = detail.get("validated_roles", {})
    for key in ("cs", "cts", "dxlab", "lab", "patient_org"):
        check(f"Validated role '{key}' is bool", isinstance(vr.get(key), bool), f"val={vr.get(key)!r}")

    # Validate contacts shape
    for c in detail.get("member_contacts", []):
        check("Contact has id", bool(c.get("id")))
        check("Contact has name", bool(c.get("name")))
        check("Contact is_board_member is bool", isinstance(c.get("is_board_member"), bool))
        check("Contact is_country_lead is bool", isinstance(c.get("is_country_lead"), bool))
        check("Contact voting_rights is bool", isinstance(c.get("voting_rights"), bool))
        break  # check first contact only

    # Validate subaccounts shape
    for sa in detail.get("subaccounts", []):
        check("SubAccount has id", bool(sa.get("id")))
        check("SubAccount has name", bool(sa.get("name")))
        check("SubAccount is_cts is bool", isinstance(sa.get("is_cts"), bool))
        check("SubAccount is_cs is bool", isinstance(sa.get("is_cs"), bool))
        check("SubAccount contacts is list", isinstance(sa.get("contacts"), list))
        break


def test_detail_invalid_id():
    section("DETAIL — invalid account ID")

    url = f"{BASE_URL}/api/members/INVALID_ID_000/detail"
    req = urllib.request.Request(
        url,
        headers={"Content-Type": "application/json", "Cookie": f"sf_session={SESSION_COOKIE}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            # Should return empty/partial detail — not a crash
            check("Invalid ID returns response without crash", True)
    except urllib.error.HTTPError as e:
        check(f"Invalid ID returns HTTP error (not 500)", e.code != 500, f"got {e.code}")
    except Exception as e:
        check("Invalid ID doesn't crash server", False, str(e))


def test_bootstrap_has_coords(rows: List[Dict]):
    section("BOOTSTRAP — coordinate data for map")

    rows_with_coords = [r for r in rows if r.get("lat") is not None and r.get("lng") is not None]
    pct = len(rows_with_coords) * 100 // max(len(rows), 1)
    print(f"  {INFO}  {len(rows_with_coords)}/{len(rows)} members have ShippingLatitude/Longitude ({pct}%)")
    check("At least some members have lat/lng coordinates", len(rows_with_coords) > 0,
          f"{len(rows_with_coords)} of {len(rows)}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if not SESSION_COOKIE:
        print(f"\033[91m✗ SF_SESSION_COOKIE is not set. Run:\033[0m")
        print("  SF_SESSION_COOKIE='<value>' python scripts/test_members.py")
        sys.exit(1)

    print(f"\n\033[1mMembers API — Integration Tests\033[0m")
    print(f"Target: {BASE_URL}\n")

    # Always test unauthenticated (no cookie needed)
    test_no_auth()

    # Bootstrap — all subsequent tests depend on it
    section("BOOTSTRAP — loading data")
    try:
        resp = get("/api/members/bootstrap")
    except Exception as e:
        print(f"  {FAIL}  Bootstrap request failed: {e}")
        sys.exit(1)

    rows = resp.get("rows", [])
    total = resp.get("total", 0)
    print(f"  {INFO}  Bootstrap returned {len(rows)} rows (total={total})")
    check("Bootstrap total matches row count", len(rows) == total, f"{len(rows)} vs {total}")

    test_bootstrap(rows)
    test_bootstrap_has_coords(rows)

    # Search tests
    test_search_no_filters(rows)
    test_search_country_filter(rows)
    test_search_role_filter_proposed_cs(rows)
    test_search_role_filter_validated_cts(rows)
    test_search_combined_and(rows)
    test_search_or_logic(rows)
    test_search_name_contains(rows)

    # Detail tests
    if rows:
        test_detail(rows[0]["account_id"])
    else:
        print(f"\n  {SKIP}  No members to test detail endpoint")

    test_detail_invalid_id()

    # ── Summary ──
    print(f"\n{'─' * 60}")
    if errors:
        print(f"\033[91m✗ {len(errors)} test(s) FAILED:\033[0m")
        for e in errors:
            print(f"    • {e}")
        sys.exit(1)
    else:
        print(f"\033[92m✓ All tests passed\033[0m")


if __name__ == "__main__":
    main()
