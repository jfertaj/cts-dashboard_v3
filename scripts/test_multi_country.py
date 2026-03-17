#!/usr/bin/env python3
"""
Multi-country filter test battery — Explorer API + Moby AI.

Covers:
  Section A — Unit tests (no network): pass_site logic with AND/OR/Groups
  Section B — Explorer API integration: multi-country AND, OR, Groups, IN operator
  Section C — Moby AI integration: natural-language multi-country queries + AND/OR intent

Usage:
  # A + B only (no Moby, requires SF session):
  SF_SESSION_COOKIE="<value>" python scripts/test_multi_country.py

  # A + B + C (full, including Moby):
  SF_SESSION_COOKIE="<value>" python scripts/test_multi_country.py --moby

  # Against local backend:
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/test_multi_country.py --moby
"""

import sys, os, json, ssl, time
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional, Set

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
RUN_MOBY       = "--moby" in sys.argv

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
INFO = "\033[94mℹ\033[0m"
SKIP = "\033[90m–\033[0m"
errors: List[str] = []

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

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
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:300]}")

def search(filters: Dict, columns: List[str] | None = None) -> List[Dict]:
    resp = _req("POST", "/api/explorer/search", {
        "filters": filters,
        "columns": columns or ["site.country", "site.city"],
    })
    return resp.get("rows", [])

def chat(message: str, last_filters: Any = None) -> Dict:
    payload: Dict[str, Any] = {
        "messages": [{"role": "user", "content": message}],
    }
    if last_filters:
        payload["last_filters"] = last_filters
    return _req("POST", "/api/ai/chat", payload)

def countries_in_rows(rows: List[Dict]) -> Set[str]:
    """Return set of unique uppercase ISO-2 country codes from result rows."""
    return {(r.get("country") or "").strip().upper() for r in rows if r.get("country")}

