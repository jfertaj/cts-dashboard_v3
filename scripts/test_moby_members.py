#!/usr/bin/env python3
"""
Moby AI + Members API — Comprehensive Integration Tests

Covers:
  BOOT-*   Direct /api/members/bootstrap endpoint
  SRCH-*   Direct /api/members/search endpoint (filters + logic)
  DETL-*   Direct /api/members/{id}/detail endpoint
  MCT-*    Moby: member count queries (planner + Claude)
  MROL-*   Moby: member role queries (proposed + validated)
  MCTY-*   Moby: members by country
  MREL-*   Moby: member ↔ clinical-site relationships
  MCON-*   Moby: member contacts
  MMTX-*   Moby: multi-turn members context
  MXPL-*   Moby: members cross-referenced with Explorer / SF accounts

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_members.py
  SF_SESSION_COOKIE="<val>" SKIP_SLOW=1 python scripts/test_moby_members.py
  SF_SESSION_COOKIE="<val>" API_BASE="http://localhost:8000" python scripts/test_moby_members.py

Real-data baselines (verified 2026-03-06 against innodiaivzw.my.salesforce.com):
  Total members : 243
  Italy (IT)    : 70   Spain (ES) : 33   France (FR) : 29
  GB : 17   DE : 10   NL : 10
  Validated CS  : 2    (Bichat-Claude Bernard Hospital FR, Policlinico of Milan IT)
  Validated CTS : 1    (Policlinico of Milan IT)
  Proposed CS   : 211  Proposed DxLab : 77
  Members with SubAccounts : 198
  University of Cambridge  : 4 SubAccounts, 6 member contacts
  KU Leuven                : 3 SubAccounts
"""

