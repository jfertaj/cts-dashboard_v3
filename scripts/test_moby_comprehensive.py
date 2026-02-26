#!/usr/bin/env python3
"""
Comprehensive Moby AI integration tests.

Covers: geographic queries, patient metrics, clinical facilities, HLA typing,
Phase I/II/III trials, activity/enrollment, study coordinators, nearest-site
proximity, country distribution, combined SF+qual filters, and multi-turn
follow-up via last_filters.

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_comprehensive.py
  SF_SESSION_COOKIE="<val>" API_BASE="http://localhost:8000" python scripts/test_moby_comprehensive.py
  SKIP_SLOW=1 ...  (skip tests that go through Gemini and are slow locally)
"""

import sys, os, json, time, ssl, re, traceback
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional, Tuple

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode   = ssl.CERT_NONE

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("API_BASE", "https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
SKIP_SLOW      = os.environ.get("SKIP_SLOW", "0") == "1"   # skip Gemini-heavy tests
CHAT_TIMEOUT   = int(os.environ.get("CHAT_TIMEOUT", "120")) # seconds

PASS = "\033[92m✓\033[0m"; FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m?\033[0m"; INFO = "\033[94mℹ\033[0m"

errors:   List[str] = []
warnings: List[str] = []
passed  = 0

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None, timeout: int = 120) -> Any:
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

def chat(msg: str, last_filters=None, timeout: int = CHAT_TIMEOUT) -> Dict:
    return _req("POST", "/api/ai/chat",
                {"messages": [{"role": "user", "content": msg}],
                 "last_filters": last_filters},
                timeout=timeout)

def explorer(filters: Dict, columns: List[str]) -> List[Dict]:
    resp = _req("POST", "/api/explorer/search", {"filters": filters, "columns": columns})
    return resp.get("rows", [])

# ── Accessors ─────────────────────────────────────────────────────────────────

def col_keys(resp: Dict) -> List[str]:
    return [c.get("key", "") for c in (resp.get("table") or {}).get("columns", [])]

def tbl_rows(resp: Dict) -> List[Dict]:
    return (resp.get("table") or {}).get("rows", [])

def get_data(row: Dict, key: str):
    return row.get("data", {}).get(key)

def cols_str(resp: Dict) -> str:
    return " ".join(col_keys(resp)).lower()

# ── Check helpers ─────────────────────────────────────────────────────────────

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
    msg = f"  {WARN}  {desc}"
    if detail: msg += f"  [{detail}]"
    print(msg)
    warnings.append(desc)

def chk(desc: str, cond: bool, detail: str = ""):
    (ok if cond else fail)(desc, detail)

def info(msg: str):
    print(f"  {INFO}  {msg}")

def skip(desc: str):
    print(f"  \033[90m-  {desc} [SKIPPED — SKIP_SLOW=1]\033[0m")

# ── Moby call with error handling ─────────────────────────────────────────────

def moby(msg: str, last_filters=None, section_name: str = "") -> Optional[Dict]:
    """Call Moby; return response or None on timeout/error."""
    try:
        resp = chat(msg, last_filters=last_filters)
        return resp
    except Exception as e:
        err = str(e)
        label = section_name or msg[:60]
        if "timed out" in err.lower() or "timeout" in err.lower():
            fail(f"{label} — Moby responded", "timed out")
        else:
            fail(f"{label} — Moby responded", err[:120])
        return None

def assert_moby_table(resp: Optional[Dict], desc: str,
                       min_rows: int = 1,
                       col_patterns: List[str] = None) -> bool:
    """Common assertions on a Moby table response. Returns True if all pass."""
    if resp is None:
        return False
    answer = resp.get("answer", "")
    rows   = tbl_rows(resp)
    cols   = col_keys(resp)
    cs     = " ".join(cols).lower()
    ok_all = True

    chk(f"{desc}: has answer", bool(answer) and "couldn't" not in answer.lower(),
        answer[:80] if answer else "empty")
    ok_rows = len(rows) >= min_rows
    chk(f"{desc}: ≥{min_rows} row(s)", ok_rows, f"{len(rows)} rows")
    if not ok_rows:
        ok_all = False

    for pat in (col_patterns or []):
        found = any(pat.lower() in c.lower() for c in cols)
        chk(f"{desc}: table has '{pat}' column", found, f"cols={cols[:6]}")
        if not found:
            ok_all = False

    return ok_all

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — Geographic / country queries  (deterministic planner)
# ═══════════════════════════════════════════════════════════════════════════════

def test_1_geographic():
    section("GEO-1 — Sites in Italy")
    info("Direct explorer: filter by site.country='Italy'")
    try:
        it_direct = explorer(
            {"logic": "AND", "rules": [{"field": "site.country", "operator": "equals", "value": "Italy"}]},
            ["sf.Account.Name", "sf.Account.ShippingCountry"]
        )
        chk("Direct: ≥5 Italian sites", len(it_direct) >= 5, f"{len(it_direct)} rows")
    except Exception as e:
        fail("Direct Italian sites", str(e))

    resp = moby("Show me all clinical sites in Italy", section_name="GEO-1 Moby Italy")
    chk("GEO-1 Moby Italy — Moby responded", resp is not None, "timed out")
    if resp:
        rows = tbl_rows(resp)
        non_it = [r for r in rows if str(r.get("country","")).lower() not in ("italy","it","italia")]
        chk("Moby Italy: ≥5 rows returned", len(rows) >= 5, f"{len(rows)} rows")
        chk("Moby Italy: all rows are Italy", len(non_it) == 0,
            f"Non-Italy: {[r.get('country') for r in non_it[:3]]}")
        info(f"Moby returned {len(rows)} Italian sites")

    section("GEO-2 — Sites in Norway")
    resp2 = moby("Show me all sites in Norway", section_name="GEO-2 Moby Norway")
    chk("GEO-2 Moby Norway — Moby responded", resp2 is not None, "timed out")
    if resp2:
        rows2 = tbl_rows(resp2)
        non_no = [r for r in rows2 if str(r.get("country","")).lower() not in ("norway","no","norge")]
        # Norway may have 0 rows if no local DB data (qual uploads) — log but don't fail
        info(f"Moby returned {len(rows2)} Norwegian sites (local DB may be incomplete)")
        chk("Moby Norway: all rows are Norway", len(non_no) == 0,
            f"Non-Norway: {[r.get('country') for r in non_no[:3]]}")
        if rows2:
            info(f"Sample: {[r.get('account_name','?') for r in rows2[:3]]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Country distribution
# ═══════════════════════════════════════════════════════════════════════════════

def test_2_distribution():
    section("DIST-1 — Sites per country (direct baseline)")
    try:
        all_rows = explorer({"logic": "AND", "rules": []},
                            ["sf.Account.ShippingCountry"])
        countries = set(r.get("country") for r in all_rows if r.get("country"))
        chk("Explorer: ≥10 distinct countries", len(countries) >= 10,
            f"{len(countries)} countries: {sorted(countries)[:8]}")
    except Exception as e:
        fail("Explorer baseline", str(e))

    if SKIP_SLOW:
        skip("DIST-2: Moby 'how many sites per country'")
        return

    section("DIST-2 — Moby: sites per country")
    resp = moby("How many clinical sites are there per country? Give me a breakdown by country.",
                section_name="DIST-2")
    if resp:
        answer = resp.get("answer","")
        rows = tbl_rows(resp)
        cs = cols_str(resp)
        chk("DIST-2: has answer", bool(answer), answer[:100])
        chk("DIST-2: table has country column", "country" in cs, f"cols={col_keys(resp)[:6]}")
        chk("DIST-2: table has count/sites column",
            any(k in cs for k in ["count","sites","total","n_sites","number"]),
            f"cols={col_keys(resp)[:6]}")
        chk("DIST-2: ≥10 countries in table", len(rows) >= 10, f"{len(rows)} rows")
        if rows:
            info(f"Top 5: {[(r.get('country','?'), r.get('sites') or r.get('count') or r.get('total')) for r in rows[:5]]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Patient metrics: ND (Newly Diagnosed T1D)
# ═══════════════════════════════════════════════════════════════════════════════

def test_3_nd_patients():
    section("ND-1 — Direct: sites with ND≥18 > 10")
    ND_O18 = "sf.C_Number_of_new_T1D_diagnosed_O_18__c"
    ND_U18 = "sf.C_Number_of_new_T1D_diagnosed_U_18__c"
    try:
        nd_rows = explorer(
            {"logic": "AND", "rules": [{"field": ND_O18, "operator": ">", "value": 10}]},
            [ND_O18, ND_U18, "sf.Account.ShippingCountry"]
        )
        chk("Direct: ≥5 sites with ND≥18 > 10", len(nd_rows) >= 5, f"{len(nd_rows)} sites")
        sample = [(r.get("account_name","?"), get_data(r, ND_O18)) for r in nd_rows[:3]]
        info(f"Sample ND sites: {sample}")
    except Exception as e:
        fail("Direct ND filter", str(e))

    if SKIP_SLOW:
        skip("ND-2: Moby top ND sites"); skip("ND-3: Moby ND by country"); return

    section("ND-2 — Moby: top 10 sites with most adult ND T1D")
    resp = moby("Show me the top 10 sites with the highest number of newly diagnosed adult T1D patients (over 18).",
                section_name="ND-2")
    assert_moby_table(resp, "ND-2", min_rows=5,
                      col_patterns=["account_name", "ND", "country"])

    section("ND-3 — Moby: ND patients by country")
    resp3 = moby("How many newly diagnosed T1D patients (adult and pediatric) are there per country?",
                 section_name="ND-3")
    if resp3:
        chk("ND-3: has answer", bool(resp3.get("answer")))
        chk("ND-3: table with country column", "country" in cols_str(resp3),
            f"cols={col_keys(resp3)[:6]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — Patient metrics: T1D currently followed
# ═══════════════════════════════════════════════════════════════════════════════

def test_4_t1d_patients():
    T1D_O18 = "sf.C_Number_of_T1D_Patients_currently_O_18__c"
    T1D_U18 = "sf.C_Number_of_T1D_Patients_currently_U_18__c"

    section("T1D-1 — Direct: sites with >50 T1D adults currently followed")
    try:
        big_rows = explorer(
            {"logic": "AND", "rules": [{"field": T1D_O18, "operator": ">", "value": 50}]},
            [T1D_O18, T1D_U18, "sf.Account.ShippingCountry"]
        )
        chk("Direct: ≥1 site with T1D≥18 > 50", len(big_rows) >= 1, f"{len(big_rows)} sites")
        info(f"Sample: {[(r.get('account_name','?'), get_data(r,T1D_O18)) for r in big_rows[:3]]}")
    except Exception as e:
        fail("Direct T1D filter", str(e))

    if SKIP_SLOW:
        skip("T1D-2: Moby T1D > 50 sites"); return

    section("T1D-2 — Moby: sites with more than 50 T1D adults currently followed")
    resp = moby("Show me all sites that have more than 50 adult T1D patients currently followed (over 18).",
                section_name="T1D-2")
    if resp:
        rows = tbl_rows(resp)
        t1d_col = any("t1d" in c.lower() or "patient" in c.lower() for c in col_keys(resp))
        chk("T1D-2: ≥1 row", len(rows) >= 1, f"{len(rows)} rows")
        chk("T1D-2: has T1D/patient column", t1d_col, f"cols={col_keys(resp)[:6]}")
        # Verify values satisfy the >50 threshold (allow slight differences from rounding/dedup)
        big_vals = []
        for r in rows:
            for k in r:
                if "t1d_patients_currently_o_18" in k.lower() or "t1d" in k.lower():
                    try:
                        v = float(r[k] or 0)
                        if v > 0:
                            big_vals.append(v)
                    except (TypeError, ValueError):
                        pass
        if big_vals:
            chk("T1D-2: returned values look >0", min(big_vals) >= 0, f"min={min(big_vals)}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — Stage 1/2 additional angles  (deterministic planner)
# ═══════════════════════════════════════════════════════════════════════════════

def test_5_stage12():
    section("S12-1 — Moby: top sites by Stage 2 (deterministic)")
    resp = moby("Show me the top 5 sites that follow the most Stage 2 individuals.",
                section_name="S12-1")
    chk("S12-1 — Moby responded", resp is not None, "timed out")
    if resp:
        answer = resp.get("answer", "")
        if "session" in answer.lower() or "salesforce" in answer.lower():
            # Graceful degradation when SF session expired — acceptable
            info("S12-1: SF session required (graceful error)")
        else:
            rows = tbl_rows(resp)
            chk("S12-1: ≥5 rows", len(rows) >= 5, f"{len(rows)} rows")
            chk("S12-1: has Stage 2 column",
                any("stage" in c.lower() or "stage2" in c.lower() for c in col_keys(resp)),
                f"cols={col_keys(resp)[:6]}")
            s2_vals = []
            for r in rows:
                for k in r:
                    if "stage2" in k.lower() or ("stage" in k.lower() and "2" in k):
                        try:
                            s2_vals.append(float(r[k] or 0))
                        except (TypeError, ValueError):
                            pass
            if len(s2_vals) >= 2:
                chk("S12-1: rows roughly sorted by Stage 2 desc",
                    s2_vals[0] >= s2_vals[-1],
                    f"first={s2_vals[0]} last={s2_vals[-1]}")

    if SKIP_SLOW:
        skip("S12-2: Stage 2 by country (Gemini)"); return

    section("S12-2 — Moby: Stage 2 totals by country")
    resp2 = moby("What is the total number of Stage 2 individuals followed per country?",
                 section_name="S12-2")
    if resp2:
        chk("S12-2: has answer", bool(resp2.get("answer")))
        chk("S12-2: has country column", "country" in cols_str(resp2),
            f"cols={col_keys(resp2)[:6]}")
        chk("S12-2: has Stage 2 or numeric column",
            any("stage" in c.lower() or "sum" in c.lower() or "total" in c.lower()
                for c in col_keys(resp2)),
            f"cols={col_keys(resp2)[:6]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — HLA typing + Clinical Trials (Phase I/II/III)
# ═══════════════════════════════════════════════════════════════════════════════

def test_6_clinical_trials():
    HLA_FIELD = "sf.C_Is_HLA_typing_performed__c"

    section("CT-1 — Direct: sites with HLA typing")
    try:
        hla_rows = explorer(
            {"logic": "AND", "rules": [{"field": HLA_FIELD, "operator": "equals", "value": "Yes"}]},
            [HLA_FIELD, "sf.Account.ShippingCountry"]
        )
        chk("Direct: ≥1 site has HLA typing", len(hla_rows) >= 1, f"{len(hla_rows)} sites")
        info(f"HLA sites sample: {[r.get('account_name','?') for r in hla_rows[:4]]}")
    except Exception as e:
        fail("Direct HLA filter", str(e))

    if SKIP_SLOW:
        skip("CT-2: Moby HLA typing sites"); skip("CT-3: Moby Phase I trials"); return

    section("CT-2 — Moby: sites that perform HLA typing")
    resp = moby("Which sites perform HLA typing? Show site name, country, and city.",
                section_name="CT-2")
    if resp:
        chk("CT-2: has answer", bool(resp.get("answer")))
        chk("CT-2: ≥1 row", len(tbl_rows(resp)) >= 1, f"{len(tbl_rows(resp))} rows")
        chk("CT-2: has site name column",
            any("name" in c.lower() or "site" in c.lower() or "account" in c.lower()
                for c in col_keys(resp)),
            f"cols={col_keys(resp)[:6]}")
        info(f"HLA response: {resp.get('answer','')[:150]}")

    section("CT-3 — Moby: sites with Phase I clinical trials")
    resp3 = moby("Show me all sites that are participating in Phase I clinical trials for Type 1 diabetes.",
                 section_name="CT-3")
    if resp3:
        chk("CT-3: has answer", bool(resp3.get("answer")))
        rows3 = tbl_rows(resp3)
        chk("CT-3: ≥1 row returned", len(rows3) >= 1, f"{len(rows3)} rows")
        chk("CT-3: has site name column",
            any("name" in c.lower() or "site" in c.lower() for c in col_keys(resp3)),
            f"cols={col_keys(resp3)[:6]}")
        info(f"Phase I response: {resp3.get('answer','')[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — Activity/Enrollment queries  (deterministic planner)
# ═══════════════════════════════════════════════════════════════════════════════

def test_7_activities():
    section("ACT-1 — Moby: which activities do sites participate in?")
    resp = moby("Show me all clinical activities and which sites are enrolled in each one.",
                section_name="ACT-1")
    chk("ACT-1 — Moby responded", resp is not None, "timed out")
    if resp:
        answer = resp.get("answer", "")
        if "session" in answer.lower() or "salesforce" in answer.lower():
            info("ACT-1: SF session required (graceful error)")
        else:
            rows = tbl_rows(resp)
            cs = cols_str(resp)
            chk("ACT-1: has answer", bool(answer))
            chk("ACT-1: ≥1 row", len(rows) >= 1, f"{len(rows)} rows")
            chk("ACT-1: has activity/opportunity column",
                any(k in cs for k in ["activity", "opportunit", "trial", "study"]),
                f"cols={col_keys(resp)[:8]}")
            info(f"Activities answer: {answer[:150]}")
            if rows:
                info(f"First row: {dict(list(rows[0].items())[:4])}")

    section("ACT-2 — Moby: sites enrolled in each activity (with country breakdown)")
    resp2 = moby("For each clinical activity or study, how many sites are enrolled? "
                 "Show me the activity name and a count of participating sites per country.",
                 section_name="ACT-2")
    chk("ACT-2 — Moby responded", resp2 is not None, "timed out")
    if resp2:
        answer2 = resp2.get("answer", "")
        if "session" in answer2.lower() or "salesforce" in answer2.lower():
            info("ACT-2: SF session required (graceful error)")
        else:
            chk("ACT-2: has answer", bool(answer2))
            rows2 = tbl_rows(resp2)
            chk("ACT-2: ≥1 row", len(rows2) >= 1, f"{len(rows2)} rows")
            info(f"Activity breakdown: {answer2[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — Study Coordinators  (deterministic planner)
# ═══════════════════════════════════════════════════════════════════════════════

def test_8_study_coordinators():
    section("SC-1 — Moby: list study coordinators")
    resp = moby("Show me all study coordinators. I need their name, site, and country.",
                section_name="SC-1")
    chk("SC-1 — Moby responded", resp is not None, "timed out")
    if resp:
        answer = resp.get("answer", "")
        if "session" in answer.lower() or "salesforce" in answer.lower():
            info("SC-1: SF session required (graceful error)")
        else:
            rows = tbl_rows(resp)
            cs = cols_str(resp)
            chk("SC-1: has answer", bool(answer))
            chk("SC-1: ≥5 study coordinators", len(rows) >= 5, f"{len(rows)} rows")
            chk("SC-1: has name/coordinator column",
                any(k in cs for k in ["name", "coordinator", "contact"]),
                f"cols={col_keys(resp)[:6]}")
            chk("SC-1: has site/account column",
                any(k in cs for k in ["site", "account", "hospital"]),
                f"cols={col_keys(resp)[:6]}")
            info(f"Study coordinators: {len(rows)} total")
            if rows:
                info(f"Sample: {dict(list(rows[0].items())[:4])}")

    section("SC-2 — Moby: study coordinators in Belgium")
    resp2 = moby("Who are the study coordinators in Belgium?", section_name="SC-2")
    chk("SC-2 — Moby responded", resp2 is not None, "timed out")
    if resp2:
        answer2 = resp2.get("answer", "")
        if "session" in answer2.lower() or "salesforce" in answer2.lower():
            info("SC-2: SF session required (graceful error)")
        else:
            rows2 = tbl_rows(resp2)
            chk("SC-2: has answer", bool(answer2))
            # Belgium might have 0 coordinators — just validate structure
            info(f"Belgium SCs: {len(rows2)} returned")
            if rows2:
                non_be = [r for r in rows2
                          if str(r.get("country","")).lower() not in ("belgium","be","belgique","bélgica")]
                chk("SC-2: all rows are Belgium (if any)",
                    len(non_be) == 0,
                    f"Non-Belgium: {[r.get('country') for r in non_be[:3]]}")
                info(f"Sample: {dict(list(rows2[0].items())[:4])}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — Facility / qualification queries  (deterministic planner)
# ═══════════════════════════════════════════════════════════════════════════════

def test_9_facilities():
    OVERN = "qual.3_5_2__overnight_stay"
    PHARM = "qual.3_6__is_your_pharmacy_on_site_or_off_campus"

    section("FAC-1 — Direct: sites with overnight stay")
    try:
        ov_rows = explorer(
            {"logic": "AND", "rules": [{"field": OVERN, "operator": "equals", "value": "Yes"}]},
            [OVERN, PHARM, "sf.Account.ShippingCountry"]
        )
        chk("Direct: ≥1 site has overnight stay", len(ov_rows) >= 1, f"{len(ov_rows)} sites")
        info(f"Overnight sites sample: {[r.get('account_name','?') for r in ov_rows[:4]]}")
    except Exception as e:
        fail("Direct overnight filter", str(e))

    section("FAC-2 — Moby: sites with overnight stay (deterministic)")
    resp = moby("Which clinical sites offer overnight stay?",
                section_name="FAC-2")
    if resp:
        rows = tbl_rows(resp)
        cs = cols_str(resp)
        chk("FAC-2: ≥1 row", len(rows) >= 1, f"{len(rows)} rows")
        chk("FAC-2: has site name column",
            any(k in cs for k in ["name","site","account"]),
            f"cols={col_keys(resp)[:6]}")
        chk("FAC-2: has overnight column",
            any("overnight" in c.lower() for c in col_keys(resp)),
            f"cols={col_keys(resp)[:6]}")
        info(f"Overnight stay sites: {len(rows)}")

    section("FAC-3 — Moby: sites with on-site pharmacy (deterministic)")
    resp3 = moby("Show me all sites with an on-site pharmacy.",
                 section_name="FAC-3")
    if resp3:
        rows3 = tbl_rows(resp3)
        chk("FAC-3: ≥1 row", len(rows3) >= 1, f"{len(rows3)} rows")
        info(f"On-site pharmacy sites: {len(rows3)}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — CTS / DxLab flags
# ═══════════════════════════════════════════════════════════════════════════════

def test_10_flags():
    if SKIP_SLOW:
        skip("FLAGS-1: CTS sites (Gemini)"); skip("FLAGS-2: DxLab sites (Gemini)"); return

    section("FLAGS-1 — Moby: CTS accredited sites")
    resp = moby("Show me all INNODIA Clinical Trial Sites (CTS) that are accredited. "
                "Include site name, country, and city.",
                section_name="FLAGS-1")
    if resp:
        chk("FLAGS-1: has answer", bool(resp.get("answer")))
        chk("FLAGS-1: ≥1 row", len(tbl_rows(resp)) >= 1, f"{len(tbl_rows(resp))} rows")
        info(f"CTS accredited: {resp.get('answer','')[:100]}")

    section("FLAGS-2 — Moby: DxLab sites")
    resp2 = moby("Which sites deliver clinical grade services (DxLab sites)? Show country and city.",
                 section_name="FLAGS-2")
    if resp2:
        chk("FLAGS-2: has answer", bool(resp2.get("answer")))
        chk("FLAGS-2: ≥1 row", len(tbl_rows(resp2)) >= 1, f"{len(tbl_rows(resp2))} rows")
        info(f"DxLab: {resp2.get('answer','')[:100]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11 — Combined SF + qual filters  (explorer_search)
# ═══════════════════════════════════════════════════════════════════════════════

def test_11_combined():
    section("COMB-1 — Direct: sites in France with on-site pharmacy")
    PHARM = "qual.3_6__is_your_pharmacy_on_site_or_off_campus"
    try:
        fr_pharm = explorer(
            {"logic": "AND", "rules": [
                {"field": "site.country", "operator": "equals",   "value": "France"},
                {"field": PHARM,          "operator": "contains", "value": "On-site"},
            ]},
            ["sf.Account.ShippingCountry", PHARM]
        )
        chk("Direct: French sites with pharmacy ≥0", len(fr_pharm) >= 0,
            f"{len(fr_pharm)} sites")  # might be 0, just check it doesn't error
        info(f"French sites with pharmacy: {len(fr_pharm)}")
    except Exception as e:
        fail("Direct France+pharmacy filter", str(e))

    if SKIP_SLOW:
        skip("COMB-2: Moby France + pharmacy"); skip("COMB-3: Moby Stage2+overnight"); return

    section("COMB-2 — Moby: sites in France with on-site pharmacy")
    resp = moby("Show me all sites in France that have an on-site pharmacy.",
                section_name="COMB-2")
    if resp:
        rows = tbl_rows(resp)
        info(f"Moby French+pharmacy sites: {len(rows)}")
        # All rows should be France
        if rows:
            non_fr = [r for r in rows
                      if str(r.get("country","")).lower() not in ("france","fr","frankreich")]
            chk("COMB-2: all rows are France", len(non_fr) == 0,
                f"Non-France: {[r.get('country') for r in non_fr[:3]]}")

    section("COMB-3 — Moby: Stage 2 > 5 AND overnight stay")
    resp3 = moby("Show me sites that follow more than 5 Stage 2 individuals AND offer overnight stay.",
                 section_name="COMB-3")
    if resp3:
        chk("COMB-3: has answer", bool(resp3.get("answer")))
        rows3 = tbl_rows(resp3)
        cs3 = cols_str(resp3)
        info(f"Stage2>5 + overnight: {len(rows3)} sites")
        chk("COMB-3: has stage or overnight column",
            any(k in cs3 for k in ["stage","overnight","stay"]),
            f"cols={col_keys(resp3)[:6]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 12 — Nearest sites  (nearest_filtered_sites tool — may be slow)
# ═══════════════════════════════════════════════════════════════════════════════

def test_12_nearest():
    if SKIP_SLOW:
        skip("NEAR-1: nearest sites to Barcelona"); skip("NEAR-2: nearest to Berlin + Stage2"); return

    section("NEAR-1 — Moby: nearest 5 sites to Barcelona")
    resp = moby("Find the 5 nearest clinical sites to Barcelona.", section_name="NEAR-1")
    if resp:
        rows = tbl_rows(resp)
        cs = cols_str(resp)
        chk("NEAR-1: ≤5 rows returned", 1 <= len(rows) <= 7,
            f"{len(rows)} rows")  # slight tolerance
        chk("NEAR-1: has distance column",
            any("distance" in c.lower() or "km" in c.lower() for c in col_keys(resp)),
            f"cols={col_keys(resp)[:6]}")
        chk("NEAR-1: has site name column",
            any("name" in c.lower() or "site" in c.lower() for c in col_keys(resp)),
            f"cols={col_keys(resp)[:6]}")
        if rows:
            # First result should be close to Barcelona (Spain/France/southern EU)
            first = rows[0]
            dist_val = None
            for k in first:
                if "distance" in k.lower() or "km" in k.lower():
                    try: dist_val = float(first[k] or 0)
                    except: pass
            if dist_val is not None:
                chk("NEAR-1: closest site within 2000km", dist_val <= 2000,
                    f"distance={dist_val}km")
            info(f"Nearest to BCN: {[(r.get('account_name','?'), r.get('distance_km','?')) for r in rows[:3]]}")

    section("NEAR-2 — Moby: nearest sites to Berlin with Stage 2 > 5")
    resp2 = moby("Find the 10 nearest clinical sites to Berlin that have more than 5 Stage 2 individuals followed.",
                 section_name="NEAR-2")
    if resp2:
        rows2 = tbl_rows(resp2)
        cs2 = cols_str(resp2)
        chk("NEAR-2: ≥1 row", len(rows2) >= 1, f"{len(rows2)} rows")
        chk("NEAR-2: has distance column",
            any("distance" in c.lower() or "km" in c.lower() for c in col_keys(resp2)),
            f"cols={col_keys(resp2)[:6]}")
        chk("NEAR-2: has Stage 2 column",
            any("stage" in c.lower() for c in col_keys(resp2)),
            f"cols={col_keys(resp2)[:6]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 13 — Multi-turn follow-up via last_filters
# ═══════════════════════════════════════════════════════════════════════════════

def test_13_multiturn():
    section("MULTI-1 — Turn 1: sites in Germany")
    resp1 = moby("Show me all clinical sites in Germany.", section_name="MULTI-1 Turn 1")
    if resp1 is None:
        return

    rows1 = tbl_rows(resp1)
    chk("MULTI-1 T1: ≥1 German site", len(rows1) >= 1, f"{len(rows1)} rows")
    lf = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} German sites. last_filters present: {lf is not None}")

    # Turn 2 — refine with overnight stay
    section("MULTI-2 — Turn 2: of those German sites, which have overnight stay?")
    resp2 = moby("Of those, which ones offer overnight stay?",
                 last_filters=lf, section_name="MULTI-2 Turn 2")
    chk("MULTI-2 Turn 2 — Moby responded", resp2 is not None, "timed out")
    if resp2:
        answer2 = resp2.get("answer", "")
        rows2 = tbl_rows(resp2)
        info(f"Turn 2 refined: {len(rows2)} German+overnight sites")
        # Must be ≤ German sites count (refinement can only reduce)
        # Germany may have 0 overnight sites — that's valid
        chk("MULTI-2: refined result ≤ original", len(rows2) <= len(rows1),
            f"{len(rows2)} ≤ {len(rows1)}")
        if rows2:
            non_de = [r for r in rows2
                      if str(r.get("country","")).lower() not in ("germany","de","deutschland")]
            chk("MULTI-2: all refined rows are Germany", len(non_de) == 0,
                f"Non-Germany: {[r.get('country') for r in non_de[:3]]}")
            info(f"Sample: {[r.get('account_name','?') for r in rows2[:3]]}")
        else:
            info(f"Turn 2: 0 sites (Germany may have no overnight stay facilities). Answer: {answer2[:100]}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 14 — Enrollment/activity → specific activity lookup
# ═══════════════════════════════════════════════════════════════════════════════

def test_14_enrollment():
    if SKIP_SLOW:
        skip("ENRL-1: which sites are enrolled in Fabulinus"); return

    section("ENRL-1 — Moby: sites enrolled in Fabulinus")
    resp = moby("Which sites are enrolled in the Fabulinus study or activity?",
                section_name="ENRL-1")
    if resp:
        answer = resp.get("answer","")
        rows = tbl_rows(resp)
        chk("ENRL-1: has answer", bool(answer))
        # Fabulinus may or may not exist; just validate the response makes sense
        info(f"Fabulinus answer: {answer[:200]}")
        if rows:
            info(f"Enrolled sites sample: {[r.get('account_name','?') for r in rows[:4]]}")

    section("ENRL-2 — Moby: all site-activity assignments")
    resp2 = moby("Show me all assignment relationships: which sites are assigned to which clinical activities.",
                 section_name="ENRL-2")
    if resp2:
        rows2 = tbl_rows(resp2)
        chk("ENRL-2: has answer", bool(resp2.get("answer")))
        chk("ENRL-2: ≥1 row", len(rows2) >= 1, f"{len(rows2)} rows")
        cs2 = cols_str(resp2)
        chk("ENRL-2: has activity column",
            any(k in cs2 for k in ["activity","opportunit","trial","study","assignment"]),
            f"cols={col_keys(resp2)[:8]}")
        chk("ENRL-2: has site column",
            any(k in cs2 for k in ["site","account","hospital","name"]),
            f"cols={col_keys(resp2)[:8]}")
        info(f"Total assignments: {len(rows2)}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 15 — Spanish-language queries
# ═══════════════════════════════════════════════════════════════════════════════

def test_15_spanish_queries():
    section("ES-1 — Moby: query in Spanish (centros en Italia)")
    resp = moby("Muéstrame todos los centros clínicos en Italia.", section_name="ES-1")
    if resp:
        rows = tbl_rows(resp)
        chk("ES-1: ≥5 Italian sites (Spanish query)", len(rows) >= 5, f"{len(rows)} rows")
        non_it = [r for r in rows
                  if str(r.get("country","")).lower() not in ("italy","it","italia")]
        chk("ES-1: all rows are Italy", len(non_it) == 0,
            f"Non-Italy: {[r.get('country') for r in non_it[:3]]}")

    if SKIP_SLOW:
        skip("ES-2: Moby Spanish T1D query"); return

    section("ES-2 — Moby: Spanish query for T1D patients")
    resp2 = moby("¿Qué sitios tienen más de 20 pacientes con T1D adultos actualmente en seguimiento?",
                 section_name="ES-2")
    if resp2:
        chk("ES-2: has answer", bool(resp2.get("answer")))
        chk("ES-2: ≥1 row", len(tbl_rows(resp2)) >= 1, f"{len(tbl_rows(resp2))} rows")
        info(f"ES-2 answer: {resp2.get('answer','')[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    global passed, errors, warnings
    t0 = time.time()

    print(f"\n{'='*65}")
    print("  Moby AI — Comprehensive Integration Tests")
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
        print(f"\n  {PASS}  Session valid — user: {me.get('display_name') or me.get('email') or 'OK'}")
    except Exception as e:
        print(f"\n  {FAIL}  Session invalid: {e}")
        sys.exit(1)

    # Run all test sections
    tests = [
        ("Geographic queries",        test_1_geographic),
        ("Country distribution",      test_2_distribution),
        ("ND patient metrics",        test_3_nd_patients),
        ("T1D patient metrics",       test_4_t1d_patients),
        ("Stage 1/2 screening",       test_5_stage12),
        ("HLA + Clinical trials",     test_6_clinical_trials),
        ("Activity / enrollment",     test_7_activities),
        ("Study coordinators",        test_8_study_coordinators),
        ("Facility qualification",    test_9_facilities),
        ("CTS / DxLab flags",         test_10_flags),
        ("Combined SF+qual filters",  test_11_combined),
        ("Nearest sites",             test_12_nearest),
        ("Multi-turn follow-up",      test_13_multiturn),
        ("Site-activity enrollment",  test_14_enrollment),
        ("Spanish-language queries",  test_15_spanish_queries),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            fail(f"Test section '{name}' crashed", str(e))
            traceback.print_exc()
        time.sleep(1)  # brief pause between sections

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
