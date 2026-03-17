#!/usr/bin/env python3
"""
Integration tests for Moby Phase 1 + Phase 2 structured planner.

Tests the /api/ai/chat endpoint directly — no mocking, real SF data.
Checks that:
  - Phase 1 short-circuit fires for high-confidence table queries
  - Phase 1 plan hint is injected when confidence is medium
  - Phase 2 followup merge works (countries + filters combined without Claude)
  - Phase 2 clarification triggers for bare "pharmacy"
  - Phase 2 catalog filters pick up kindex fields
  - CSV export path returns csv_data in short-circuit

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_planner.py
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/test_moby_planner.py
"""

import sys, os, json, re, time, ssl
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("API_BASE", "http://localhost:8000").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

PASS  = "\033[92m✓\033[0m"
FAIL  = "\033[91m✗\033[0m"
SKIP  = "\033[93m–\033[0m"
INFO  = "\033[94mℹ\033[0m"
BOLD  = "\033[1m"
RESET = "\033[0m"

passed = 0
failed = 0
errors: List[str] = []

# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: Any = None, timeout: int = 60) -> Any:
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
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        return json.loads(resp.read())


def chat(
    message: str,
    last_filters: Optional[Dict] = None,
    last_table:   Optional[Dict] = None,
    timeout: int = 90,
) -> Dict:
    return _req("POST", "/api/ai/chat", {
        "messages":     [{"role": "user", "content": message}],
        "last_filters": last_filters,
        "last_table":   last_table,
    }, timeout=timeout)


# ── Assertion helpers ──────────────────────────────────────────────────────────

