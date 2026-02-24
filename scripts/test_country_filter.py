#!/usr/bin/env python3
"""
Tests for the country filter fix.

1) Unit tests  — validate _country_norm + pass_site logic in isolation
2) Integration test — call the production /api/explorer/search endpoint
                      (requires a valid Salesforce session cookie)

Usage:
  # Unit tests only (no network needed):
  python scripts/test_country_filter.py

  # Unit + integration (need SF session cookie):
  SF_SESSION_COOKIE="<value of sf_access_token cookie from the browser>" \
    python scripts/test_country_filter.py --integration
"""

import sys
import json
import os
from typing import Optional, Any, Dict, List

# ─── Reproduce the backend logic locally ─────────────────────────────────────

_ISO2 = {
    "spain": "ES", "españa": "ES", "espana": "ES", "es": "ES",
    "portugal": "PT", "pt": "PT",
    "france": "FR", "francia": "FR", "fr": "FR",
    "germany": "DE", "deutschland": "DE", "de": "DE",
    "italy": "IT", "italia": "IT", "it": "IT",
    "belgium": "BE", "be": "BE",
    "netherlands": "NL", "nl": "NL",
    "united kingdom": "GB", "uk": "GB", "gb": "GB",
    "ireland": "IE", "ie": "IE",
    "denmark": "DK", "dk": "DK",
    "sweden": "SE", "se": "SE",
    "norway": "NO", "no": "NO",
    "finland": "FI", "fi": "FI",
    "poland": "PL", "pl": "PL",
    "czech republic": "CZ", "czechia": "CZ", "cz": "CZ",
    "greece": "GR", "gr": "GR",
    "switzerland": "CH", "ch": "CH",
    "turkey": "TR", "tr": "TR",
}

def _country_norm(country: Optional[str]) -> Optional[str]:
    if not country:
        return None
    s = str(country).strip().lower()
    if s in _ISO2:
        return _ISO2[s]
    if len(s) == 2:
        return s.upper()
    return s.title()

def _op_norm(op: str) -> str:
    synonyms = {"eq": "equals", "neq": "not_equals", "=": "equals", "!=": "not_equals"}
    return synonyms.get(op, op)

def pass_site(acc: Dict[str, Any], site_rules: List[Dict], logic: str = "AND") -> bool:
    """Reproduces the backend pass_site logic including the country fix."""
    if not site_rules:
        return True

    def match_one(rule: Dict) -> bool:
        val = ""
        if rule["field"] == "site.city":    val = acc.get("city") or ""
        if rule["field"] == "site.country": val = acc.get("country") or ""
        op = _op_norm(rule.get("operator", "equals"))
        s  = str(rule.get("value") or "")

        # Normalize both stored value and filter value to ISO codes
        # so "Italy"/"italy"/"IT" all match regardless of how data is stored.
        if rule["field"] == "site.country" and op in ("equals", "=", "not_equals", "!="):
            s   = (_country_norm(s) or s).upper()
            val = (_country_norm(val) or val).upper()

        if   op in ("equals", "="):      return val.lower() == s.lower()
        elif op in ("not_equals", "!="): return val.lower() != s.lower()
        elif op == "contains":           return s.lower() in val.lower()
        elif op == "not_contains":       return s.lower() not in val.lower()
        elif op == "starts_with":        return val.lower().startswith(s.lower())
        elif op == "ends_with":          return val.lower().endswith(s.lower())
        return True

    results = [match_one(r) for r in site_rules]
    return all(results) if logic == "AND" else any(results)


# ─── UNIT TESTS ──────────────────────────────────────────────────────────────

PASS  = "\033[92m✓\033[0m"
FAIL  = "\033[91m✗\033[0m"
errors: List[str] = []

def check(description: str, actual: Any, expected: Any) -> None:
    ok = actual == expected
    icon = PASS if ok else FAIL
    print(f"  {icon}  {description}")
    if not ok:
        errors.append(f"{description}: expected {expected!r}, got {actual!r}")