def check(label: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    print(f"  {icon}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        errors.append(label)

def skip(label: str, reason: str = "") -> None:
    print(f"  {SKIP}  {label}" + (f"  (skipped: {reason})" if reason else ""))

def header(title: str) -> None:
    print(f"\n\033[1m{'─'*60}\n  {title}\n{'─'*60}\033[0m")

# ─── Section A: Unit tests ────────────────────────────────────────────────────
# Reproduce the fixed pass_site logic locally (no network).

_ISO2_MAP = {
    "spain": "ES", "españa": "ES", "es": "ES",
    "portugal": "PT", "pt": "PT",
    "ireland": "IE", "ie": "IE",
    "germany": "DE", "deutschland": "DE", "de": "DE",
    "france": "FR", "fr": "FR",
    "italy": "IT", "italia": "IT", "it": "IT",
    "czech republic": "CZ", "czechia": "CZ", "cz": "CZ",
    "united kingdom": "GB", "uk": "GB", "gb": "GB",
    "netherlands": "NL", "nl": "NL",
    "belgium": "BE", "be": "BE",
}

def _country_norm(v: Optional[str]) -> Optional[str]:
    if not v:
        return None
    s = v.strip().lower()
    return _ISO2_MAP.get(s, s.upper() if len(s) == 2 else s.title())

def _op_norm(op: str) -> str:
    return {"eq": "equals", "=": "equals", "neq": "not_equals", "!=": "not_equals"}.get(op, op)

class Rule:
    def __init__(self, field: str, operator: str, value: Any):
        self.field = field; self.operator = operator; self.value = value

def _match_one(acc: Dict, rule: Rule) -> bool:
    val = acc.get("city", "") if rule.field == "site.city" else acc.get("country", "")
    op = _op_norm(rule.operator)
    s = str(rule.value or "")
    if rule.field == "site.country" and op in ("equals", "not_equals", "=", "!="):
        val = (_country_norm(val) or val).upper()
        s   = (_country_norm(s)   or s).upper()
    if op in ("equals", "="):       return val.lower() == s.lower()
    if op in ("not_equals", "!="):  return val.lower() != s.lower()
    if op == "contains":            return s.lower() in val.lower()
    if op == "in":
        items = {(_country_norm(x.strip()) or x.strip()).upper()
                 for x in str(rule.value or "").split(",") if x.strip()}
        return val.upper() in items
    if op == "not_in":
        items = {(_country_norm(x.strip()) or x.strip()).upper()
                 for x in str(rule.value or "").split(",") if x.strip()}
        return val.upper() not in items
    return True

def pass_site_fixed(acc: Dict, rules: List[Rule], logic: str = "AND") -> bool:
    """Reproduces the FIXED pass_site (multi-country AND → OR)."""
    if not rules:
        return True
    res = [_match_one(acc, r) for r in rules]
    glue_and = logic == "AND"
    if glue_and:
        country_idx = [i for i, r in enumerate(rules) if r.field == "site.country"]
        if len(country_idx) > 1:
            country_pass = any(res[i] for i in country_idx)
            other_pass   = all(res[i] for i in range(len(res)) if i not in set(country_idx))
            return country_pass and other_pass
    return all(res) if glue_and else any(res)

def section_a():
    header("A  Unit tests — pass_site logic (no network)")

    es = {"country": "ES", "city": "Madrid"}
    pt = {"country": "PT", "city": "Lisbon"}
    de = {"country": "DE", "city": "Berlin"}
    ie = {"country": "IE", "city": "Dublin"}

    # A-1: Single country equals — basic regression
    r = [Rule("site.country", "equals", "Spain")]
    check("A-1  Single country equals Spain → ES matches",   pass_site_fixed(es, r) is True)
    check("A-1  Single country equals Spain → DE no match",  pass_site_fixed(de, r) is False)

    # A-2: Multiple countries with AND logic → treated as implicit OR
    r = [Rule("site.country", "equals", "Spain"),
         Rule("site.country", "equals", "Portugal")]
    check("A-2  AND[Spain,Portugal] → ES matches (implicit OR)", pass_site_fixed(es, r, "AND") is True)
    check("A-2  AND[Spain,Portugal] → PT matches (implicit OR)", pass_site_fixed(pt, r, "AND") is True)
    check("A-2  AND[Spain,Portugal] → DE no match",             pass_site_fixed(de, r, "AND") is False)

    # A-3: Explicit OR logic
    r = [Rule("site.country", "equals", "Spain"),
         Rule("site.country", "equals", "Germany")]
    check("A-3  OR[Spain,Germany] → ES matches",  pass_site_fixed(es, r, "OR") is True)
    check("A-3  OR[Spain,Germany] → DE matches",  pass_site_fixed(de, r, "OR") is True)
    check("A-3  OR[Spain,Germany] → IE no match", pass_site_fixed(ie, r, "OR") is False)

    # A-4: IN operator with comma-separated ISO codes
    r = [Rule("site.country", "in", "ES,PT,IE")]
    check("A-4  IN[ES,PT,IE] → ES matches", pass_site_fixed(es, r) is True)
    check("A-4  IN[ES,PT,IE] → PT matches", pass_site_fixed(pt, r) is True)
    check("A-4  IN[ES,PT,IE] → IE matches", pass_site_fixed(ie, r) is True)
    check("A-4  IN[ES,PT,IE] → DE no match", pass_site_fixed(de, r) is False)

    # A-5: NOT_IN operator
    r = [Rule("site.country", "not_in", "ES,PT")]
    check("A-5  NOT_IN[ES,PT] → DE matches",  pass_site_fixed(de, r) is True)
    check("A-5  NOT_IN[ES,PT] → ES no match", pass_site_fixed(es, r) is False)

    # A-6: Mixed country + city AND rule (country OR logic, city AND logic)
    r = [Rule("site.country", "equals", "Spain"),
         Rule("site.country", "equals", "Portugal"),
         Rule("site.city",    "equals", "Lisbon")]
    lisbon_pt = {"country": "PT", "city": "Lisbon"}
    madrid_es = {"country": "ES", "city": "Madrid"}
    check("A-6  AND[Spain,Portugal,city=Lisbon] → PT+Lisbon matches", pass_site_fixed(lisbon_pt, r, "AND") is True)
    check("A-6  AND[Spain,Portugal,city=Lisbon] → ES+Madrid no match (city≠Lisbon)", pass_site_fixed(madrid_es, r, "AND") is False)

    # A-7: Three countries with AND → all three should match
    r = [Rule("site.country", "equals", "Spain"),
         Rule("site.country", "equals", "Ireland"),
         Rule("site.country", "equals", "Germany")]
    check("A-7  AND[Spain,Ireland,Germany] → ES matches", pass_site_fixed(es, r, "AND") is True)
    check("A-7  AND[Spain,Ireland,Germany] → IE matches", pass_site_fixed(ie, r, "AND") is True)
    check("A-7  AND[Spain,Ireland,Germany] → DE matches", pass_site_fixed(de, r, "AND") is True)
    check("A-7  AND[Spain,Ireland,Germany] → PT no match", pass_site_fixed(pt, r, "AND") is False)

# ─── Section B: Explorer API integration ──────────────────────────────────────

def section_b():
    header("B  Explorer API — multi-country filters (needs SF session)")

    if not SESSION_COOKIE:
        skip("B-* all", "SF_SESSION_COOKIE not set")
        return

    COLS = ["site.country", "site.city"]

    # B-1: Single country baseline
    rows_es = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"}
    ]}, COLS)
    ctrs_es = countries_in_rows(rows_es)
    check("B-1  Single country Spain → only ES returned",
          ctrs_es == {"ES"}, f"{len(rows_es)} rows, countries={ctrs_es}")

    # B-2: Two countries — AND logic (fixed: implicit OR)
    rows_and2 = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"},
        {"field": "site.country", "operator": "equals", "value": "Portugal"},
    ]}, COLS)
    ctrs_and2 = countries_in_rows(rows_and2)
    check("B-2  AND[Spain, Portugal] → both ES and PT returned",
          {"ES", "PT"}.issubset(ctrs_and2),
          f"{len(rows_and2)} rows, countries={ctrs_and2}")
    check("B-2  AND[Spain, Portugal] → no other countries",
          ctrs_and2.issubset({"ES", "PT"}),
          f"extra={ctrs_and2 - {'ES', 'PT'}}")

    # B-3: Explicit OR logic — two countries
    rows_or2 = search({"logic": "OR", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"},
        {"field": "site.country", "operator": "equals", "value": "Ireland"},
    ]}, COLS)
    ctrs_or2 = countries_in_rows(rows_or2)
    check("B-3  OR[Spain, Ireland] → both ES and IE returned",
          {"ES", "IE"}.issubset(ctrs_or2),
          f"{len(rows_or2)} rows, countries={ctrs_or2}")

    # B-4: Three countries — AND logic
    rows_and3 = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"},
        {"field": "site.country", "operator": "equals", "value": "Portugal"},
        {"field": "site.country", "operator": "equals", "value": "Ireland"},
    ]}, COLS)
    ctrs_and3 = countries_in_rows(rows_and3)
    check("B-4  AND[Spain, Portugal, Ireland] → ES, PT, IE all returned",
          {"ES", "PT", "IE"}.issubset(ctrs_and3),
          f"{len(rows_and3)} rows, countries={ctrs_and3}")

    # B-5: Five countries — AND logic
    rows_and5 = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"},
        {"field": "site.country", "operator": "equals", "value": "Ireland"},
        {"field": "site.country", "operator": "equals", "value": "Portugal"},
        {"field": "site.country", "operator": "equals", "value": "Germany"},
        {"field": "site.country", "operator": "equals", "value": "Czech Republic"},
    ]}, COLS)
    ctrs_and5 = countries_in_rows(rows_and5)
    check("B-5  AND[Spain,Ireland,Portugal,Germany,CZ] → all 5 present",
          {"ES", "IE", "PT", "DE", "CZ"}.issubset(ctrs_and5),
          f"{len(rows_and5)} rows, countries={ctrs_and5}")

    # B-6: IN operator (comma-separated ISO codes)
    rows_in = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "in", "value": "ES,PT,IE"},
    ]}, COLS)
    ctrs_in = countries_in_rows(rows_in)
    check("B-6  IN[ES,PT,IE] → ES, PT, IE returned",
          {"ES", "PT", "IE"}.issubset(ctrs_in),
          f"{len(rows_in)} rows, countries={ctrs_in}")
    check("B-6  IN[ES,PT,IE] → no other countries",
          ctrs_in.issubset({"ES", "PT", "IE"}),
          f"extra={ctrs_in - {'ES', 'PT', 'IE'}}")

    # B-7: NOT_IN operator
    rows_nin = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "not_in", "value": "ES,PT,IE,DE,FR,IT,GB,NL,BE,CZ,PL,SE,DK,FI,NO,CH,AT,GR,SI,HR,RO,HU,BG,SK,EE,LV,LT,LU,CY,MT,RS,TR,IL,US,CA,AU"},
    ]}, COLS)
    ctrs_nin = countries_in_rows(rows_nin)
    check("B-7  NOT_IN[big list] → none of excluded countries returned",
          not ctrs_nin.intersection({"ES", "PT", "IE", "DE", "FR"}),
          f"found excluded countries: {ctrs_nin.intersection({'ES','PT','IE','DE','FR'})}")

    # B-8: Nested group — (Spain OR Portugal) AND group with second country OR Ireland
    #      Group structure: { logic:AND, rules: [ {group:OR[Spain,Portugal]}, {group:OR[Ireland,Germany]} ] }
    rows_nested = search({
        "logic": "AND",
        "rules": [
            {
                "logic": "OR",
                "rules": [
                    {"field": "site.country", "operator": "equals", "value": "Spain"},
                    {"field": "site.country", "operator": "equals", "value": "Portugal"},
                ]
            },
        ]
    }, COLS)
    ctrs_nested = countries_in_rows(rows_nested)
    check("B-8  Nested group OR[Spain,Portugal] → ES+PT returned",
          {"ES", "PT"}.issubset(ctrs_nested),
          f"{len(rows_nested)} rows, countries={ctrs_nested}")
    check("B-8  Nested group OR[Spain,Portugal] → no other countries",
          ctrs_nested.issubset({"ES", "PT"}),
          f"extra={ctrs_nested - {'ES','PT'}}")

    # B-9: AND with country + city
    rows_cc = search({"logic": "AND", "rules": [
        {"field": "site.country", "operator": "equals", "value": "Spain"},
        {"field": "site.country", "operator": "equals", "value": "Germany"},
        {"field": "site.city",    "operator": "contains", "value": "a"},  # most cities have 'a'
    ]}, COLS)
    ctrs_cc = countries_in_rows(rows_cc)
    check("B-9  AND[Spain,Germany,city contains 'a'] → results are ES or DE only",
          ctrs_cc.issubset({"ES", "DE"}),
          f"extra={ctrs_cc - {'ES','DE'}}")
    print(f"  {INFO}  B-9  {len(rows_cc)} sites in ES/DE with city containing 'a'")

    # B-10: B-2 result count should be >= B-1 (Spain alone) + some Portugal sites
    check("B-10 AND[Spain,Portugal] row count >= Spain alone",
          len(rows_and2) >= len(rows_es),
          f"and2={len(rows_and2)}, es={len(rows_es)}")