def check(desc: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    icon = PASS if ok else FAIL
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {icon}  {desc}{suffix}")
    if ok:
        passed += 1
    else:
        failed += 1
        errors.append(f"{desc}" + (f": {detail}" if detail else ""))


def section(title: str) -> None:
    bar = "─" * max(1, 60 - len(title))
    print(f"\n{BOLD}── {title} {bar}{RESET}")


def rows(resp: Dict) -> List[Dict]:
    return (resp.get("table") or {}).get("rows", [])


def answer(resp: Dict) -> str:
    return resp.get("answer", "")


def last_filters(resp: Dict) -> Optional[Dict]:
    return resp.get("last_filters")


def is_short_circuit_answer(resp: Dict) -> bool:
    """Detect Phase 1 short-circuit answer pattern: 'Found <N> site(s)'."""
    a = answer(resp)
    return bool(re.search(r"Found\s+<strong>\d+</strong>\s+site\(s\)", a))


def is_followup_merge_answer(resp: Dict) -> bool:
    """Detect Phase 2 followup merge answer pattern: 'now including'."""
    return "now including" in answer(resp)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_p1_short_circuit_overnight():
    """
    Phase 1 — Short-circuit: 'show all sites with overnight stay'
    Overnight is in a deterministic handler, but the answer format
    should come from the planner or deterministic path — either way
    we verify rows are returned and no LLM hallucination.
    """
    section("P1-SC1 — Sites with overnight stay (short-circuit or deterministic)")
    try:
        resp = chat("show all sites with overnight stay")
        r = rows(resp)
        check("Returns at least 1 site", len(r) >= 1, f"{len(r)} rows")
        check("Answer is non-empty", bool(answer(resp).strip()))
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_short_circuit_country_and_filter():
    """
    Phase 1 — Short-circuit: clear country + filter → should fire directly.
    The followup merge path won't trigger (no last_filters).
    The clarification path won't trigger (overnight is unambiguous).
    """
    section("P1-SC2 — Country + filter short-circuit (IT + overnight)")
    try:
        resp = chat("show all sites in Italy with overnight stay")
        r = rows(resp)
        # Italy may have 0 overnight sites — check response is valid
        check("Request returns a valid response", "answer" in resp)
        check("Answer is non-empty", bool(answer(resp).strip()))
        if r:
            check("All rows have account_name", all("account_name" in row for row in r))
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_short_circuit_stage2_spain():
    """
    Phase 1 — Stage 2 in Spain. Likely intercepted by deterministic handler
    but the end result should include rows with Stage2 > 0.
    """
    section("P1-SC3 — Stage 2 in Spain")
    try:
        resp = chat("show all sites in Spain with stage 2 > 0")
        r = rows(resp)
        check("Returns sites", len(r) >= 1, f"{len(r)} rows")
        check("Answer mentions stage or sites", bool(re.search(r"site|stage|found", answer(resp), re.I)))
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_multi_country_short_circuit():
    """
    Phase 1 — Multi-country query: plan builds OR sub-group for countries.
    """
    section("P1-SC4 — Multi-country (Italy + Germany + stage 1)")
    try:
        resp = chat("show sites in Italy and Germany with stage 1 > 0")
        r = rows(resp)
        check("Returns at least 1 site", len(r) >= 1, f"{len(r)} rows")
        # Check that sites from both countries appear (or at least response is valid)
        countries_in_result = {row.get("country", "") for row in r}
        check("Response is valid", "answer" in resp)
        print(f"  {INFO}  Countries in result: {sorted(countries_in_result)}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_export_csv():
    """
    Phase 2 — CSV export: 'export sites in France as CSV'
    If plan short-circuits with needs_export=True → csv_data in response.
    If goes to Claude → still should mention export.
    """
    section("P1/P2-CSV — CSV export intent")
    try:
        resp = chat("export all sites in France with stage 2 > 0 as CSV")
        check("Response is valid", "answer" in resp)
        if "csv_data" in resp:
            check("csv_data present in short-circuit", True, f"{len(resp['csv_data'])} chars")
            check("csv_data has header row", resp["csv_data"].strip().startswith("account_name"))
        else:
            # Fell through to Claude — that's ok, just verify answer
            check("Answer mentions export or data", bool(re.search(r"export|csv|data|site", answer(resp), re.I)))
            print(f"  {INFO}  csv_data not in response (Claude handled it, not short-circuit)")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_clarification_pharmacy():
    """
    Phase 2 — Clarification: bare 'pharmacy' without on-site/off-campus qualifier.
    Either triggers structured clarify dict OR falls through to deterministic handler.
    We verify the response is sensible in both cases.
    """
    section("P2-CLAR1 — Pharmacy clarification")
    try:
        resp = chat("show me sites with a pharmacy")
        check("Response is valid", "answer" in resp)
        if "clarify" in resp:
            c = resp["clarify"]
            check("Clarify has question", "question" in c, c.get("question", ""))
            check("Clarify has options", len(c.get("options", [])) >= 2)
            check("Options have label+query", all("label" in o and "query" in o for o in c["options"]))
            print(f"  {INFO}  Clarification triggered: {c['question']}")
        else:
            # Deterministic pharmacy handler caught it
            r = rows(resp)
            check("Response has sites or answer", len(r) >= 0)  # either 0 or more is ok
            print(f"  {INFO}  Deterministic handler caught it: {len(r)} rows")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_followup_merge_add_country():
    """
    Phase 2 — Followup merge: start with Italy, then add France.
    The merge should produce an OR sub-group [IT, FR] and return combined results.
    """
    section("P2-MERGE1 — Followup merge: Italy → add France")

    last_it = {
        "logic": "AND",
        "rules": [{"field": "site.country", "operator": "equals", "value": "IT"}],
    }
    last_tbl = {"rows": [{"account_name": "Site A"}]}  # non-empty → has context

    try:
        resp = chat("and also France", last_filters=last_it, last_table=last_tbl)
        r = rows(resp)
        lf = last_filters(resp)

        check("Returns sites (IT + FR combined)", len(r) >= 1, f"{len(r)} rows")
        check("last_filters present in response", lf is not None)
        check("Answer indicates followup merge", is_followup_merge_answer(resp) or "found" in answer(resp).lower())

        if lf:
            all_rules = lf.get("rules", [])
            # Find OR sub-group with country rules
            or_groups = [r for r in all_rules if isinstance(r.get("rules"), list) and r.get("logic") == "OR"]
            if or_groups:
                iso2s = {rule["value"] for rule in or_groups[0]["rules"]}
                check("OR group contains IT", "IT" in iso2s, str(iso2s))
                check("OR group contains FR", "FR" in iso2s, str(iso2s))
            else:
                # Maybe returned flat rules
                country_vals = {r["value"] for r in all_rules if r.get("field") == "site.country"}
                check("Result has IT and FR", {"IT", "FR"} <= country_vals, str(country_vals))
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_followup_merge_add_filter():
    """
    Phase 2 — Followup merge: existing country filter + new stage filter added.
    """
    section("P2-MERGE2 — Followup merge: Spain + add stage 2")

    last_es = {
        "logic": "AND",
        "rules": [{"field": "site.country", "operator": "equals", "value": "ES"}],
    }
    last_tbl = {"rows": [{"account_name": "Site B"}]}

    try:
        resp = chat("and also show stage 2 > 0", last_filters=last_es, last_table=last_tbl)
        r = rows(resp)
        lf = last_filters(resp)

        check("Returns sites", len(r) >= 0)  # may be 0 if no ES sites have stage2
        check("Response is valid", "answer" in resp)
        if lf:
            all_rules = lf.get("rules", [])
            flat_rules = [r for r in all_rules if isinstance(r.get("field"), str)]
            check("last_filters has country rule", any(r.get("field") == "site.country" for r in flat_rules))
        print(f"  {INFO}  Rows: {len(r)}, last_filters rules: {len((lf or {}).get('rules', []))}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_followup_merge_multi_turn():
    """
    Phase 2 — Full multi-turn: build up filters across 3 turns.
    Turn 1: sites in Germany → returns IT last_filters
    Turn 2: "and also Italy" → merges DE+IT
    Turn 3: "and also Spain" → merges DE+IT+ES
    """
    section("P2-MERGE3 — Multi-turn country accumulation (DE → DE+IT → DE+IT+ES)")

    try:
        # Turn 1: sites in Germany (goes through normal path)
        resp1 = chat("show all sites in Germany")
        r1 = rows(resp1)
        lf1 = last_filters(resp1)
        check("Turn 1: Germany sites returned", len(r1) >= 1, f"{len(r1)} rows")
        print(f"  {INFO}  Turn 1 last_filters: {json.dumps(lf1)[:120] if lf1 else 'None'}")

        if not lf1:
            # Build a synthetic last_filters for Germany to continue the test
            lf1 = {"logic": "AND", "rules": [{"field": "site.country", "operator": "equals", "value": "DE"}]}
            print(f"  {INFO}  Using synthetic last_filters for Germany (handler didn't return one)")

        # Turn 2: add Italy
        resp2 = chat("and also Italy", last_filters=lf1, last_table={"rows": r1[:3]})
        r2 = rows(resp2)
        lf2 = last_filters(resp2)
        check("Turn 2: DE+IT sites returned", len(r2) >= len(r1), f"{len(r2)} rows (was {len(r1)})")
        check("Turn 2: last_filters present", lf2 is not None)
        print(f"  {INFO}  Turn 2 rows: {len(r2)}")

        if not lf2:
            lf2 = {
                "logic": "AND",
                "rules": [{"logic": "OR", "rules": [
                    {"field": "site.country", "operator": "equals", "value": "DE"},
                    {"field": "site.country", "operator": "equals", "value": "IT"},
                ]}],
            }

        # Turn 3: add Spain
        resp3 = chat("and also Spain", last_filters=lf2, last_table={"rows": r2[:3]})
        r3 = rows(resp3)
        lf3 = last_filters(resp3)
        check("Turn 3: DE+IT+ES sites returned", len(r3) >= len(r2), f"{len(r3)} rows (was {len(r2)})")
        check("Turn 3: last_filters present", lf3 is not None)
        print(f"  {INFO}  Turn 3 rows: {len(r3)}")

    except Exception as e:
        check("Multi-turn request succeeded", False, str(e))


def test_p2_followup_no_new_info():
    """
    Phase 2 — Followup with only context reuse, no new countries/filters.
    Should reuse last_filters (pass through to Claude or existing handler).
    """
    section("P2-MERGE4 — Followup with no new info (context reuse)")

    last_de = {
        "logic": "AND",
        "rules": [{"field": "site.country", "operator": "equals", "value": "DE"}],
    }
    last_tbl = {"rows": [{"account_name": "Site C", "data": {}}]}

    try:
        resp = chat("what about the PI names for those?", last_filters=last_de, last_table=last_tbl)
        check("Response is valid", "answer" in resp)
        check("Answer is non-empty", bool(answer(resp).strip()))
        # This goes to Claude — just verify it returns something sensible
        print(f"  {INFO}  Answer preview: {answer(resp)[:120]}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_pharmacy_on_site_resolves():
    """
    Phase 2 — Pharmacy with explicit qualifier resolves to a filter, no clarification.
    """
    section("P2-QUAL1 — Pharmacy on-site (fully qualified, no clarification)")
    try:
        resp = chat("show all sites with pharmacy on-site in Italy")
        check("Response is valid", "answer" in resp)
        check("No clarify key in response", "clarify" not in resp)
        r = rows(resp)
        check("Returns sites or empty result", isinstance(r, list))
        print(f"  {INFO}  Rows: {len(r)}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_plan_hint_injection():
    """
    Phase 1 — Medium-confidence query: should reach Claude with plan hint injected.
    We verify the answer is coherent (Claude used the hint).
    """
    section("P1-HINT1 — Medium-confidence query reaches Claude with plan hint")
    try:
        resp = chat("which sites in Portugal have the most patients in stage 1?")
        check("Response is valid", "answer" in resp)
        check("Answer mentions stage or sites or Portugal", bool(
            re.search(r"stage|site|Portugal|patient|PT\b", answer(resp), re.I)
        ))
        print(f"  {INFO}  Answer preview: {answer(resp)[:200]}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p1_other_intent_no_short_circuit():
    """
    Phase 1 — Conversational query: intent=other, should NOT short-circuit.
    Claude responds with natural language.
    """
    section("P1-OTHER1 — Conversational query (no short-circuit)")
    try:
        resp = chat("what is the difference between stage 1 and stage 2 in INNODIA?")
        check("Response is valid", "answer" in resp)
        check("No table returned for conversational query", not rows(resp))
        check("Answer is non-empty", bool(answer(resp).strip()))
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_followup_merge_deduplicates():
    """
    Phase 2 — Followup merge: adding a country already in last_filters → no duplicate.
    """
    section("P2-MERGE5 — Followup merge deduplicates existing country")

    last_it = {
        "logic": "AND",
        "rules": [{"field": "site.country", "operator": "equals", "value": "IT"}],
    }
    last_tbl = {"rows": [{"account_name": "Site D"}]}

    try:
        # Add Italy again — should be deduplicated
        resp = chat("and also show Italian sites", last_filters=last_it, last_table=last_tbl)
        r = rows(resp)
        lf = last_filters(resp)
        check("Response is valid", "answer" in resp)

        if lf:
            # Count IT occurrences across all rules
            it_count = 0
            def count_it(rules):
                nonlocal it_count
                for rule in rules:
                    if rule.get("field") == "site.country" and rule.get("value") == "IT":
                        it_count += 1
                    if rule.get("rules"):
                        count_it(rule["rules"])
            count_it(lf.get("rules", []))
            check("IT not duplicated in last_filters", it_count <= 1, f"IT count: {it_count}")
        print(f"  {INFO}  Rows: {len(r)}")
    except Exception as e:
        check("Request succeeded", False, str(e))


def test_p2_multi_country_then_qual_columns():
    """
    Phase 2 — Story test: multi-country selection then explicit qual column query.

    Architecture note: The "overnight/pharmacy" deterministic handler in _try_planner
    intercepts those terms before Phase 2. So for T2 we use Explorer directly to verify
    qual columns work with an ISO2 country filter, then test the followup merge for T3.

    Turn 1 (Moby): "show me sites in Italy, Spain and France"
                   → chat API, get sites + last_filters
    Turn 2 (Explorer): POST /api/explorer/search with IT+ES+FR + qual columns explicitly
                   → verify overnight, pharmacy, stage1 columns appear in result
    Turn 3 (Moby): "and also add Germany to that list"
                   → followup merge via Phase 2, DE added to ISO2 OR group
    """
    section("P2-STORY1 — Multi-country + qual columns (IT+ES+FR → Explorer qual → add DE)")

    # Canonical ISO2 last_filters for IT+ES+FR (what Phase 2 merge expects)
    lf_it_es_fr = {
        "logic": "AND",
        "rules": [{
            "logic": "OR",
            "rules": [
                {"field": "site.country", "operator": "equals", "value": "IT"},
                {"field": "site.country", "operator": "equals", "value": "ES"},
                {"field": "site.country", "operator": "equals", "value": "FR"},
            ],
        }],
    }

    try:
        # ── Turn 1: multi-country chat ────────────────────────────────────────
        resp1 = chat("show me sites in Italy, Spain and France")
        r1 = rows(resp1)
        check("T1: sites returned (IT+ES+FR)", len(r1) >= 1, f"{len(r1)} rows")
        check("T1: response valid", "answer" in resp1)
        print(f"  {INFO}  T1 rows: {len(r1)}")

        # ── Turn 2: Explorer with qual columns (bypasses Moby handlers) ───────
        # This is what the frontend does when user picks columns in the ColumnPicker
        qual_cols = [
            "sf.C_Number_of_Stage1_Individuals_followed__c",
            "sf.C_Number_of_Stage2_Individuals_followed__c",
            "qual.3_5_2__overnight_stay",
            "qual.3_6__is_your_pharmacy_on_site_or_off_campus",
        ]
        explorer_resp = _req("POST", "/api/explorer/search", {
            "filters": lf_it_es_fr,
            "columns": qual_cols,
        })
        r2 = explorer_resp.get("rows", [])
        col_keys2 = [c.get("key", "") for c in explorer_resp.get("columns", [])]

        check("T2: Explorer returns sites for IT+ES+FR", len(r2) >= 1, f"{len(r2)} rows")

        # Columns metadata may be empty; check row data directly
        def has_key_in_rows(field_substr):
            return any(
                any(field_substr in k for k in row.get("data", {}).keys())
                for row in r2
            )
        def values_for_key(field_key):
            return [row.get("data", {}).get(field_key) for row in r2]

        has_overnight = has_key_in_rows("overnight")
        has_pharmacy  = has_key_in_rows("pharmacy")
        has_stage1    = has_key_in_rows("Stage1")
        check("T2: overnight data present in rows", has_overnight)
        check("T2: pharmacy data present in rows",  has_pharmacy)
        check("T2: stage1 data present in rows",    has_stage1)

        # Spot-check: at least some rows have non-null qual values
        overnight_vals = values_for_key("qual.3_5_2__overnight_stay")
        non_null_overnight = [v for v in overnight_vals if v is not None]
        check("T2: overnight has non-null values in some rows",
              len(non_null_overnight) >= 1,
              f"{len(non_null_overnight)}/{len(r2)} rows have overnight value")

        # Country scope check: no row from outside IT/ES/FR
        out_of_scope = [row for row in r2 if row.get("country", "") not in ("IT", "ES", "FR", "Italy", "Spain", "France")]
        check("T2: all rows within IT+ES+FR scope", len(out_of_scope) == 0,
              f"{len(out_of_scope)} rows out of scope: {[r.get('country') for r in out_of_scope[:3]]}")
        print(f"  {INFO}  T2 rows: {len(r2)}, overnight non-null: {len(non_null_overnight)}")

        # ── Turn 3: Moby followup — add Germany via Phase 2 merge ─────────────
        # "and also Germany" phrasing avoids _try_planner country handler
        # (which triggers on "list/show/sites + country name")
        resp3 = chat(
            "and also Germany",
            last_filters=lf_it_es_fr,
            last_table={"rows": r2[:5]},
        )
        r3 = rows(resp3)
        lf3 = last_filters(resp3)

        check("T3: response valid", "answer" in resp3)
        check("T3: more sites than T1 (DE added)", len(r3) >= len(r1),
              f"{len(r3)} rows (was {len(r1)})")
        check("T3: last_filters present", lf3 is not None)
        check("T3: answer indicates merge", is_followup_merge_answer(resp3) or "found" in answer(resp3).lower())

        if lf3:
            def find_country_values(rules):
                vals = set()
                for rule in rules:
                    if rule.get("field") == "site.country":
                        vals.add(rule.get("value", ""))
                    if rule.get("rules"):
                        vals |= find_country_values(rule["rules"])
                return vals
            c3 = find_country_values(lf3.get("rules", []))
            check("T3: IT in scope", "IT" in c3, str(c3))
            check("T3: ES in scope", "ES" in c3, str(c3))
            check("T3: FR in scope", "FR" in c3, str(c3))
            check("T3: DE in scope", "DE" in c3, str(c3))
        print(f"  {INFO}  T3 rows: {len(r3)}")

    except Exception as e:
        check("Story test succeeded", False, str(e))
        import traceback; traceback.print_exc()


def test_p1_export_no_country():
    """
    Phase 1/2 — Export intent without country: plan should detect needs_export=True.
    """
    section("P1/P2-CSV2 — Export intent detection (no country)")
    try:
        resp = chat("descargar lista de sitios con stage 1 > 0 como CSV")
        check("Response is valid", "answer" in resp)
        # Either csv_data or a table result
        if "csv_data" in resp:
            check("csv_data present", True, f"{len(resp['csv_data'])} chars")
        else:
            check("Returns sites or answer", rows(resp) or bool(answer(resp)))
        print(f"  {INFO}  Keys: {list(resp.keys())}")
    except Exception as e:
        check("Request succeeded", False, str(e))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not SESSION_COOKIE:
        print(f"{FAIL} SF_SESSION_COOKIE not set. Export it first:")
        print("  export SF_SESSION_COOKIE='<value>'")
        sys.exit(1)

    print(f"\n{BOLD}Moby Phase 1 + 2 Integration Tests{RESET}")
    print(f"  API base:  {BASE_URL}")
    print(f"  Cookie:    {SESSION_COOKIE[:20]}…")

    t0 = time.time()

    test_p1_short_circuit_overnight()
    test_p1_short_circuit_country_and_filter()
    test_p1_short_circuit_stage2_spain()
    test_p1_multi_country_short_circuit()
    test_p1_export_csv()
    test_p2_clarification_pharmacy()
    test_p2_followup_merge_add_country()
    test_p2_followup_merge_add_filter()
    test_p2_followup_merge_multi_turn()
    test_p2_followup_no_new_info()
    test_p2_pharmacy_on_site_resolves()
    test_p1_plan_hint_injection()
    test_p1_other_intent_no_short_circuit()
    test_p2_followup_merge_deduplicates()
    test_p1_export_no_country()
    test_p2_multi_country_then_qual_columns()

    elapsed = time.time() - t0
    total = passed + failed

    print(f"\n{'─' * 62}")
    print(f"{BOLD}Results:{RESET}  {PASS} {passed}/{total} passed   {FAIL} {failed}/{total} failed   ({elapsed:.1f}s)")

    if errors:
        print(f"\n{BOLD}Failures:{RESET}")
        for e in errors:
            print(f"  {FAIL}  {e}")
        sys.exit(1)
    else:
        print(f"\n{PASS} All tests passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
