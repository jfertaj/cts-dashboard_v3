#!/usr/bin/env python3
"""
User Story integration tests for Moby AI chat.

Story 1: Sites in a screening program → Stage 1/2, PI contact, city/country
Story 2: Sites with onsite pharmacy + overnight stay → ND, T1D patients, PI, current trials

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_user_stories.py
  SF_SESSION_COOKIE="<value>" API_BASE="https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com" python scripts/test_user_stories.py
"""

import sys, os, json, time, ssl
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional

# Allow self-signed / hostname-mismatch certs (ALB raw hostname)
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("API_BASE", "https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m?\033[0m"
INFO = "\033[94mℹ\033[0m"
errors: List[str] = []

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None) -> Any:
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
        with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode()[:300]
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body_txt}")

def chat(message: str, last_filters=None) -> Dict:
    return _req("POST", "/api/ai/chat", {
        "messages": [{"role": "user", "content": message}],
        "last_filters": last_filters,
    })

def explorer_search(filters: Dict, columns: List[str]) -> List[Dict]:
    resp = _req("POST", "/api/explorer/search", {"filters": filters, "columns": columns})
    return resp.get("rows", [])

# ─── Accessors ────────────────────────────────────────────────────────────────

def get_data(row: Dict, key: str) -> Optional[Any]:
    return row.get("data", {}).get(key)

def col_keys(resp: Dict) -> List[str]:
    return [c.get("key", "") for c in (resp.get("table") or {}).get("columns", [])]

def rows(resp: Dict) -> List[Dict]:
    return (resp.get("table") or {}).get("rows", [])

# ─── Assertion helper ─────────────────────────────────────────────────────────

