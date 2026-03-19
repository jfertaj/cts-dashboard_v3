#!/usr/bin/env python3
"""
STEP 1 — Generate Moby ground truth fixtures.

Runs every test case against the Explorer / Members / Nearby API (NOT Moby)
and records expected account_ids.  The resulting JSON is consumed by
test_moby_questions.py to evaluate Moby's answers.

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/generate_ground_truth.py
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/generate_ground_truth.py
  SF_SESSION_COOKIE="<value>" python scripts/generate_ground_truth.py --out fixtures/my_gt.json
"""

import sys, os, json, time, ssl, argparse
import urllib.request, urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("API_BASE", "https://cts-innodia-dashboard.org").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

PASS = "\033[92m✓\033[0m"; FAIL = "\033[91m✗\033[0m"
SKIP = "\033[90m-\033[0m"; INFO = "\033[94mℹ\033[0m"

# ── HTTP helpers ───────────────────────────────────────────────────────────────
def _req(method: str, path: str, body: Any = None, timeout: int = 120) -> Any:
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "Cookie": f"sf_session={SESSION_COOKIE}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {e.read().decode()[:300]}")

def explorer_search(filters: Dict, columns: List[str] = None) -> List[Dict]:
    cols = columns or ["sf.Account.Name"]
    resp = _req("POST", "/api/explorer/search", {"filters": filters, "columns": cols})
    return resp.get("rows", [])

def members_search(filters: Dict) -> List[Dict]:
    resp = _req("POST", "/api/members/search", {"filters": filters})
    return resp.get("rows", resp.get("members", []))

def get_fields() -> List[Dict]:
    return _req("GET", "/api/explorer/fields").get("fields", [])

# ── Field discovery ────────────────────────────────────────────────────────────
def find_qual(catalog: List[Dict], *kws: str) -> Optional[str]:
    kws_l = [k.lower() for k in kws]
    for f in catalog:
        if f.get("source") != "qual": continue
        lbl = f.get("label", "").lower()
        if all(k in lbl for k in kws_l):
            return f["key"]
    return None

def find_sf(catalog: List[Dict], *kws: str) -> Optional[str]:
    kws_l = [k.lower() for k in kws]
    for f in catalog:
        if f.get("source") != "sf": continue
        lbl = f.get("label", "").lower()
        if all(k in lbl for k in kws_l):
            return f["key"]
    return None

# ── Filter helpers ─────────────────────────────────────────────────────────────
def AND(*rules) -> Dict:  return {"logic": "AND", "rules": list(rules)}
def OR(*rules)  -> Dict:  return {"logic": "OR",  "rules": list(rules)}

def R(field, op, value=None):
    return {"field": field, "operator": op, "value": value}

def country_or(*iso2):
    return OR(*[R("site.country", "equals", c) for c in iso2])

def sf_gt(f, v):   return R(f"sf.{f}", "gt", v)
def sf_gte(f, v):  return R(f"sf.{f}", "gte", v)
def sf_eq(f, v):   return R(f"sf.{f}", "equals", v)
def sf_nn(f):      return R(f"sf.{f}", "is_not_null")
def sf_null(f):    return R(f"sf.{f}", "is_null")
def sf_true(f):    return R(f"sf.{f}", "equals", True)

def q_eq(k, v):    return R(k, "equals", v)
def q_nn(k):       return R(k, "is_not_null")
def q_null(k):     return R(k, "is_null")

def extra_gt(f, v):       return R(f"extra.{f}", "gt", v)
def extra_eq(f, v):       return R(f"extra.{f}", "equals", v)
def extra_has(f, v):      return R(f"extra.{f}", "contains", v)
def extra_not_has(f, v):  return R(f"extra.{f}", "not_contains", v)

def _ids(rows):    return sorted({str(r["account_id"]) for r in rows if r.get("account_id")})
def _mids(mems):   return sorted({str(m["account_id"]) for m in mems if m.get("account_id")})

# ── Case builder ───────────────────────────────────────────────────────────────
def ef(id, desc, question, filter_, cols=None, tolerance="exact", notes="", tags=None):
    """explorer_filter case"""
    return {"id": id, "description": desc, "question": question,
            "category": "explorer_filter", "filter": filter_,
            "columns": cols or ["sf.Account.Name"], "tolerance": tolerance,
            "notes": notes, "tags": tags or []}

def mb(id, desc, question, checks=None, tags=None):
    """moby_only case"""
    return {"id": id, "description": desc, "question": question,
            "category": "moby_only", "checks": checks or ["has_answer"],
            "tags": tags or []}

def mem(id, desc, question, filter_, tolerance="exact", notes="", tags=None):
    """members case"""
    return {"id": id, "description": desc, "question": question,
            "category": "members", "filter": filter_,
            "tolerance": tolerance, "notes": notes, "tags": tags or []}