# ─── Section C: Moby AI multi-country queries ─────────────────────────────────

def section_c():
    header("C  Moby AI — natural-language multi-country queries")

    if not SESSION_COOKIE:
        skip("C-* all", "SF_SESSION_COOKIE not set")
        return
    if not RUN_MOBY:
        skip("C-* all", "pass --moby to enable")
        return

    def moby_countries(resp: Dict) -> Set[str]:
        """Extract unique countries from Moby table rows, normalized to ISO2."""
        tbl = resp.get("table") or {}
        row_list = tbl.get("rows") or []
        result: Set[str] = set()
        for r in row_list:
            c = r.get("country") or (r.get("data") or {}).get("site.country") or ""
            if c:
                # Moby SOQL handler returns full SF names ("Spain"), Explorer returns ISO2 ("ES")
                norm = (_country_norm(c.strip()) or c.strip()).upper()
                result.add(norm)
        return result

    def moby_last_filters(resp: Dict) -> Optional[Dict]:
        return resp.get("last_filters")

    def check_moby_countries(label: str, resp: Dict, expected: Set[str]) -> None:
        ctrs = moby_countries(resp)
        tbl  = resp.get("table") or {}
        n    = len(tbl.get("rows") or [])
        ok   = expected.issubset(ctrs) and n > 0
        check(label, ok, f"{n} rows, countries={ctrs}")

    # C-1: Two countries in English
    print(f"\n  Querying: 'give me the sites in Spain and Portugal'")
    r1 = chat("give me the sites in Spain and Portugal")
    check_moby_countries("C-1  Spain and Portugal → ES+PT in table", r1, {"ES", "PT"})
    lf1 = moby_last_filters(r1)
    check("C-1  last_filters contains OR logic or both countries",
          lf1 is not None and (
              lf1.get("logic") == "OR" or
              len(lf1.get("rules", [])) >= 2 or
              "IN" in json.dumps(lf1).upper()
          ),
          f"last_filters={json.dumps(lf1)[:120]}")
    time.sleep(2)

    # C-2: Three countries
    print(f"\n  Querying: 'show me clinical sites in Spain, Portugal, Ireland'")
    r2 = chat("show me clinical sites in Spain, Portugal, Ireland")
    check_moby_countries("C-2  Spain, Portugal, Ireland → ES+PT+IE in table", r2, {"ES", "PT", "IE"})
    time.sleep(2)

    # C-3: Five countries (original user story)
    print(f"\n  Querying: 'clinical trial sites in Spain, Ireland, Portugal, Germany, and Czech Republic'")
    r3 = chat("I need to see the clinical trial sites in Spain, Ireland, Portugal, Germany, and Czech Republic")
    check_moby_countries("C-3  5 countries → ES+IE+PT+DE+CZ in table", r3, {"ES", "IE", "PT", "DE", "CZ"})
    time.sleep(2)

    # C-4: OR phrasing
    print(f"\n  Querying: 'sites in France or Italy'")
    r4 = chat("sites in France or Italy")
    check_moby_countries("C-4  France or Italy → FR+IT in table", r4, {"FR", "IT"})
    time.sleep(2)

    # C-5: Spanish-language multi-country
    print(f"\n  Querying: 'dame los sitios en España y Alemania'")
    r5 = chat("dame los sitios en España y Alemania")
    check_moby_countries("C-5  España y Alemania → ES+DE in table", r5, {"ES", "DE"})
    time.sleep(2)

    # C-6: last_filters from C-2 usable as Explorer filter
    lf2 = moby_last_filters(r2)
    if lf2:
        try:
            rows_from_lf = search(lf2, ["site.country", "site.city"])
            ctrs_lf = countries_in_rows(rows_from_lf)
            check("C-6  last_filters from C-2 reused in Explorer → ES+PT+IE",
                  {"ES", "PT", "IE"}.issubset(ctrs_lf),
                  f"{len(rows_from_lf)} rows, countries={ctrs_lf}")
        except Exception as e:
            check("C-6  last_filters from C-2 reused in Explorer", False, str(e))
    else:
        skip("C-6  last_filters reuse", "no last_filters in C-2 response")

    # C-7: Multi-turn — refine with additional filter
    print(f"\n  Multi-turn: first ask Spain+Germany, then add Stage1 > 0")
    r7a = chat("sites in Spain and Germany")
    lf7 = moby_last_filters(r7a)
    time.sleep(2)
    r7b = chat("now only those with at least 1 Stage 1 individual", last_filters=lf7)
    tbl7 = r7b.get("table") or {}
    rows7 = tbl7.get("rows") or []
    ctrs7 = moby_countries(r7b)
    check("C-7  Multi-turn refine → results still ES+DE only",
          ctrs7.issubset({"ES", "DE"}) and len(rows7) > 0,
          f"{len(rows7)} rows, countries={ctrs7}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"\n\033[1mMulti-country filter test battery\033[0m")
    print(f"  API: {BASE_URL}")
    print(f"  SF session: {'set (' + SESSION_COOKIE[:8] + '...)' if SESSION_COOKIE else 'NOT SET'}")
    print(f"  Moby tests: {'enabled' if RUN_MOBY else 'disabled (pass --moby)'}")

    section_a()
    section_b()
    section_c()

    print(f"\n{'─'*60}")
    if errors:
        print(f"\033[91m  {len(errors)} test(s) FAILED:\033[0m")
        for e in errors:
            print(f"    ✗  {e}")
        sys.exit(1)
    else:
        print(f"\033[92m  All tests passed.\033[0m")

if __name__ == "__main__":
    main()