def run_unit_tests() -> None:
    print("\n── Unit tests: _country_norm ──────────────────────────────────")

    check("'Spain'  → 'ES'",          _country_norm("Spain"),          "ES")
    check("'spain'  → 'ES'",          _country_norm("spain"),          "ES")
    check("'SPAIN'  → 'ES'",          _country_norm("SPAIN"),          "ES")
    check("'ES'     → 'ES'",          _country_norm("ES"),             "ES")
    check("'es'     → 'ES'",          _country_norm("es"),             "ES")
    check("'España' → 'ES'",          _country_norm("España"),         "ES")
    check("'Germany'→ 'DE'",          _country_norm("Germany"),        "DE")
    check("'DE'     → 'DE'",          _country_norm("DE"),             "DE")
    check("'France' → 'FR'",          _country_norm("France"),         "FR")
    check("'United Kingdom' → 'GB'",  _country_norm("United Kingdom"), "GB")
    check("None     → None",          _country_norm(None),             None)
    check("''       → None",          _country_norm(""),               None)

    print("\n── Unit tests: pass_site country filter ───────────────────────")

    # Account with country stored as ISO code "ES"
    acc_es = {"country": "ES", "city": "Madrid"}
    # Account with country stored as ISO code "DE"
    acc_de = {"country": "DE", "city": "Berlin"}
    # Account with country stored as full name (lowercase) — real-world case
    acc_italy = {"country": "italy", "city": "Rome"}
    acc_Italy = {"country": "Italy", "city": "Milan"}

    rule_spain    = {"field": "site.country", "operator": "equals",     "value": "Spain"}
    rule_ES       = {"field": "site.country", "operator": "equals",     "value": "ES"}
    rule_es_lower = {"field": "site.country", "operator": "equals",     "value": "es"}
    rule_not_ES   = {"field": "site.country", "operator": "not_equals", "value": "ES"}
    rule_contains = {"field": "site.country", "operator": "contains",   "value": "E"}
    rule_de       = {"field": "site.country", "operator": "equals",     "value": "DE"}
    rule_germany  = {"field": "site.country", "operator": "equals",     "value": "Germany"}
    rule_italy    = {"field": "site.country", "operator": "equals",     "value": "Italy"}
    rule_IT       = {"field": "site.country", "operator": "equals",     "value": "IT"}

    # equals: full name should match ISO
    check("acc(ES)  matches filter 'Spain'",   pass_site(acc_es, [rule_spain]),    True)
    check("acc(ES)  matches filter 'ES'",      pass_site(acc_es, [rule_ES]),       True)
    check("acc(ES)  matches filter 'es'",      pass_site(acc_es, [rule_es_lower]), True)
    check("acc(DE)  no match filter 'Spain'",  pass_site(acc_de, [rule_spain]),    False)
    check("acc(DE)  matches filter 'DE'",      pass_site(acc_de, [rule_de]),       True)
    check("acc(DE)  matches filter 'Germany'", pass_site(acc_de, [rule_germany]),  True)

    # stored as full name (lowercase) — "italy" should match "Italy" and "IT"
    check("acc('italy') matches filter 'Italy'", pass_site(acc_italy, [rule_italy]), True)
    check("acc('italy') matches filter 'IT'",    pass_site(acc_italy, [rule_IT]),    True)
    check("acc('Italy') matches filter 'IT'",    pass_site(acc_Italy, [rule_IT]),    True)
    check("acc('Italy') matches filter 'Italy'", pass_site(acc_Italy, [rule_italy]), True)
    check("acc(ES) no match filter 'Italy'",     pass_site(acc_es,    [rule_italy]), False)

    # not_equals
    check("acc(ES)  not_equals 'ES'  → False", pass_site(acc_es, [rule_not_ES]),   False)
    check("acc(DE)  not_equals 'ES'  → True",  pass_site(acc_de, [rule_not_ES]),   True)

    # contains (uses raw value, not normalized — keeps original behaviour)
    check("acc(ES)  contains 'E' → True",      pass_site(acc_es, [rule_contains]), True)

    # no rules → always passes
    check("no rules → True",                   pass_site(acc_es, []),              True)

    print("\n── Unit tests: AND / OR logic ─────────────────────────────────")

    rule_madrid = {"field": "site.city", "operator": "equals", "value": "Madrid"}
    rule_berlin = {"field": "site.city", "operator": "equals", "value": "Berlin"}

    # AND: country=ES AND city=Madrid → True for acc_es
    check("ES AND Madrid  → True",  pass_site(acc_es, [rule_ES, rule_madrid], "AND"), True)
    # AND: country=ES AND city=Berlin → False
    check("ES AND Berlin  → False", pass_site(acc_es, [rule_ES, rule_berlin], "AND"), False)
    # OR: country=ES OR city=Berlin → True (first matches)
    check("ES OR Berlin   → True",  pass_site(acc_es, [rule_ES, rule_berlin], "OR"),  True)