def nb(id, desc, question, city, country, max_km, top_n=None, tags=None):
    """nearby case"""
    # Use ordered_top_n (precision ≥ 80%) for all nearby cases.
    # "subset" is too strict — Moby may return SF accounts not in the local Explorer DB.
    return {"id": id, "description": desc, "question": question,
            "category": "nearby", "city": city, "country": country,
            "max_km": max_km, "top_n": top_n,
            "tolerance": "ordered_top_n",
            "tags": tags or []}

# ══════════════════════════════════════════════════════════════════════════════
def build_cases(C: Dict) -> List[Dict]:
    """Build all test cases. C = discovered qual/sf field keys."""

    # shorthand
    k_pharm     = C.get("pharmacy")
    k_overnight = C.get("overnight")
    k_znt8      = C.get("znt8")
    k_hla       = C.get("hla")
    k_early     = C.get("early_diag")
    k_ogtt      = C.get("ogtt")
    k_ins_auto  = C.get("insulin_autoantibody")
    k_islet     = C.get("islet_autoantibody")
    k_biobank   = C.get("biobank")
    k_docs_ret  = C.get("documents_retained")
    k_longday   = C.get("longest_single_day")
    k_emerg     = C.get("emergency_personnel")

    cases = []

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP A — Country discovery (Explorer — Site Discovery & Filtering)
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        ef("A01", "CTS sites in Germany, Italy, Belgium",
           "Show me all CTS sites in Germany, Italy, and Belgium",
           OR(R("site.country","equals","DE"), R("site.country","equals","IT"), R("site.country","equals","BE")),
           tags=["country","explorer"]),

        ef("A02", "CTS sites in Spain or Portugal",
           "Show me all clinical sites in Spain or Portugal",
           country_or("ES","PT"), tags=["country","explorer"]),

        ef("A03", "CTS sites in Germany",
           "List all clinical sites in Germany",
           AND(R("site.country","equals","DE")), tags=["country","explorer"]),

        ef("A04", "CTS sites in Italy",
           "Show me all clinical sites in Italy",
           AND(R("site.country","equals","IT")), tags=["country","explorer"]),

        ef("A05", "CTS sites in France",
           "Which sites do we have in France?",
           AND(R("site.country","equals","FR")), tags=["country","explorer"]),

        ef("A06", "CTS sites in Poland",
           "Show me all sites in Poland",
           AND(R("site.country","equals","PL")), tags=["country","explorer"]),

        ef("A07", "Sites in Eastern Europe (PL CZ SK HU RO)",
           "What sites do we have in Eastern Europe — Poland, Czech Republic, Slovakia, Hungary, Romania?",
           country_or("PL","CZ","SK","HU","RO"), tags=["country","explorer"]),

        ef("A08", "Sites in Nordic countries (SE NO DK FI)",
           "Are there any CTS sites in the Nordic countries? Show me all of them.",
           country_or("SE","NO","DK","FI"), tags=["country","explorer"]),

        ef("A09", "Sites in UK",
           "What clinical sites do we have in the UK?",
           AND(R("site.country","equals","GB")), tags=["country","explorer"]),

        ef("A10", "Sites in Belgium or Netherlands",
           "Show me all sites in Belgium and the Netherlands",
           country_or("BE","NL"), tags=["country","explorer"]),
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP B — Patient metrics (Stage 1, Stage 2, ND)
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        # NOTE B01-B06: sf.* numeric fields (Stage1/2, ND counts) are Opportunity-level
        # fields that return all 195 sites when used as Explorer filters (filter silently
        # dropped). Use moby_only so Moby's deterministic handlers are tested instead.
        mb("B01", "Sites with Stage 1 > 50",
           "Which sites have more than 50 Stage 1 individuals currently followed?",
           checks=["has_table","min_rows_1"], tags=["stage1","metrics"]),

        mb("B02", "All sites with any Stage 1",
           "Show me all sites that have Stage 1 individuals followed",
           checks=["has_table","min_rows_10"], tags=["stage1","metrics"]),

        mb("B03", "Sites with any Stage 2",
           "Which sites have Stage 2 individuals currently followed?",
           checks=["has_table","min_rows_5"], tags=["stage2","metrics"]),

        mb("B04", "Sites with Stage 1 AND Stage 2",
           "Show me sites that have both Stage 1 AND Stage 2 patients",
           checks=["has_table","min_rows_3"], tags=["stage1","stage2","metrics"]),

        mb("B05", "ND under-18 > 20 per year",
           "Which sites have more than 20 newly diagnosed T1D patients under 18 per year?",
           checks=["has_table","min_rows_1"], tags=["nd","metrics"]),

        mb("B06", "ND over-18 > 50 per year",
           "Which sites see more than 50 newly diagnosed adult T1D patients per year?",
           checks=["has_table","min_rows_1"], tags=["nd","metrics"]),

        # NOTE B07/B09/B10/B15/B16: INNODIA_Clinical_Trial_Site__c, C_Profiling_Complete__c,
        # C_Referral_Clinical_Partner__c, C_Date_First_Contact__c etc. are Account-level
        # fields — Explorer returns HTTP 400 "Campo no permitido" for these sf.* filters.
        mb("B07", "CTS-validated sites",
           "Show me all CTS-validated sites",
           checks=["has_table","min_rows_1"], tags=["status"]),

        ef("B08", "CT-3 Phase I sites",
           "Which sites are participating in CT-3 Phase I trials?",
           AND(sf_true("C_Phase_I_Type1__c")),
           cols=["sf.Account.Name","sf.C_Phase_I_Type1__c"],
           tags=["ct3","status"]),

        mb("B09", "Sites where profiling is complete",
           "Show me all sites where profiling has been completed",
           checks=["has_answer"], tags=["profiling","status"]),

        mb("B10", "Sites with Referral/Clinical Partner role",
           "Which sites have a Referral Clinical Partner role?",
           checks=["has_answer"], tags=["status"]),

        # NOTE B11-B13: country filter works but sf_gt numeric is silently dropped →
        # returns ALL country sites regardless of Stage1/2 count (wrong baseline).
        mb("B11", "German sites with Stage 1 > 0",
           "Show me German sites that have Stage 1 patients followed",
           checks=["has_table","min_rows_1"], tags=["country","stage1"]),

        mb("B12", "Italian sites with Stage 1 > 30",
           "Show me Italian sites with more than 30 Stage 1 individuals followed",
           checks=["has_table","min_rows_1"], tags=["country","stage1"]),

        mb("B13", "French sites with Stage 2 > 0",
           "Which French sites have Stage 2 patients?",
           checks=["has_table","min_rows_1"], tags=["country","stage2"]),

        # NOTE B14/B15/B16/B17: profiling pipeline fields are Account-level → HTTP 400.
        mb("B14", "Sites with profiling form uploaded to DB",
           "Which sites have had the profiling form uploaded to the database?",
           checks=["has_answer"], tags=["profiling","status"]),

        mb("B15", "Sites with first contact but no meeting date",
           "Which sites have had a first contact date but no meeting date yet?",
           checks=["has_answer"], tags=["profiling","pipeline"]),

        mb("B16", "CTS-validated but no assignment",
           "Which sites are CTS-validated but not yet active in any assignment?",
           checks=["has_answer"], tags=["status","assignment"]),

        mb("B17", "Profiling form sent but not received",
           "Find sites where the profiling questionnaire was sent but not yet received",
           checks=["has_answer"], tags=["profiling","pipeline"]),
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP C — Qualification data
    # ─────────────────────────────────────────────────────────────────────────
    if k_pharm:
        cases.append(ef("C01", "Sites with pharmacy on-site",
            "Find all sites with pharmacy available on-site",
            AND(q_eq(k_pharm, "On-site")), cols=["sf.Account.Name", k_pharm],
            notes=f"qual: {k_pharm}", tags=["qual","facilities"]))

    if k_overnight:
        cases.append(ef("C02", "Sites with overnight stay capacity",
            "Which sites have overnight stay capacity?",
            AND(q_nn(k_overnight)), cols=["sf.Account.Name", k_overnight],
            notes=f"qual: {k_overnight}", tags=["qual","facilities"]))

    if k_pharm and k_overnight:
        cases.append(ef("C03", "Sites with pharmacy AND overnight",
            "Show me sites that have pharmacy on-site AND overnight stay capacity",
            AND(q_eq(k_pharm,"On-site"), q_nn(k_overnight)),
            cols=["sf.Account.Name", k_pharm, k_overnight],
            tags=["qual","facilities"]))

        # NOTE C04: sf_gt numeric silently dropped → returns all overnight sites; moby_only
        cases.append(mb("C04", "Sites Stage 2 AND overnight",
            "Which sites have overnight stay capacity AND Stage 2 patients?",
            checks=["has_table","min_rows_1"], tags=["qual","stage2"]))

        cases.append(ef("C05", "Sites in ES/PT with pharmacy AND overnight",
            "Find sites in Spain or Portugal that have pharmacy on-site and overnight stay",
            {"logic":"1 AND 2 AND (3 OR 4)","rules":[
                q_eq(k_pharm,"On-site"), q_nn(k_overnight),
                R("site.country","equals","ES"), R("site.country","equals","PT")]},
            cols=["sf.Account.Name", k_pharm, k_overnight],
            tags=["qual","country","facilities"]))

        cases.append(ef("C06", "Sites in DE/AT with pharmacy AND overnight",
            "Show me sites in Germany or Austria that have pharmacy on-site and overnight stay",
            {"logic":"1 AND 2 AND (3 OR 4)","rules":[
                q_eq(k_pharm,"On-site"), q_nn(k_overnight),
                R("site.country","equals","DE"), R("site.country","equals","AT")]},
            cols=["sf.Account.Name", k_pharm, k_overnight],
            tags=["qual","country","facilities"]))

    if k_znt8:
        cases.append(ef("C07", "Sites with ZnT8 testing",
            "Which sites have ZnT8 autoantibody testing available?",
            AND(q_nn(k_znt8)), cols=["sf.Account.Name", k_znt8],
            notes=f"qual: {k_znt8}", tags=["qual","autoantibody"]))

    if k_hla:
        cases.append(ef("C08", "Sites with HLA typing",
            "Which sites have HLA typing capability available?",
            AND(q_nn(k_hla)), cols=["sf.Account.Name", k_hla],
            notes=f"qual: {k_hla}", tags=["qual","lab"]))

    if k_islet:
        cases.append(ef("C09", "Sites with islet autoantibody testing",
            "Which sites can perform islet autoantibody testing?",
            AND(q_nn(k_islet)), cols=["sf.Account.Name", k_islet],
            notes=f"qual: {k_islet}", tags=["qual","autoantibody"]))

    if k_hla and k_islet:
        cases.append(ef("C10", "Sites with HLA typing AND islet autoantibody testing",
            "Find sites that have both HLA typing and islet autoantibody testing",
            AND(q_nn(k_hla), q_nn(k_islet)),
            cols=["sf.Account.Name", k_hla, k_islet],
            tags=["qual","lab","autoantibody"]))

    if k_early:
        cases.append(ef("C11", "Sites running early diagnosis programs",
            "Which sites can perform early diagnosis screening?",
            AND(q_nn(k_early)), cols=["sf.Account.Name", k_early],
            notes=f"qual: {k_early}", tags=["qual","early_diag"]))

    if k_ogtt:
        cases.append(ef("C12", "Sites with OGTT capability",
            "Which sites have OGTT (oral glucose tolerance test) capacity?",
            AND(q_nn(k_ogtt)), cols=["sf.Account.Name", k_ogtt],
            notes=f"qual: {k_ogtt}", tags=["qual","lab"]))

    if k_ins_auto:
        cases.append(ef("C13", "Sites with insulin autoantibody testing",
            "Which sites have insulin autoantibody testing?",
            AND(q_nn(k_ins_auto)), cols=["sf.Account.Name", k_ins_auto],
            notes=f"qual: {k_ins_auto}", tags=["qual","autoantibody"]))

    if k_biobank:
        cases.append(ef("C14", "Sites with biobank or sample storage",
            "Which sites have a biobank or sample storage facility?",
            AND(q_nn(k_biobank)), cols=["sf.Account.Name", k_biobank],
            notes=f"qual: {k_biobank}", tags=["qual","infrastructure"]))

    if k_longday:
        cases.append(ef("C15", "Sites accommodating long single-day visits",
            "Which sites can accommodate a single-day visit longer than 8 hours?",
            AND(q_nn(k_longday)), cols=["sf.Account.Name", k_longday],
            notes=f"qual: {k_longday}", tags=["qual","logistics"]))

    if k_docs_ret:
        cases.append(ef("C16", "Sites retaining documents long-term",
            "Which sites retain documents for more than 15 years?",
            AND(q_nn(k_docs_ret)), cols=["sf.Account.Name", k_docs_ret],
            notes=f"qual: {k_docs_ret}", tags=["qual","compliance"]))

    if k_emerg:
        cases.append(ef("C17", "Sites with emergency personnel within 30 min",
            "Which sites confirmed emergency personnel can arrive within 30 minutes?",
            AND(q_nn(k_emerg)), cols=["sf.Account.Name", k_emerg],
            notes=f"qual: {k_emerg}", tags=["qual","safety"]))

    # Complex qual combos
    if k_znt8 and k_islet:
        cases.append(ef("C18", "Sites ZnT8 AND islet autoantibody",
            "Find sites that have both ZnT8 testing and islet autoantibody testing",
            AND(q_nn(k_znt8), q_nn(k_islet)),
            cols=["sf.Account.Name", k_znt8, k_islet],
            tags=["qual","autoantibody"]))

    if k_hla and k_overnight and k_pharm:
        cases.append(ef("C19", "Sites HLA AND overnight AND pharmacy",
            "Which sites have HLA typing AND overnight stay AND pharmacy on-site?",
            AND(q_nn(k_hla), q_nn(k_overnight), q_eq(k_pharm,"On-site")),
            cols=["sf.Account.Name", k_hla, k_overnight, k_pharm],
            tags=["qual","facilities","lab"]))

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP D — Activity / Assignment filters
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        ef("D01", "Sites in any assignment",
           "Which sites are active in any current assignment?",
           AND(extra_gt("AssignmentsCount", 0)),
           cols=["sf.Account.Name","extra.AssignmentsNames","extra.AssignmentsCount"],
           tags=["assignment"]),

        ef("D02", "Sites with NO assignment",
           "List all sites that have no current assignment",
           AND(extra_eq("AssignmentsCount", 0)),
           cols=["sf.Account.Name","extra.AssignmentsCount"],
           tags=["assignment"]),

        ef("D03", "Sites in Beta Preserve assignment",
           "Show me all sites involved in the Beta Preserve assignment",
           AND(extra_has("AssignmentsNames","Beta Preserve")),
           cols=["sf.Account.Name","extra.AssignmentsNames"],
           tags=["assignment","activity"]),

        ef("D04", "Sites in Safeguard assignment",
           "Which sites are participating in Safeguard?",
           AND(extra_has("AssignmentsNames","Safeguard")),
           cols=["sf.Account.Name","extra.AssignmentsNames"],
           tags=["assignment","activity"]),

        ef("D05", "Sites NOT in Beta Preserve",
           "Show me CTS sites that are NOT involved in Beta Preserve",
           # NOTE: INNODIA_Clinical_Trial_Site__c is an Account field → HTTP 400.
           # Use extra filter only; baseline = all CTS sites not in Beta Preserve.
           AND(extra_not_has("AssignmentsNames","Beta Preserve")),
           cols=["sf.Account.Name","extra.AssignmentsNames"],
           tags=["assignment","activity"]),
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP E — Complex multi-filter
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        # NOTE E01/E02/E03: sf_gt numeric fields silently dropped, or Account fields
        # → HTTP 400. Converted to moby_only so deterministic planner handlers are tested.
        mb("E01", "Sites in IT or ES with Stage 1 AND Stage 2",
           "Which Italian or Spanish sites have both Stage 1 and Stage 2 patients?",
           checks=["has_table","min_rows_1"], tags=["country","stage1","stage2","complex"]),

        mb("E02", "CTS-validated AND profiling complete",
           "Show me sites that are both CTS-validated and have completed profiling",
           checks=["has_answer"], tags=["status","complex"]),

        mb("E03", "Stage 1 > 30 in DE, AT, CH",
           "Find sites in Germany, Austria, or Switzerland with more than 30 Stage 1 patients followed",
           checks=["has_table","min_rows_1"], tags=["country","stage1","complex"]),
    ]

    # Add complex cases with qual fields if available
    if k_pharm and k_overnight:
        # NOTE E04: sf_gt numeric silently dropped → wrong baseline; moby_only
        cases.append(mb("E04", "Stage 1 > 20 + pharmacy + overnight (feasibility)",
            "We need 20 sites for a study requiring overnight stay and pharmacy — which qualify?",
            checks=["has_table","min_rows_1"],
            tags=["qual","stage1","feasibility","complex"]))

    if k_hla and k_overnight and k_pharm:
        cases.append(ef("E05", "HLA AND overnight AND pharmacy in FR or BE",
            "Which French or Belgian sites have HLA typing AND overnight stay AND pharmacy on-site?",
            {"logic":"(1 OR 2) AND 3 AND 4 AND 5","rules":[
                R("site.country","equals","FR"), R("site.country","equals","BE"),
                q_nn(k_hla), q_nn(k_overnight), q_eq(k_pharm,"On-site")]},
            cols=["sf.Account.Name", k_hla, k_overnight, k_pharm],
            tags=["qual","country","complex"]))

    if k_znt8:
        # NOTE E06: C_Profiling_Complete__c is Account field → HTTP 400; moby_only
        cases.append(mb("E06", "ZnT8 AND profiling complete",
            "Show me all sites where profiling is complete AND they have ZnT8 testing",
            checks=["has_table","min_rows_1"],
            tags=["profiling","qual","complex"]))

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP F — Geographic / Nearby (nearby category)
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        nb("F01","5 closest sites to Munich",
           "What are the 5 closest CTS sites to Munich?","Munich","DE",500,top_n=5,
           tags=["geographic","nearby"]),
        nb("F02","Sites within 200 km of Paris",
           "Show me all sites within 200 km of Paris","Paris","FR",200,
           tags=["geographic","nearby"]),
        nb("F03","Sites within 300 km of Warsaw",
           "Which CTS sites are within 300 km of Warsaw?","Warsaw","PL",300,
           tags=["geographic","nearby"]),
        nb("F04","Sites within 150 km of Brussels",
           "Find CTS sites within 150 km of Brussels","Brussels","BE",150,
           tags=["geographic","nearby"]),
        nb("F05","Sites within 200 km of Barcelona",
           "What sites are within 200 km of Barcelona?","Barcelona","ES",200,
           tags=["geographic","nearby"]),
        nb("F06","Nearest 3 sites to Leuven",
           "Which sites are nearest to Leuven, Belgium?","Leuven","BE",300,top_n=3,
           tags=["geographic","nearby"]),
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP G — Members view
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        mem("G01","Members in UK",
            "Show me all member institutions in the UK",
            {"logic":"AND","rules":[R("site.country","equals","GB")]},
            tags=["members"]),
        mem("G02","Members in Germany",
            "Which member institutions are in Germany?",
            {"logic":"AND","rules":[R("site.country","equals","DE")]},
            tags=["members"]),
        mem("G03","Members in France",
            "Show me all member institutions in France",
            {"logic":"AND","rules":[R("site.country","equals","FR")]},
            tags=["members"]),
        mem("G04","Members in Italy",
            "Which member institutions are in Italy?",
            {"logic":"AND","rules":[R("site.country","equals","IT")]},
            tags=["members"]),
        mem("G05","Members in Belgium",
            "Show me member institutions in Belgium",
            {"logic":"AND","rules":[R("site.country","equals","BE")]},
            tags=["members"]),
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # GROUP H — Moby-only (natural language, structural checks only)
    # ─────────────────────────────────────────────────────────────────────────
    cases += [
        # Country distribution
        mb("H01","How many sites per country",
           "How many CTS sites do we have per country?",
           checks=["has_answer","mentions_country"], tags=["distribution","moby"]),
        mb("H02","Which country has most Stage 1 patients",
           "Which country has the most Stage 1 patients followed?",
           checks=["has_answer"], tags=["distribution","stage1","moby"]),
        mb("H03","Which country has most ND under-18",
           "Which country has the most newly diagnosed T1D patients under 18?",
           checks=["has_answer"], tags=["distribution","nd","moby"]),
        mb("H04","Stage 2 breakdown by country",
           "Show me a breakdown of Stage 2 patients by country",
           checks=["has_answer"], tags=["distribution","stage2","moby"]),

        # Rankings
        mb("H05","Top 10 sites by Stage 1 patients",
           "What are the top 10 sites with the most Stage 1 individuals followed?",
           checks=["has_table","min_rows_5"], tags=["ranking","stage1","moby"]),
        mb("H06","Top sites by T1D patients per year",
           "Which sites have the highest number of T1D patients seen per year?",
           checks=["has_table","min_rows_3"], tags=["ranking","moby"]),
        mb("H07","Top ND sites",
           "Show me the sites with the most newly diagnosed T1D patients",
           checks=["has_table","min_rows_3"], tags=["ranking","nd","moby"]),

        # Study coordinators and PI
        mb("H08","Study coordinators at Italian sites",
           "Who are the study coordinators at the Italian sites?",
           checks=["has_table","min_rows_1"], tags=["contacts","sc","moby"]),
        mb("H09","Principal investigators at German sites",
           "Show me the principal investigators for all German sites",
           checks=["has_table","min_rows_1"], tags=["contacts","pi","moby"]),
        mb("H10","Study coordinators at Belgian sites",
           "Who are the study coordinators in Belgium?",
           checks=["has_table","min_rows_1"], tags=["contacts","sc","moby"]),
        mb("H11","Sites without study coordinator listed",
           "Which sites don't have a dedicated study coordinator listed?",
           checks=["has_answer"], tags=["contacts","sc","moby"]),

        # Activities
        mb("H12","All activities and site counts",
           "Show me all activities and how many sites participate in each",
           checks=["has_table","min_rows_1"], tags=["activity","moby"]),
        mb("H13","Assignments breakdown by country",
           "Show me a breakdown of assignments by country",
           checks=["has_answer"], tags=["activity","distribution","moby"]),
        mb("H14","Activities sponsored by Sanofi",
           "How many sites are involved in activities sponsored by Sanofi?",
           checks=["has_answer"], tags=["activity","moby"]),

        # Geographic natural language
        mb("H15","Nearest 5 sites to Munich (natural language)",
           "What are the 5 closest CTS sites to Munich?",
           checks=["has_table","min_rows_3"], tags=["geographic","moby"]),
        mb("H16","Sites near Frankfurt for a monitoring tour",
           "I need to plan a monitoring visit tour through central Germany — which sites are near Frankfurt?",
           checks=["has_table","min_rows_1"], tags=["geographic","moby"]),
        mb("H17","Nearest site with overnight to Warsaw",
           "Find the closest site with overnight stay capacity to Warsaw",
           checks=["has_table","min_rows_1"], tags=["geographic","nearby","moby"]),
        mb("H18","Sites near Leuven Belgium",
           "Which sites are nearest to Leuven, Belgium?",
           checks=["has_table","min_rows_1"], tags=["geographic","moby"]),

        # Reporting and export
        mb("H19","Table of all sites with Stage 1 and Stage 2 by country",
           "Give me a table of all CTS sites with their Stage 1 and Stage 2 patient counts, by country",
           checks=["has_table","min_rows_5"], tags=["reporting","moby"]),
        mb("H20","Sites per country with validated role",
           "Give me a count of sites per country that have at least one validated role",
           checks=["has_answer"], tags=["reporting","moby"]),
        mb("H21","Profiling sites with completion status by country",
           "Show me all profiling sites in Poland sorted by meeting date",
           checks=["has_answer"], tags=["reporting","profiling","moby"]),

        # Feasibility
        mb("H22","Feasibility for Stage 1 prevention trial (30+ Stage 1, 20 sites needed)",
           "For a Stage 1 prevention trial we need sites with more than 30 Stage 1 patients — how many sites do we have and in which countries?",
           checks=["has_answer"], tags=["feasibility","stage1","moby"]),
        mb("H23","Expansion into Eastern Europe feasibility",
           "We want to expand into Eastern Europe — which countries already have profiling sites we could fast-track to CTS?",
           checks=["has_answer"], tags=["feasibility","profiling","moby"]),
        mb("H24","Sites that could support early diagnosis work",
           "How many sites could potentially support early diagnosis work based on their profiling data?",
           checks=["has_answer"], tags=["feasibility","early_diag","moby"]),
        mb("H25","Sites for HLA-based sub-study",
           "If we need HLA typing at the site level, how many of our current sites can provide it?",
           checks=["has_answer"], tags=["feasibility","hla","moby"]),

        # Re-engagement
        mb("H26","Sites not in any assignment in 2 years",
           "Which sites haven't been involved in any assignment? They might need re-engagement.",
           checks=["has_table","min_rows_1"], tags=["assignment","moby"]),

        # Members natural language
        mb("H27","Which institution does a site belong to",
           "Which member institution does the site in Brest belong to?",
           checks=["has_answer"], tags=["members","moby"]),
        mb("H28","Institutions with multiple clinical subaccounts",
           "Which member institutions have multiple clinical subaccounts that could serve as coordinating centers?",
           checks=["has_answer"], tags=["members","moby"]),

        # Charts
        mb("H29","Chart Stage 2 by country",
           "Show me a chart of Stage 2 patients per country",
           checks=["has_answer"], tags=["chart","moby"]),
        mb("H30","Sites active in assignments per country",
           "How many sites per country are currently active in assignments?",
           checks=["has_answer"], tags=["assignment","distribution","moby"]),
    ]

    return cases

# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="fixtures/moby_ground_truth.json")
    parser.add_argument("--skip-groups", nargs="*", default=[],
                        help="Skip groups by letter, e.g. --skip-groups F G")
    args = parser.parse_args()

    if not SESSION_COOKIE:
        print(f"{FAIL}  SF_SESSION_COOKIE is not set. Export it first.")
        sys.exit(1)

    out_path = Path(__file__).parent.parent / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*65}")
    print(f"  CTS Dashboard — Moby Ground Truth Generator")
    print(f"  API: {BASE_URL}")
    print(f"  Output: {out_path}")
    print(f"{'═'*65}\n")

    # ── 0. Discover qual field keys ────────────────────────────────────────────
    print(f"{INFO}  Fetching field catalog …")
    catalog = get_fields()
    qual_count = sum(1 for f in catalog if f.get("source") == "qual")
    print(f"{PASS}  Catalog: {len(catalog)} fields ({qual_count} qual)\n")

    FIELD_KEYS: Dict[str, Optional[str]] = {
        # Prefer categorical "pharmacy available/on-site" over text "description of pharmacy role"
        "pharmacy":            (find_qual(catalog, "pharmacy", "on site") or
                                find_qual(catalog, "pharmacy", "campus") or
                                find_qual(catalog, "pharmacy", "available") or
                                find_qual(catalog, "pharmacy", "on-site") or
                                find_qual(catalog, "pharmacy", "service")),
        "overnight":           find_qual(catalog, "overnight"),
        "znt8":                find_qual(catalog, "znt8"),
        "hla":                 find_qual(catalog, "hla"),
        "islet_autoantibody":  find_qual(catalog, "islet", "autoantibody") or find_qual(catalog, "autoantibody"),
        "early_diag":          find_qual(catalog, "early", "diagnos"),
        "ogtt":                find_qual(catalog, "ogtt") or find_qual(catalog, "glucose", "tolerance"),
        "insulin_autoantibody":find_qual(catalog, "insulin", "autoantibody") or find_qual(catalog, "insulin", "test"),
        "biobank":             find_qual(catalog, "biobank") or find_qual(catalog, "sample", "storage"),
        "documents_retained":  find_qual(catalog, "document", "retain") or find_qual(catalog, "document"),
        "longest_single_day":  find_qual(catalog, "single", "day") or find_qual(catalog, "day", "visit"),
        "emergency_personnel": find_qual(catalog, "emergency"),
    }

    print("  Qual field discovery:")
    for lbl, key in FIELD_KEYS.items():
        icon = PASS if key else f"\033[93m?\033[0m"
        print(f"  {icon}  {lbl:<25} → {key or 'not found (cases skipped)'}")

    # ── 1. Build all cases ────────────────────────────────────────────────────
    cases = build_cases(FIELD_KEYS)
    skip_g = {g.upper() for g in args.skip_groups}
    if skip_g:
        cases = [c for c in cases if c["id"][0].upper() not in skip_g]
        print(f"\n  Skipping groups: {skip_g}")

    print(f"\n  {len(cases)} test cases across groups A–H\n")
    print(f"{'─'*65}")

    # ── 2. Run baselines ──────────────────────────────────────────────────────
    results = {}
    errors_list = []
    n_ok = n_skip = n_err = 0

    for case in cases:
        cid  = case["id"]
        cat  = case["category"]
        desc = case["description"]
        gid  = cid[0]

        if cat == "moby_only":
            results[cid] = {
                "description": desc, "question": case["question"],
                "category": cat, "checks": case.get("checks", []),
                "tags": case.get("tags", []),
            }
            n_skip += 1
            print(f"  {SKIP}  [{cid}] {desc}  ← moby_only")
            continue

        try:
            if cat == "explorer_filter":
                rows = explorer_search(case["filter"], case.get("columns"))
                ids  = _ids(rows)
                results[cid] = {
                    "description": desc, "question": case["question"],
                    "category": cat, "filter_used": case["filter"],
                    "columns": case.get("columns", []),
                    "expected_count": len(ids),
                    "expected_account_ids": ids,
                    "tolerance": case.get("tolerance", "exact"),
                    "notes": case.get("notes", ""),
                    "tags": case.get("tags", []),
                }
                print(f"  {PASS}  [{cid}] {desc}  → {len(ids)} sites")
                n_ok += 1

            elif cat == "members":
                mems = members_search(case["filter"])
                ids  = _mids(mems)
                results[cid] = {
                    "description": desc, "question": case["question"],
                    "category": cat, "filter_used": case["filter"],
                    "expected_count": len(ids),
                    "expected_account_ids": ids,
                    "tolerance": case.get("tolerance", "exact"),
                    "notes": case.get("notes", ""),
                    "tags": case.get("tags", []),
                }
                print(f"  {PASS}  [{cid}] {desc}  → {len(ids)} members")
                n_ok += 1

            elif cat == "nearby":
                # Record all known account_ids — Moby must return a subset of these
                all_rows = explorer_search({"logic":"AND","rules":[]})
                all_ids  = _ids(all_rows)
                results[cid] = {
                    "description": desc, "question": case["question"],
                    "category": cat,
                    "city": case["city"], "country": case["country"],
                    "max_km": case["max_km"], "top_n": case.get("top_n"),
                    "all_known_account_ids": all_ids,
                    "tolerance": case.get("tolerance", "subset"),
                    "tags": case.get("tags", []),
                    "notes": "Moby results must be a subset of known CTS sites",
                }
                print(f"  {PASS}  [{cid}] {desc}  → {len(all_ids)} known sites baseline")
                n_ok += 1

            time.sleep(0.25)

        except Exception as e:
            print(f"  {FAIL}  [{cid}] {desc}  → ERROR: {e}")
            errors_list.append({"id": cid, "error": str(e)})
            results[cid] = {
                "description": desc, "question": case.get("question",""),
                "category": cat, "error": str(e),
                "expected_count": None, "expected_account_ids": [],
                "tolerance": case.get("tolerance","exact"),
                "tags": case.get("tags", []),
            }
            n_err += 1

    # ── 3. Save fixture ────────────────────────────────────────────────────────
    fixture = {
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "api_base":            BASE_URL,
        "total_cases":         len(cases),
        "qual_fields_found":   FIELD_KEYS,
        "errors":              errors_list,
        "cases":               results,
    }
    out_path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")

    # ── 4. Summary ─────────────────────────────────────────────────────────────
    print(f"\n{'═'*65}")
    print(f"  Saved → {out_path}")
    print(f"  {n_ok} baselines OK  |  {n_skip} moby-only  |  {n_err} errors")
    if errors_list:
        print(f"\n  Errors:")
        for e in errors_list:
            print(f"    {FAIL}  {e['id']}: {e['error'][:80]}")
    print(f"\n  Next step:")
    print(f'    SF_SESSION_COOKIE="$SF_SESSION_COOKIE" python scripts/test_moby_questions.py')
    print(f"{'═'*65}\n")


if __name__ == "__main__":
    main()