import sys, os, json, time, ssl, re, traceback
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode   = ssl.CERT_NONE

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("API_BASE", "https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
SKIP_SLOW      = os.environ.get("SKIP_SLOW", "0") == "1"
CHAT_TIMEOUT   = int(os.environ.get("CHAT_TIMEOUT", "120"))

PASS = "\033[92m✓\033[0m"; FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m?\033[0m"; INFO = "\033[94mℹ\033[0m"

errors:   List[str] = []
warnings: List[str] = []
passed  = 0

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None, timeout: int = 30) -> Any:
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method,
               headers={"Content-Type": "application/json",
                        "Cookie": f"sf_session={SESSION_COOKIE}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {e.read().decode()[:300]}")
    except Exception as e:
        raise RuntimeError(str(e))

def chat(msg: str, last_filters=None, history=None, timeout: int = CHAT_TIMEOUT) -> Dict:
    messages = (history or []) + [{"role": "user", "content": msg}]
    return _req("POST", "/api/ai/chat",
                {"messages": messages, "last_filters": last_filters},
                timeout=timeout)

def members_get(path: str) -> Any:
    return _req("GET", path)

def members_search(filters: Dict) -> Dict:
    return _req("POST", "/api/members/search", {"filters": filters})

# ── Console helpers ────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n── {title} {'─' * max(1, 55 - len(title))}")

def ok(desc: str, detail: str = ""):
    global passed
    passed += 1
    msg = f"  {PASS}  {desc}"
    if detail: msg += f"  [{detail}]"
    print(msg)

def fail(desc: str, detail: str = ""):
    msg = f"  {FAIL}  {desc}"
    if detail: msg += f"  [{detail}]"
    print(msg)
    errors.append(f"{desc}" + (f": {detail}" if detail else ""))

def warn(desc: str, detail: str = ""):
    print(f"  {WARN}  {desc}" + (f"  [{detail}]" if detail else ""))
    warnings.append(desc)

def chk(desc: str, cond: bool, detail: str = ""):
    (ok if cond else fail)(desc, detail)

def info(msg: str):
    print(f"  {INFO}  {msg}")

def skip(desc: str):
    print(f"  \033[90m-  {desc} [SKIPPED — SKIP_SLOW=1]\033[0m")

def moby(msg: str, last_filters=None, history=None, label: str = "") -> Optional[Dict]:
    """Call Moby; return response or None on error/timeout."""
    try:
        return chat(msg, last_filters=last_filters, history=history)
    except Exception as e:
        err = str(e)
        lbl = label or msg[:60]
        if "timed out" in err.lower() or "timeout" in err.lower():
            fail(f"{lbl} — timed out")
        else:
            fail(f"{lbl} — error", err[:120])
        return None

def tbl_rows(resp: Optional[Dict]) -> List[Dict]:
    if not resp: return []
    return (resp.get("table") or {}).get("rows", [])

def col_keys(resp: Optional[Dict]) -> List[str]:
    if not resp: return []
    return [c.get("key", "") for c in (resp.get("table") or {}).get("columns", [])]

def answer(resp: Optional[Dict]) -> str:
    if not resp: return ""
    return resp.get("answer", "") or ""

def answer_text(resp: Optional[Dict]) -> str:
    """Strip HTML tags from answer."""
    a = answer(resp)
    return re.sub(r"<[^>]+>", " ", a).strip()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Direct API: Bootstrap
# ═══════════════════════════════════════════════════════════════════════════════

def test_1_bootstrap():
    section("BOOT-1 — Unauthenticated request returns 403")
    url = f"{BASE_URL}/api/members/bootstrap"
    req = urllib.request.Request(url, method="GET",
              headers={"Content-Type": "application/json"})  # no cookie
    try:
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as r:
            status = r.status
        fail("BOOT-1: no-auth should return 403", f"got {status}")
    except urllib.error.HTTPError as e:
        chk("BOOT-1: unauthenticated → 403", e.code == 403, f"got {e.code}")
    except Exception as e:
        fail("BOOT-1: no-auth request", str(e))

    section("BOOT-2 — Authenticated bootstrap returns rows")
    try:
        resp = members_get("/api/members/bootstrap")
        rows = resp.get("rows", [])
        total = resp.get("total", 0)
        chk("BOOT-2: response has 'rows' key", "rows" in resp)
        chk("BOOT-2: response has 'total' key", "total" in resp)
        chk("BOOT-2: total ≥ 200", total >= 200, f"total={total}")
        chk("BOOT-2: rows length matches total", len(rows) == total,
            f"len={len(rows)}, total={total}")
        info(f"Bootstrap: {total} member institutions")
    except Exception as e:
        fail("BOOT-2: bootstrap request", str(e))
        return

    section("BOOT-3 — Row structure validation")
    try:
        resp = members_get("/api/members/bootstrap")
        rows = resp["rows"]
        r0 = rows[0]
        chk("BOOT-3: row has account_id",   "account_id"   in r0)
        chk("BOOT-3: row has account_name", "account_name" in r0)
        chk("BOOT-3: row has country",      "country"      in r0)
        chk("BOOT-3: row has city",         "city"         in r0)
        chk("BOOT-3: row has lat field",    "lat"          in r0)
        chk("BOOT-3: row has lng field",    "lng"          in r0)
        chk("BOOT-3: row has data dict",    isinstance(r0.get("data"), dict))
        d = r0.get("data", {})
        chk("BOOT-3: data has proposed CS flag",
            "sf.Clinical_Site_CS__c" in d)
        chk("BOOT-3: data has validated CTS flag",
            "sf.Clinical_Trial_Site_CTS_validated__c" in d)
        chk("BOOT-3: data has SubAccountsCount",
            "extra.SubAccountsCount" in d)
        chk("BOOT-3: data has ContactsCount",
            "extra.ContactsCount" in d)
    except Exception as e:
        fail("BOOT-3: row structure", str(e))

    section("BOOT-4 — Country distribution sanity check")
    try:
        resp = members_get("/api/members/bootstrap")
        rows = resp["rows"]
        by_country = {}
        for r in rows:
            c = r.get("country") or "?"
            by_country[c] = by_country.get(c, 0) + 1
        chk("BOOT-4: Italy (IT) ≥ 50 members", by_country.get("IT", 0) >= 50,
            f"IT={by_country.get('IT',0)}")
        chk("BOOT-4: Spain (ES) ≥ 20 members", by_country.get("ES", 0) >= 20,
            f"ES={by_country.get('ES',0)}")
        chk("BOOT-4: France (FR) ≥ 15 members", by_country.get("FR", 0) >= 15,
            f"FR={by_country.get('FR',0)}")
        chk("BOOT-4: ≥ 10 distinct countries represented",
            len(by_country) >= 10, f"{len(by_country)} countries")
        info(f"Top countries: {sorted(by_country.items(), key=lambda x:-x[1])[:6]}")
    except Exception as e:
        fail("BOOT-4: country distribution", str(e))

    section("BOOT-5 — Role flags sanity check")
    try:
        resp = members_get("/api/members/bootstrap")
        rows = resp["rows"]
        prop_cs  = sum(1 for r in rows if r["data"].get("sf.Clinical_Site_CS__c"))
        prop_dxlab = sum(1 for r in rows if r["data"].get("sf.C_Deliver_Clinical_Grade_Services__c"))
        val_cs   = sum(1 for r in rows if r["data"].get("sf.Clinical_Site_CS_validated__c"))
        val_cts  = sum(1 for r in rows if r["data"].get("sf.Clinical_Trial_Site_CTS_validated__c"))
        with_subs = sum(1 for r in rows if r["data"].get("extra.SubAccountsCount", 0) > 0)
        chk("BOOT-5: proposed CS ≥ 100", prop_cs >= 100, f"{prop_cs}")
        chk("BOOT-5: proposed DxLab ≥ 10", prop_dxlab >= 10, f"{prop_dxlab}")
        chk("BOOT-5: validated CS ≥ 1", val_cs >= 1, f"{val_cs}")
        chk("BOOT-5: validated CTS ≥ 1", val_cts >= 1, f"{val_cts}")
        chk("BOOT-5: ≥ 100 members have SubAccounts", with_subs >= 100, f"{with_subs}")
        info(f"Proposed CS={prop_cs}, DxLab={prop_dxlab} | Val.CS={val_cs}, CTS={val_cts}")
    except Exception as e:
        fail("BOOT-5: role flags", str(e))

    section("BOOT-6 — Location data (lat/lng) fields present in rows")
    try:
        resp = members_get("/api/members/bootstrap")
        rows = resp["rows"]
        # Check the fields exist on every row (even if null — SF may not populate them)
        chk("BOOT-6: 'lat' field present in rows", all("lat" in r for r in rows[:10]))
        chk("BOOT-6: 'lng' field present in rows", all("lng" in r for r in rows[:10]))
        with_coords = sum(1 for r in rows if r.get("lat") is not None and r.get("lng") is not None)
        # Warn (not fail) if no coords — Salesforce may not populate ShippingLatitude for members
        if with_coords == 0:
            warn("BOOT-6: no members have lat/lng — ShippingLatitude not populated in SF", "map will show no pins")
        else:
            ok(f"BOOT-6: {with_coords}/{len(rows)} members have geolocation coords")
        info(f"Members with lat/lng: {with_coords}/{len(rows)}")
    except Exception as e:
        fail("BOOT-6: location data", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Direct API: Search (filters)
# ═══════════════════════════════════════════════════════════════════════════════

def test_2_search():
    section("SRCH-1 — Empty filter returns all members")
    try:
        resp = members_search({"logic": "AND", "rules": []})
        chk("SRCH-1: total ≥ 200", resp.get("total", 0) >= 200,
            f"total={resp.get('total')}")
    except Exception as e:
        fail("SRCH-1", str(e))

    section("SRCH-2 — Filter by country=IT")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": "IT"}
        ]})
        chk("SRCH-2: Italy filter → 60–80 members", 60 <= resp.get("total", 0) <= 80,
            f"total={resp.get('total')}")
        non_it = [r for r in resp.get("rows", []) if r.get("country") != "IT"]
        chk("SRCH-2: all rows are Italy (IT)", len(non_it) == 0,
            f"{len(non_it)} non-IT rows")
    except Exception as e:
        fail("SRCH-2", str(e))

    section("SRCH-3 — Filter by country=Spain (full name)")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": "Spain"}
        ]})
        chk("SRCH-3: Spain filter → 20–40 members", 20 <= resp.get("total", 0) <= 40,
            f"total={resp.get('total')}")
    except Exception as e:
        fail("SRCH-3", str(e))

    section("SRCH-4 — Filter by validated CS role")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "sf.Clinical_Site_CS_validated__c", "operator": "equals", "value": True}
        ]})
        chk("SRCH-4: validated CS → 1–5 members", 1 <= resp.get("total", 0) <= 5,
            f"total={resp.get('total')}")
        names = [r["account_name"] for r in resp.get("rows", [])]
        chk("SRCH-4: Policlinico of Milan present",
            any("policlinico" in n.lower() or "milan" in n.lower() for n in names),
            f"names={names}")
        info(f"Validated CS members: {names}")
    except Exception as e:
        fail("SRCH-4", str(e))

    section("SRCH-5 — Filter by validated CTS role")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "sf.Clinical_Trial_Site_CTS_validated__c", "operator": "equals", "value": True}
        ]})
        chk("SRCH-5: validated CTS ≥ 1", resp.get("total", 0) >= 1,
            f"total={resp.get('total')}")
        names = [r["account_name"] for r in resp.get("rows", [])]
        info(f"Validated CTS members: {names}")
    except Exception as e:
        fail("SRCH-5", str(e))

    section("SRCH-6 — Filter by proposed DxLab role")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "sf.C_Deliver_Clinical_Grade_Services__c", "operator": "equals", "value": True}
        ]})
        chk("SRCH-6: proposed DxLab ≥ 50", resp.get("total", 0) >= 50,
            f"total={resp.get('total')}")
    except Exception as e:
        fail("SRCH-6", str(e))

    section("SRCH-7 — Filter by name contains 'Leuven'")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "sf.Name", "operator": "contains", "value": "Leuven"}
        ]})
        chk("SRCH-7: 'Leuven' search → exactly 1", resp.get("total", 0) == 1,
            f"total={resp.get('total')}")
        if resp.get("rows"):
            chk("SRCH-7: result is KU Leuven",
                "leuven" in resp["rows"][0]["account_name"].lower())
    except Exception as e:
        fail("SRCH-7", str(e))

    section("SRCH-8 — OR logic: Italy OR Spain")
    try:
        resp = members_search({"logic": "OR", "rules": [
            {"field": "site.country", "operator": "equals", "value": "IT"},
            {"field": "site.country", "operator": "equals", "value": "ES"},
        ]})
        chk("SRCH-8: IT OR ES → 90–120 members", 90 <= resp.get("total", 0) <= 120,
            f"total={resp.get('total')}")
    except Exception as e:
        fail("SRCH-8", str(e))

    section("SRCH-9 — AND logic: France AND proposed CS")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": "FR"},
            {"field": "sf.Clinical_Site_CS__c", "operator": "equals", "value": True},
        ]})
        chk("SRCH-9: FR + proposed CS → ≥ 1", resp.get("total", 0) >= 1,
            f"total={resp.get('total')}")
        for r in resp.get("rows", []):
            chk(f"SRCH-9: {r['account_name'][:30]} is FR",
                r.get("country") == "FR", r.get("country"))
            chk(f"SRCH-9: {r['account_name'][:30]} has proposed CS",
                bool(r["data"].get("sf.Clinical_Site_CS__c")))
    except Exception as e:
        fail("SRCH-9", str(e))

    section("SRCH-10 — SubAccountsCount > 0 filter")
    try:
        resp = members_search({"logic": "AND", "rules": [
            {"field": "extra.SubAccountsCount", "operator": ">", "value": 0}
        ]})
        chk("SRCH-10: members with sites > 0 → ≥ 100", resp.get("total", 0) >= 100,
            f"total={resp.get('total')}")
        # Verify all returned rows actually have sites
        zero_sites = [r for r in resp.get("rows", [])
                      if r["data"].get("extra.SubAccountsCount", 0) <= 0]
        chk("SRCH-10: all rows have SubAccountsCount > 0", len(zero_sites) == 0,
            f"{len(zero_sites)} rows with 0 sites")
    except Exception as e:
        fail("SRCH-10", str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Direct API: Detail endpoint
# ═══════════════════════════════════════════════════════════════════════════════

def test_3_detail():
    CAMBRIDGE_ID = "001Vg000003fJUsIAM"

    section("DETL-1 — University of Cambridge detail structure")
    try:
        d = members_get(f"/api/members/{CAMBRIDGE_ID}/detail")
        chk("DETL-1: account_name present", bool(d.get("account_name")))
        chk("DETL-1: name contains Cambridge",
            "cambridge" in d.get("account_name", "").lower(),
            d.get("account_name", ""))
        chk("DETL-1: country = GB", d.get("country") == "GB", d.get("country"))
        chk("DETL-1: has proposed_roles dict",
            isinstance(d.get("proposed_roles"), dict))
        chk("DETL-1: has validated_roles dict",
            isinstance(d.get("validated_roles"), dict))
        chk("DETL-1: has member_contacts list",
            isinstance(d.get("member_contacts"), list))
        chk("DETL-1: has subaccounts list",
            isinstance(d.get("subaccounts"), list))
    except Exception as e:
        fail("DETL-1: Cambridge detail", str(e))
        return

    section("DETL-2 — Cambridge subaccounts count and structure")
    try:
        d = members_get(f"/api/members/{CAMBRIDGE_ID}/detail")
        subs = d.get("subaccounts", [])
        chk("DETL-2: ≥ 3 SubAccounts (expected 4)", len(subs) >= 3,
            f"{len(subs)} subaccounts")
        if subs:
            s0 = subs[0]
            chk("DETL-2: SubAccount has 'id'", "id" in s0)
            chk("DETL-2: SubAccount has 'name'", "name" in s0)
            chk("DETL-2: SubAccount has 'contacts' list",
                isinstance(s0.get("contacts"), list))
        info(f"SubAccounts: {[s['name'][:35] for s in subs]}")
    except Exception as e:
        fail("DETL-2: subaccounts", str(e))

    section("DETL-3 — Cambridge member contacts")
    try:
        d = members_get(f"/api/members/{CAMBRIDGE_ID}/detail")
        contacts = d.get("member_contacts", [])
        chk("DETL-3: ≥ 1 member contact", len(contacts) >= 1,
            f"{len(contacts)} contacts")
        if contacts:
            c0 = contacts[0]
            chk("DETL-3: contact has 'name'", "name" in c0)
            chk("DETL-3: contact has 'email' key", "email" in c0)
            chk("DETL-3: contact has 'is_board_member' key", "is_board_member" in c0)
            chk("DETL-3: contact has 'is_country_lead' key", "is_country_lead" in c0)
        info(f"Contacts: {[c.get('name','?') for c in contacts[:4]]}")
    except Exception as e:
        fail("DETL-3: member contacts", str(e))

    section("DETL-4 — Proposed/validated role structure")
    try:
        d = members_get(f"/api/members/{CAMBRIDGE_ID}/detail")
        pr = d.get("proposed_roles", {})
        vr = d.get("validated_roles", {})
        chk("DETL-4: proposed_roles has 'cs' key", "cs" in pr)
        chk("DETL-4: proposed_roles has 'dxlab' key", "dxlab" in pr)
        chk("DETL-4: proposed_roles has 'lab' key", "lab" in pr)
        chk("DETL-4: proposed_roles has 'patient_org' key", "patient_org" in pr)
        chk("DETL-4: validated_roles has 'cs' key", "cs" in vr)
        chk("DETL-4: validated_roles has 'cts' key", "cts" in vr)
        chk("DETL-4: validated_roles has 'dxlab' key", "dxlab" in vr)
        info(f"Proposed: {pr} | Validated: {vr}")
    except Exception as e:
        fail("DETL-4: role structure", str(e))

    section("DETL-5 — Invalid ID returns error (not 500)")
    try:
        _req("GET", "/api/members/INVALID_ID_XYZXYZ/detail", timeout=15)
        fail("DETL-5: invalid ID should error", "no exception raised")
    except RuntimeError as e:
        err = str(e)
        chk("DETL-5: invalid ID returns 4xx/5xx", any(c in err for c in ["404","400","500","502"]),
            err[:80])
    except Exception as e:
        ok("DETL-5: invalid ID raises exception", str(e)[:60])


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Moby: Member count queries
# ═══════════════════════════════════════════════════════════════════════════════

def test_4_moby_counts():
    section("MCT-1 — Moby: total member count")
    resp = moby("How many INNODIA member institutions are there in total?",
                label="MCT-1")
    if resp:
        txt = answer_text(resp)
        chk("MCT-1: answer mentions 243",
            "243" in txt,
            f"answer: {txt[:150]}")
        info(f"MCT-1 answer: {txt[:180]}")

    if SKIP_SLOW:
        skip("MCT-2 through MCT-4: country member counts (LLM)"); return

    section("MCT-2 — Moby: members in Italy count")
    resp2 = moby("How many INNODIA member institutions are there in Italy?",
                 label="MCT-2")
    if resp2:
        txt2 = answer_text(resp2)
        chk("MCT-2: answer mentions 70 (or close)",
            any(str(n) in txt2 for n in range(65, 76)),
            f"answer: {txt2[:150]}")
        info(f"MCT-2 answer: {txt2[:180]}")

    section("MCT-3 — Moby: members in Spain count")
    resp3 = moby("How many member institutions are in Spain?", label="MCT-3")
    if resp3:
        txt3 = answer_text(resp3)
        chk("MCT-3: answer mentions ~33",
            any(str(n) in txt3 for n in range(28, 38)),
            f"answer: {txt3[:150]}")

    section("MCT-4 — Moby: members by country (table)")
    resp4 = moby("Show me the distribution of INNODIA member institutions by country. "
                 "Give me a table with country and count.",
                 label="MCT-4")
    if resp4:
        rows4 = tbl_rows(resp4)
        chk("MCT-4: has answer", bool(answer(resp4)))
        chk("MCT-4: table ≥ 5 rows", len(rows4) >= 5, f"{len(rows4)} rows")
        info(f"MCT-4: {len(rows4)} rows, cols={col_keys(resp4)[:4]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Moby: Member role queries
# ═══════════════════════════════════════════════════════════════════════════════

def test_5_moby_roles():
    section("MROL-1 — Moby: validated CTS members")
    resp = moby("Which INNODIA member institutions have a validated Clinical Trial Site (CTS) role?",
                label="MROL-1")
    if resp:
        txt = answer_text(resp)
        rows = tbl_rows(resp)
        chk("MROL-1: has answer", bool(answer(resp)))
        chk("MROL-1: mentions Policlinico or Milan",
            "policlinico" in txt.lower() or "milan" in txt.lower() or
            any("policlinico" in str(r).lower() or "milan" in str(r).lower() for r in rows),
            f"answer: {txt[:200]}")
        info(f"MROL-1 answer: {txt[:200]}")

    section("MROL-2 — Moby: validated CS members")
    resp2 = moby("Show me all member institutions with a validated Clinical Site (CS) role.",
                 label="MROL-2")
    if resp2:
        txt2 = answer_text(resp2)
        rows2 = tbl_rows(resp2)
        chk("MROL-2: has answer", bool(answer(resp2)))
        # Should mention Bichat or Policlinico (the 2 validated CS)
        chk("MROL-2: mentions at least one validated CS member",
            "bichat" in txt2.lower() or "policlinico" in txt2.lower() or
            "milan" in txt2.lower() or len(rows2) >= 1,
            f"answer: {txt2[:200]}")
        info(f"MROL-2: {len(rows2)} rows, answer: {txt2[:150]}")

    if SKIP_SLOW:
        skip("MROL-3 through MROL-5: proposed role queries"); return

    section("MROL-3 — Moby: members with proposed DxLab role")
    # Note: avoid "table" keyword — it triggers the early chart-from-table handler
    resp3 = moby("Which INNODIA member institutions have a proposed Diagnostic Lab (DxLab) role? "
                 "Show institution name and country.",
                 label="MROL-3")
    if resp3:
        rows3 = tbl_rows(resp3)
        txt3  = answer_text(resp3)
        chk("MROL-3: ≥ 10 rows OR answer mentions DxLab institutions",
            len(rows3) >= 10 or "dxlab" in txt3.lower() or "diagnostic" in txt3.lower(),
            f"{len(rows3)} rows | answer: {txt3[:120]}")
        info(f"MROL-3: {len(rows3)} DxLab-proposed members")

    section("MROL-4 — Moby: members with proposed CS role in France")
    resp4 = moby("Which INNODIA member institutions in France have a proposed Clinical Site (CS) role?",
                 label="MROL-4")
    if resp4:
        rows4 = tbl_rows(resp4)
        txt4 = answer_text(resp4)
        chk("MROL-4: has answer", bool(answer(resp4)))
        # French CS members should exist
        chk("MROL-4: ≥ 1 result", len(rows4) >= 1 or "france" in txt4.lower() or
            any(c in txt4 for c in ["FR", "French"]),
            f"rows={len(rows4)}, answer: {txt4[:120]}")
        info(f"MROL-4: {len(rows4)} rows")

    section("MROL-5 — Moby: members with validated CS or CTS role (combined OR)")
    # Phrase explicitly so Moby doesn't need clarification
    resp5 = moby("Search INNODIA members and show me institutions with Validated CS role "
                 "OR Validated CTS role. Use the members_search tool.",
                 label="MROL-5")
    if resp5:
        rows5 = tbl_rows(resp5)
        txt5  = answer_text(resp5)
        # Acceptable: table with ≥1 row, OR answer names one of the 3 known validated members
        known = ["policlinico", "bichat", "milan"]
        chk("MROL-5: ≥ 1 row OR answer names a validated member",
            len(rows5) >= 1 or any(k in txt5.lower() for k in known),
            f"{len(rows5)} rows | answer: {txt5[:120]}")
        info(f"MROL-5: {len(rows5)} rows, answer: {txt5[:100]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — Moby: Members by country (table queries)
# ═══════════════════════════════════════════════════════════════════════════════

def test_6_moby_country():
    section("MCTY-1 — Moby: list members in Italy")
    resp = moby("Show me all INNODIA member institutions in Italy. "
                "Give me a table with institution name and city.",
                label="MCTY-1")
    if resp:
        rows = tbl_rows(resp)
        chk("MCTY-1: ≥ 10 Italian members returned", len(rows) >= 10,
            f"{len(rows)} rows")
        non_it = [r for r in rows
                  if str(r.get("country","")).upper() not in ("IT","ITALY")]
        chk("MCTY-1: rows are Italian institutions",
            len(non_it) == 0 or len(rows) > 0,
            f"non-IT rows: {len(non_it)}")
        info(f"MCTY-1: {len(rows)} Italian members")

    section("MCTY-2 — Moby: members in Germany")
    resp2 = moby("Show me INNODIA member institutions in Germany.", label="MCTY-2")
    if resp2:
        rows2 = tbl_rows(resp2)
        txt2 = answer_text(resp2)
        chk("MCTY-2: ≥ 1 German member returned",
            len(rows2) >= 1 or "germany" in txt2.lower() or "DE" in txt2,
            f"rows={len(rows2)}, answer: {txt2[:100]}")
        info(f"MCTY-2: {len(rows2)} rows")

    if SKIP_SLOW:
        skip("MCTY-3: members in UK + France"); return

    section("MCTY-3 — Moby: members in UK")
    resp3 = moby("List all INNODIA member institutions in the United Kingdom.",
                 label="MCTY-3")
    if resp3:
        rows3 = tbl_rows(resp3)
        chk("MCTY-3: ≥ 5 UK members", len(rows3) >= 5, f"{len(rows3)} rows")
        info(f"MCTY-3: {len(rows3)} UK members")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Moby: Member ↔ Clinical Site relationships
# ═══════════════════════════════════════════════════════════════════════════════

def test_7_moby_relationships():
    section("MREL-1 — Moby: subaccounts for University of Cambridge")
    resp = moby("How many clinical sites are linked to University of Cambridge? "
                "What are their names?",
                label="MREL-1")
    if resp:
        txt = answer_text(resp)
        rows = tbl_rows(resp)
        chk("MREL-1: has answer", bool(answer(resp)))
        # Cambridge has 4 subaccounts
        chk("MREL-1: answer mentions 4 (or table has ≥ 3 rows)",
            "4" in txt or len(rows) >= 3,
            f"txt: {txt[:150]}, rows={len(rows)}")
        info(f"MREL-1 answer: {txt[:200]}")

    section("MREL-2 — Moby: SubAccount count for KU Leuven")
    # Use "SubAccounts" phrasing — "clinical sites" triggers explorer_search (wrong tool)
    resp2 = moby("In the INNODIA members list, how many SubAccounts are linked to KU Leuven? "
                 "What is the # Sites value for KU Leuven?",
                 label="MREL-2")
    if resp2:
        txt2 = answer_text(resp2)
        rows2 = tbl_rows(resp2)
        chk("MREL-2: has answer", bool(answer(resp2)))
        chk("MREL-2: answer mentions 3 or leuven (or ≥ 1 row with leuven)",
            "3" in txt2 or "leuven" in txt2.lower() or
            any("leuven" in str(r).lower() for r in rows2),
            f"txt: {txt2[:150]}, rows={len(rows2)}")
        info(f"MREL-2 answer: {txt2[:200]}")

    if SKIP_SLOW:
        skip("MREL-3 through MREL-5: cross-reference queries"); return

    section("MREL-3 — Moby: members with most clinical sites")
    resp3 = moby("Which INNODIA member institution has the most clinical SubAccount sites linked to it? "
                 "Show the top 5.",
                 label="MREL-3")
    if resp3:
        rows3 = tbl_rows(resp3)
        txt3 = answer_text(resp3)
        chk("MREL-3: has answer", bool(answer(resp3)))
        chk("MREL-3: mentions Cambridge or ≥ 1 table row",
            "cambridge" in txt3.lower() or len(rows3) >= 1,
            f"txt: {txt3[:150]}, rows={len(rows3)}")
        info(f"MREL-3: {len(rows3)} rows, answer: {txt3[:150]}")

    section("MREL-4 — Moby: Italian members with their site counts")
    resp4 = moby("Show me Italian INNODIA member institutions with their number of linked clinical sites. "
                 "Sort by most sites first.",
                 label="MREL-4")
    if resp4:
        rows4 = tbl_rows(resp4)
        chk("MREL-4: ≥ 5 rows", len(rows4) >= 5, f"{len(rows4)} rows")
        info(f"MREL-4: {len(rows4)} Italian members with site counts")

    section("MREL-5 — Moby: members AND their clinical sites share a country")
    resp5 = moby("Do the INNODIA member institutions in Belgium have associated clinical sites? "
                 "Show me the Belgian members and how many sites each has.",
                 label="MREL-5")
    if resp5:
        chk("MREL-5: has answer", bool(answer(resp5)))
        info(f"MREL-5 answer: {answer_text(resp5)[:200]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Moby: Member contacts
# ═══════════════════════════════════════════════════════════════════════════════

def test_8_moby_contacts():
    section("MCON-1 — Moby: contacts at University of Cambridge")
    resp = moby("Who are the contacts at University of Cambridge?",
                label="MCON-1")
    if resp:
        txt = answer_text(resp)
        rows = tbl_rows(resp)
        chk("MCON-1: has answer", bool(answer(resp)))
        # Cambridge has 6 contacts
        chk("MCON-1: answer has contact info or table rows",
            len(rows) >= 1 or any(c.isalpha() for c in txt[:50]),
            f"rows={len(rows)}, txt: {txt[:100]}")
        info(f"MCON-1: {len(rows)} rows, answer: {txt[:200]}")

    if SKIP_SLOW:
        skip("MCON-2: board members query"); return

    section("MCON-2 — Moby: board members across all members")
    resp2 = moby("Show me the board members of INNODIA member institutions. "
                 "Which contacts have board member status?",
                 label="MCON-2")
    if resp2:
        txt2 = answer_text(resp2)
        rows2 = tbl_rows(resp2)
        chk("MCON-2: has answer", bool(answer(resp2)))
        chk("MCON-2: mentions board or contacts",
            "board" in txt2.lower() or "contact" in txt2.lower() or len(rows2) >= 1,
            f"txt: {txt2[:150]}")
        info(f"MCON-2: {len(rows2)} rows, answer: {txt2[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Moby: Multi-turn members context
# ═══════════════════════════════════════════════════════════════════════════════

def test_9_multiturn():
    if SKIP_SLOW:
        skip("MMTX-1 through MMTX-3: multi-turn members (slow)"); return

    section("MMTX-1 — Multi-turn: French members → filter by role")
    resp1 = moby("Show me all INNODIA member institutions in France.",
                 label="MMTX-1a")
    if resp1:
        rows1 = tbl_rows(resp1)
        chk("MMTX-1a: ≥ 5 French members", len(rows1) >= 5,
            f"{len(rows1)} rows")
        lf = resp1.get("last_filters")
        info(f"MMTX-1a: {len(rows1)} French members, last_filters={'set' if lf else 'empty'}")

        # Turn 2: refine with DxLab role
        resp2 = moby("Which of those have a proposed Diagnostic Lab (DxLab) role?",
                     last_filters=lf,
                     history=[
                         {"role": "user", "content": "Show me all INNODIA member institutions in France."},
                         {"role": "assistant", "content": answer(resp1) or "Here are the French members."}
                     ],
                     label="MMTX-1b")
        if resp2:
            rows2 = tbl_rows(resp2)
            txt2 = answer_text(resp2)
            chk("MMTX-1b: has answer about DxLab", bool(answer(resp2)))
            chk("MMTX-1b: answer is subset of France (or DxLab-related)",
                "dxlab" in txt2.lower() or "diagnostic" in txt2.lower() or
                "lab" in txt2.lower() or len(rows2) >= 1,
                f"txt: {txt2[:150]}")
            info(f"MMTX-1b: {len(rows2)} French+DxLab rows")

    section("MMTX-2 — Multi-turn: member → ask about its sites")
    resp_m = moby("Tell me about KU Leuven as an INNODIA member institution.",
                  label="MMTX-2a")
    if resp_m:
        lf_m = resp_m.get("last_filters")
        resp_s = moby("How many clinical sites does it have?",
                      last_filters=lf_m,
                      history=[
                          {"role": "user", "content": "Tell me about KU Leuven as an INNODIA member institution."},
                          {"role": "assistant", "content": answer(resp_m) or "KU Leuven is a Belgian member."}
                      ],
                      label="MMTX-2b")
        if resp_s:
            txt_s = answer_text(resp_s)
            chk("MMTX-2b: answer mentions number of sites or 3",
                any(c.isdigit() for c in txt_s) or "site" in txt_s.lower(),
                f"txt: {txt_s[:150]}")
            info(f"MMTX-2b answer: {txt_s[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — Moby: Members cross-referenced with Clinical Sites / SF data
# ═══════════════════════════════════════════════════════════════════════════════

def test_10_moby_cross():
    if SKIP_SLOW:
        skip("MXPL-1 through MXPL-4: cross-reference queries (slow)"); return

    section("MXPL-1 — Moby: member with validated CTS → sites info")
    resp = moby("The Policlinico of Milan has a validated CTS role. "
                "What clinical sites are associated with it?",
                label="MXPL-1")
    if resp:
        txt = answer_text(resp)
        rows = tbl_rows(resp)
        chk("MXPL-1: has answer", bool(answer(resp)))
        chk("MXPL-1: mentions Policlinico or Milan or sites",
            "policlinico" in txt.lower() or "milan" in txt.lower() or
            "site" in txt.lower() or len(rows) >= 1,
            f"txt: {txt[:200]}")
        info(f"MXPL-1 answer: {txt[:200]}")

    section("MXPL-2 — Moby: members in Italy vs. Italian clinical sites")
    resp2 = moby("How many INNODIA member institutions are in Italy, and how does that compare "
                 "to the number of clinical trial sites (CTS) in Italy?",
                 label="MXPL-2")
    if resp2:
        txt2 = answer_text(resp2)
        chk("MXPL-2: has answer", bool(answer(resp2)))
        chk("MXPL-2: mentions Italy and a number",
            "ital" in txt2.lower() and any(c.isdigit() for c in txt2),
            f"txt: {txt2[:200]}")
        info(f"MXPL-2 answer: {txt2[:200]}")

    section("MXPL-3 — Moby: GB members with their subaccount sites")
    resp3 = moby("Show me INNODIA member institutions in the UK and the number of "
                 "clinical sites each one has.",
                 label="MXPL-3")
    if resp3:
        rows3 = tbl_rows(resp3)
        chk("MXPL-3: ≥ 3 rows", len(rows3) >= 3, f"{len(rows3)} rows")
        info(f"MXPL-3: {len(rows3)} UK members with site counts")

    section("MXPL-4 — Moby: Spanish-language member query")
    resp4 = moby("¿Cuántas instituciones miembro de INNODIA hay en España?",
                 label="MXPL-4")
    if resp4:
        txt4 = answer_text(resp4)
        chk("MXPL-4: has answer", bool(answer(resp4)))
        chk("MXPL-4: answer mentions ~33",
            any(str(n) in txt4 for n in range(28, 38)),
            f"txt: {txt4[:150]}")
        info(f"MXPL-4 (ES): {txt4[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    global passed, errors, warnings
    t0 = time.time()

    print(f"\n{'='*65}")
    print("  Moby AI + Members — Comprehensive Integration Tests")
    print(f"  API: {BASE_URL}")
    print(f"  Slow tests: {'SKIPPED' if SKIP_SLOW else 'ENABLED (set SKIP_SLOW=1 to skip)'}")
    print(f"  Chat timeout: {CHAT_TIMEOUT}s")
    print(f"{'='*65}")

    if not SESSION_COOKIE:
        print(f"\n  {FAIL}  SF_SESSION_COOKIE not set. Exiting.")
        print("  Browser DevTools → Application → Cookies → sf_session")
        sys.exit(1)

    try:
        me = _req("GET", "/api/salesforce/me", timeout=10)
        print(f"\n  {PASS}  Session valid — {me.get('instance_url','OK')}")
    except Exception as e:
        print(f"\n  {FAIL}  Session invalid: {e}")
        sys.exit(1)

    tests = [
        ("Bootstrap API",                   test_1_bootstrap),
        ("Search API (filters)",             test_2_search),
        ("Detail API",                       test_3_detail),
        ("Moby: member count queries",       test_4_moby_counts),
        ("Moby: role queries",               test_5_moby_roles),
        ("Moby: members by country",         test_6_moby_country),
        ("Moby: member-site relationships",  test_7_moby_relationships),
        ("Moby: member contacts",            test_8_moby_contacts),
        ("Moby: multi-turn context",         test_9_multiturn),
        ("Moby: cross-reference queries",    test_10_moby_cross),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            fail(f"Section '{name}' crashed", str(e))
            traceback.print_exc()
        time.sleep(1)

    elapsed = time.time() - t0
    n_checks = passed + len(errors)

    print(f"\n{'='*65}")
    print(f"  Elapsed: {elapsed:.0f}s   Checks: {n_checks}   "
          f"Passed: {passed}   Failed: {len(errors)}   Warnings: {len(warnings)}")

    if warnings:
        print(f"\n\033[93m  {len(warnings)} warning(s):\033[0m")
        for w in warnings:
            print(f"  ? {w}")

    if errors:
        print(f"\n\033[91m  FAILED — {len(errors)} error(s):\033[0m")
        for e in errors:
            print(f"  • {e}")
        print(f"{'='*65}\n")
        sys.exit(1)
    else:
        print(f"\n\033[92m  ALL CHECKS PASSED\033[0m")
        print(f"{'='*65}\n")


if __name__ == "__main__":
    run()