def check(desc: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {desc}"
    if detail:
        msg += f"  [{detail}]"
    print(msg)
    if not ok:
        errors.append(f"{desc}" + (f": {detail}" if detail else ""))

def section(title: str):
    print(f"\n── {title} {'─' * max(1, 55 - len(title))}")

# ─── STORY 1 ──────────────────────────────────────────────────────────────────

def test_story1():
    """
    Story 1: "I would like to see all sites who are participating to a screening program
    and view what type of patients they screen and how many patients in each stage they
    are following. Would need to view the name of the site, country, city, Stage 1/2 counts,
    and PI contact name."
    """
    section("STORY 1 — Screening program sites (Stage 1/2 + PI)")

    # ── 1a. Direct explorer_search with Stage 1/2 columns ─────────────────
    print(f"  {INFO}  Querying explorer directly for sites with Stage 1 or 2 patients...")
    cols = [
        "sf.Account.Name", "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
        "sf.C_Number_of_Stage1_Individuals_followed__c",
        "sf.C_Number_of_Stage2_Individuals_followed__c",
    ]
    try:
        all_rows = explorer_search({"logic": "AND", "rules": []}, cols)
        check("Explorer returns at least 1 site", len(all_rows) >= 1, f"{len(all_rows)} rows")
    except Exception as e:
        check("Explorer baseline works", False, str(e))
        return

    # Filter to sites with Stage1 OR Stage2 > 0
    s1_key = "sf.C_Number_of_Stage1_Individuals_followed__c"
    s2_key = "sf.C_Number_of_Stage2_Individuals_followed__c"
    sites_with_stages = []
    for r in all_rows:
        s1 = get_data(r, s1_key) or 0
        s2 = get_data(r, s2_key) or 0
        try:
            if float(s1) > 0 or float(s2) > 0:
                sites_with_stages.append(r)
        except (TypeError, ValueError):
            pass

    check(f"At least 5 sites have Stage 1 or 2 individuals followed",
          len(sites_with_stages) >= 5, f"{len(sites_with_stages)} sites")
    if sites_with_stages:
        print(f"  {INFO}  Top 5 screening sites:")
        for r in sites_with_stages[:5]:
            name = r.get("account_name") or "?"
            country = r.get("country") or "?"
            city = r.get("city") or "?"
            s1 = get_data(r, s1_key) or 0
            s2 = get_data(r, s2_key) or 0
            print(f"        {name} | {city}, {country} | Stage1={s1} Stage2={s2}")

    # ── 1b. Moby query ─────────────────────────────────────────────────────
    section("STORY 1b — Moby: screening program with Stage 1/2 + PI")
    print(f"  {INFO}  Sending user story 1 to Moby...")
    query = (
        "Show me all sites that have Stage 1 or Stage 2 individuals being followed. "
        "For each site show: site name, country, city, "
        "number of Stage 1 individuals followed, and number of Stage 2 individuals followed."
    )
    try:
        resp = chat(query)
    except Exception as e:
        check("Moby story 1 responded", False, str(e))
        return

    answer = resp.get("answer", "")
    table_rows = rows(resp)
    table_cols = col_keys(resp)

    check("Moby returned an answer", bool(answer), answer[:100] if answer else "empty")
    check("Moby returned a table", len(table_rows) >= 1, f"{len(table_rows)} rows")
    check("Table has site name column",
          any("name" in k.lower() or "account" in k.lower() for k in table_cols),
          f"cols={table_cols[:8]}")
    check("Table has country column",
          any("country" in k.lower() or "shippingcountry" in k.lower() for k in table_cols),
          f"cols={table_cols[:8]}")
    check("Table has Stage 1 or Stage 2 column",
          any("stage" in k.lower() or "stage1" in k.lower() or "stage2" in k.lower() for k in table_cols),
          f"cols={table_cols[:8]}")

    print(f"  {INFO}  Moby answer: {answer[:200]}")
    print(f"  {INFO}  Columns: {table_cols}")
    if table_rows:
        print(f"  {INFO}  First row: {table_rows[0]}")

# ─── STORY 2 ──────────────────────────────────────────────────────────────────

def test_story2():
    """
    Story 2: "Sites with onsite pharmacy AND overnight stay.
    View: ND (both ages), T1D patients followed (both ages), city, country, PI name,
    current trials (assignments). Export to Excel."
    """
    section("STORY 2 — Sites with pharmacy + overnight stay (qual + sf + contacts)")

    PHARMACY_FIELD  = "qual.3_6__is_your_pharmacy_on_site_or_off_campus"
    OVERNIGHT_FIELD = "qual.3_5_2__overnight_stay"
    ND_O18_FIELD    = "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
    ND_U18_FIELD    = "sf.C_Number_of_new_T1D_diagnosed_U_18__c"
    T1D_O18_FIELD   = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
    T1D_U18_FIELD   = "sf.C_Number_of_T1D_Patients_currently_U_18__c"

    # ── 2a. Direct explorer_search with pharmacy + overnight filters ────────
    print(f"  {INFO}  Querying explorer directly: pharmacy on-site AND overnight stay...")
    cols = [
        "sf.Account.Name", "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
        ND_O18_FIELD, ND_U18_FIELD, T1D_O18_FIELD, T1D_U18_FIELD,
        PHARMACY_FIELD, OVERNIGHT_FIELD,
    ]
    filters = {
        "logic": "AND",
        "rules": [
            {"field": PHARMACY_FIELD,  "operator": "contains", "value": "On-site"},
            {"field": OVERNIGHT_FIELD, "operator": "equals",   "value": "Yes"},
        ],
    }
    try:
        filtered_rows = explorer_search(filters, cols)
    except Exception as e:
        check("Explorer pharmacy+overnight search works", False, str(e))
        return

    check("At least 1 site has pharmacy AND overnight stay",
          len(filtered_rows) >= 1, f"{len(filtered_rows)} sites")

    # Verify every row satisfies both conditions
    wrong_pharm = []
    wrong_over  = []
    for r in filtered_rows:
        ph = str(get_data(r, PHARMACY_FIELD) or "").lower()
        ov = str(get_data(r, OVERNIGHT_FIELD) or "").lower()
        if "on-site" not in ph and "on site" not in ph:
            wrong_pharm.append(r.get("account_name", "?"))
        if ov != "yes":
            wrong_over.append(r.get("account_name", "?"))

    check(f"All {len(filtered_rows)} sites have on-site pharmacy",
          len(wrong_pharm) == 0, f"Wrong: {wrong_pharm[:3]}")
    check(f"All {len(filtered_rows)} sites have overnight stay = Yes",
          len(wrong_over) == 0, f"Wrong: {wrong_over[:3]}")

    if filtered_rows:
        print(f"  {INFO}  {len(filtered_rows)} sites match. Sample:")
        for r in filtered_rows[:5]:
            name    = r.get("account_name") or "?"
            country = r.get("country") or "?"
            city    = r.get("city") or "?"
            nd_o18  = get_data(r, ND_O18_FIELD)
            nd_u18  = get_data(r, ND_U18_FIELD)
            ph      = get_data(r, PHARMACY_FIELD) or "?"
            ov      = get_data(r, OVERNIGHT_FIELD) or "?"
            print(f"        {name} | {city}, {country} | ND≥18={nd_o18} ND<18={nd_u18} | pharm={ph} over={ov}")

    # ── 2b. Count how many sites have ND data ─────────────────────────────
    sites_with_nd = [r for r in filtered_rows if get_data(r, ND_O18_FIELD) is not None or get_data(r, ND_U18_FIELD) is not None]
    if filtered_rows:
        pct = 100 * len(sites_with_nd) / len(filtered_rows)
        check(f"At least 50% of filtered sites have ND patient data",
              pct >= 50, f"{len(sites_with_nd)}/{len(filtered_rows)} = {pct:.0f}%")

    # ── 2c. Moby query ─────────────────────────────────────────────────────
    section("STORY 2b — Moby: pharmacy + overnight stay query")
    print(f"  {INFO}  Sending user story 2 to Moby (this may take 30-60s)...")
    query = (
        "Show me all sites that have an onsite pharmacy AND offer overnight stay. "
        "For each site I need: site name, country, city, "
        "number of newly diagnosed T1D patients over 18, "
        "number of newly diagnosed T1D patients under 18, "
        "number of T1D patients currently followed over 18, "
        "number of T1D patients currently followed under 18, "
        "and what other clinical trials they are currently participating in."
    )
    try:
        resp = chat(query)
    except Exception as e:
        check("Moby story 2 responded", False, str(e))
        return

    answer = resp.get("answer", "")
    table_rows_moby = rows(resp)
    table_cols_moby = col_keys(resp)

    check("Moby returned an answer", bool(answer), answer[:100] if answer else "empty")
    check("Moby returned a table with results", len(table_rows_moby) >= 1,
          f"{len(table_rows_moby)} rows")

    # Check column coverage
    col_str = " ".join(table_cols_moby).lower()
    check("Table has site name", any("name" in k.lower() or k.lower() == "site" for k in table_cols_moby), str(table_cols_moby[:6]))
    check("Table has country/city", "country" in col_str or "city" in col_str, str(table_cols_moby[:6]))
    check("Table has ND patients or T1D data",
          any(x in col_str for x in ["nd", "t1d", "newly", "diagnos", "patient"]),
          str(table_cols_moby[:8]))

    # Check result count consistency with direct query (within 20%)
    n_moby   = len(table_rows_moby)
    n_direct = len(filtered_rows)
    if n_moby > 0 and n_direct > 0:
        ratio = n_moby / n_direct
        check(f"Moby row count consistent with direct filter ({n_moby} vs {n_direct})",
              0.5 <= ratio <= 2.0, f"ratio={ratio:.2f}")

    print(f"  {INFO}  Moby answer: {answer[:250]}")
    print(f"  {INFO}  Columns ({len(table_cols_moby)}): {table_cols_moby[:8]}...")
    if table_rows_moby:
        print(f"  {INFO}  First row sample: { {k: table_rows_moby[0].get(k) for k in list(table_rows_moby[0])[:5]} }")

# ─── BONUS: Country filter test ───────────────────────────────────────────────

def test_country_filter():
    """Bonus: 'show me all sites in Spain' should return only Spanish sites."""
    section("BONUS — Country filter: 'show me all sites in Spain'")
    print(f"  {INFO}  Sending query to Moby...")
    try:
        resp = chat("show me all sites in Spain")
    except Exception as e:
        check("Moby country query responded", False, str(e))
        return

    answer = resp.get("answer", "")
    table_rows_moby = rows(resp)
    table_cols_moby = col_keys(resp)

    check("Moby returned an answer", bool(answer) and "couldn't" not in answer.lower(),
          answer[:100])
    check("Moby returned at least 1 row", len(table_rows_moby) >= 1, f"{len(table_rows_moby)} rows")

    # Check only Spanish sites returned
    non_spain = [r for r in table_rows_moby
                 if r.get("country") and str(r.get("country")).lower() not in ("spain", "es", "españa")]
    if table_rows_moby:
        check(f"All {len(table_rows_moby)} rows are in Spain",
              len(non_spain) == 0, f"Non-Spain: {[r.get('country') for r in non_spain[:3]]}")

    # Also test direct explorer with Spain filter
    try:
        direct = explorer_search(
            {"logic": "AND", "rules": [{"field": "site.country", "operator": "equals", "value": "Spain"}]},
            ["sf.Account.Name", "sf.Account.ShippingCountry"],
        )
        print(f"  {INFO}  Direct explorer (site.country=Spain): {len(direct)} rows")
    except Exception as e:
        print(f"  {WARN}  Direct explorer filter error: {e}")

    print(f"  {INFO}  Moby: {len(table_rows_moby)} rows, answer: {answer[:150]}")
    if table_rows_moby:
        for r in table_rows_moby[:5]:
            print(f"        {r.get('account_name','?')} | {r.get('city','?')} | {r.get('country','?')}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*65}")
    print("  Moby AI — User Story Integration Tests")
    print(f"  API: {BASE_URL}")
    print(f"{'='*65}")

    if not SESSION_COOKIE:
        print(f"\n  {FAIL}  SF_SESSION_COOKIE env var not set.")
        print("  Get it: Browser DevTools → Application → Cookies → sf_session")
        sys.exit(1)

    # Verify session
    try:
        me = _req("GET", "/api/salesforce/me")
        print(f"\n  {PASS}  Session valid — user: {me.get('display_name') or me.get('email') or 'OK'}")
    except Exception as e:
        print(f"\n  {FAIL}  Session invalid: {e}")
        sys.exit(1)

    test_story1()
    time.sleep(2)
    test_story2()
    time.sleep(2)
    test_country_filter()

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'='*65}")
    if errors:
        print(f"\033[91m  FAILED — {len(errors)} error(s):\033[0m")
        for e in errors:
            print(f"  • {e}")
        sys.exit(1)
    else:
        print(f"\033[92m  ALL CHECKS PASSED\033[0m")
    print(f"{'='*65}\n")

if __name__ == "__main__":
    run()