# ─── INTEGRATION TEST ────────────────────────────────────────────────────────

def run_integration_test(base_url: str, session_cookie: str) -> None:
    import urllib.request
    import urllib.error

    print(f"\n── Integration test: {base_url} ───────────────────────────────")

    countries_to_test = [
        ("Spain",   "ES"),
        ("ES",      "ES"),
        ("Germany", "DE"),
        ("DE",      "DE"),
        ("France",  "FR"),
    ]

    for country_input, expected_iso in countries_to_test:
        payload = json.dumps({
            "filters": {
                "logic": "AND",
                "rules": [{"field": "site.country", "operator": "equals", "value": country_input}]
            },
            "columns": ["sf.Account.Name", "site.country", "site.city"]
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/api/explorer/search",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Cookie": f"sf_session={session_cookie}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            rows = data.get("rows", [])
            countries_returned = set(r.get("country", "") for r in rows if r.get("country"))

            if not rows:
                print(f"  \033[93m?\033[0m  filter '{country_input}': 0 results (no data for this country?)")
                continue

            mismatched = [c for c in countries_returned if c.upper() != expected_iso]
            if mismatched:
                print(f"  {FAIL}  filter '{country_input}': got countries {countries_returned} (expected only {expected_iso!r})")
                errors.append(f"filter '{country_input}': unexpected countries {mismatched}")
            else:
                print(f"  {PASS}  filter '{country_input}': {len(rows)} rows, all country='{expected_iso}'")

        except urllib.error.HTTPError as e:
            if e.code == 401:
                print(f"  \033[93m!\033[0m  401 Unauthorized — SF session cookie may be invalid or expired")
            else:
                print(f"  {FAIL}  HTTP {e.code}: {e.reason}")
                errors.append(f"HTTP error {e.code} for country '{country_input}'")
        except Exception as e:
            print(f"  {FAIL}  {e}")
            errors.append(str(e))


# ─── MAIN ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_integration = "--integration" in sys.argv

    print("=" * 55)
    print("  Country filter tests")
    print("=" * 55)

    run_unit_tests()

    if run_integration:
        session_cookie = os.environ.get("SF_SESSION_COOKIE", "")
        base_url = os.environ.get("API_BASE", "https://cts-innodia-dashboard.org")
        if not session_cookie:
            print("\n\033[93mWARNING: SF_SESSION_COOKIE not set. Integration test will likely return 401.\033[0m")
            print("Get it from browser DevTools → Application → Cookies → sf_session\n")
        run_integration_test(base_url, session_cookie)

    print("\n" + "=" * 55)
    if errors:
        print(f"\033[91m  FAILED — {len(errors)} error(s):\033[0m")
        for e in errors:
            print(f"  • {e}")
        sys.exit(1)
    else:
        tests_run = 26 + (5 if run_integration else 0)
        print(f"\033[92m  ALL PASSED ({tests_run} checks)\033[0m")
    print("=" * 55)
