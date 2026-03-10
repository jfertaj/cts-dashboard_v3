#!/usr/bin/env python3
"""
Integration test: combined filters spanning site.* + qual.* (+ sf.*) categories.

Verifies that AND/OR logic works correctly across filter categories
(site.country, qual.* fields, sf.* profile/account fields).

NOTE on row structure:
  Each row from /api/explorer/search has the shape:
    { account_id, account_name, country, city, ..., data: { "sf.StageName": ..., "qual.xxx": ... } }
  Top-level: account_id, account_name, country, city (always present)
  In row["data"]: sf.*, qual.*, site.* values (keyed by full column key)

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_combined_filter.py
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/test_combined_filter.py
"""

import sys, os, json
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter
import urllib.request, urllib.error

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_URL       = os.environ.get("API_BASE", "https://cts-innodia-dashboard.org").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m?\033[0m"
INFO = "\033[94mℹ\033[0m"
errors: List[str] = []

# ─── HTTP helper ──────────────────────────────────────────────────────────────

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
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())

def search(filters: Dict, columns: List[str]) -> List[Dict]:
    resp = _req("POST", "/api/explorer/search", {"filters": filters, "columns": columns})
    return resp.get("rows", [])

# ─── Row accessors ────────────────────────────────────────────────────────────

def get_country(row: Dict) -> str:
    """Country is at the top-level of the row."""
    return (row.get("country") or "").strip().upper()

def get_data(row: Dict, key: str) -> Optional[str]:
    """All sf.* / qual.* / site.* values are inside row['data']."""
    return row.get("data", {}).get(key)

def get_data_str(row: Dict, key: str) -> str:
    v = get_data(row, key)
    return str(v).strip() if v is not None else ""

# ─── Assertion helper ─────────────────────────────────────────────────────────

def check(desc: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    msg = f"  {icon}  {desc}"
    if detail:
        msg += f"  [{detail}]"
    print(msg)
    if not ok:
        errors.append(f"{desc}" + (f": {detail}" if detail else ""))

# ─── Discover best qual field ─────────────────────────────────────────────────

def pick_qual_field(qual_keys: List[str], rows: List[Dict]) -> Optional[Tuple[str, str, int]]:
    """
    Returns (qual_key, value, count) for the qual field whose most-common
    non-null value appears in the most rows (at least 3).
    """
    best_field, best_val, best_cnt = None, None, 0
    for qk in qual_keys:
        counts: Counter = Counter()
        for row in rows:
            v = get_data_str(row, qk)
            if v and v.lower() not in ("none", "null", ""):
                counts[v] += 1
        if counts:
            top_val, top_cnt = counts.most_common(1)[0]
            if top_cnt > best_cnt:
                best_field, best_val, best_cnt = qk, top_val, top_cnt

    return (best_field, best_val, best_cnt) if best_field and best_cnt >= 3 else None

# ─── Main test runner ─────────────────────────────────────────────────────────

def run():
    print(f"\n{'='*60}")
    print("  Combined filter integration tests")
    print(f"  API: {BASE_URL}")
    print(f"{'='*60}\n")

    if not SESSION_COOKIE:
        print(f"  {FAIL}  SF_SESSION_COOKIE env var not set.")
        print("  Get it from browser DevTools → Application → Cookies → sf_session")
        sys.exit(1)

    # ── Discover fields ────────────────────────────────────────────────────
    print("── Step 1: Discover available fields ──────────────────────────")
    try:
        fields_resp = _req("GET", "/api/explorer/fields")
    except urllib.error.HTTPError as e:
        print(f"  {FAIL}  /fields HTTP {e.code}: {e.reason}")
        sys.exit(1)

    field_list = fields_resp if isinstance(fields_resp, list) else fields_resp.get("fields", [])
    qual_keys = [f["key"] for f in field_list if f.get("key", "").startswith("qual.")]
    sf_keys   = [f["key"] for f in field_list if f.get("key", "").startswith("sf.")]

    print(f"  {INFO}  {len(field_list)} total fields: 2 site.*, {len(qual_keys)} qual.*, {len(sf_keys)} sf.*")
    if qual_keys:
        print(f"  {INFO}  Sample qual keys: {qual_keys[:3]}")

    # ── Baseline: unfiltered ───────────────────────────────────────────────
    print("\n── Step 2: Baseline (no filters) ──────────────────────────────")
    disc_qual_cols = qual_keys[:20]  # use 20 qual fields to find one with data
    base_cols = ["site.country", "site.city", "sf.StageName"] + disc_qual_cols

    try:
        base_rows = search({"logic": "AND", "rules": []}, base_cols)
    except urllib.error.HTTPError as e:
        print(f"  {FAIL}  Baseline search HTTP {e.code}")
        sys.exit(1)

    total = len(base_rows)
    check("Baseline returns at least 1 site", total >= 1, f"{total} rows")

    # Pick most-common country
    country_counts: Counter = Counter(get_country(r) for r in base_rows if get_country(r))
    if not country_counts:
        print(f"  {WARN}  No country data — aborting")
        sys.exit(1)
    test_country, ctr_total = country_counts.most_common(1)[0]
    print(f"  {INFO}  {total} total sites — testing with most common country: {test_country} ({ctr_total} sites)")

    # Pick most-common StageName value
    stage_counts: Counter = Counter(
        get_data_str(r, "sf.StageName") for r in base_rows
        if get_data_str(r, "sf.StageName")
    )
    test_stage = stage_counts.most_common(1)[0][0] if stage_counts else "Closed Won"
    print(f"  {INFO}  Most common StageName: {test_stage!r} ({stage_counts.get(test_stage,0)} sites)")

    # ── Test A: site.country alone (sanity) ───────────────────────────────
    print("\n── Test A: site.country filter (sanity) ────────────────────────")
    ctr_rows = search(
        {"logic": "AND", "rules": [{"field": "site.country", "operator": "equals", "value": test_country}]},
        ["site.country", "site.city"],
    )
    ctr_count = len(ctr_rows)
    wrong_ctr = [r for r in ctr_rows if get_country(r) != test_country]
    check(f"All {ctr_count} rows have country={test_country}", len(wrong_ctr) == 0,
          f"{len(wrong_ctr)} with wrong country")
    check(f"Country filter reduces total ({total} → {ctr_count})", ctr_count < total)

    # ── Discover qual field ────────────────────────────────────────────────
    print("\n── Step 3: Discover qual field with real data ──────────────────")
    qual_pick = pick_qual_field(disc_qual_cols, base_rows) if qual_keys else None

    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print(f"  {INFO}  Selected: {q_key}")
        print(f"  {INFO}  Value to test: {q_val!r}  ({q_cnt}/{total} sites have this value)")
    else:
        print(f"  {WARN}  No qual field with ≥3 non-null values found in first {len(disc_qual_cols)} qual fields")
        # Try harder: fetch with more qual fields
        if len(qual_keys) > 20:
            print(f"  {INFO}  Retrying with {min(60, len(qual_keys))} qual fields...")
            more_cols = ["site.country", "site.city"] + qual_keys[:60]
            more_rows = search({"logic": "AND", "rules": []}, more_cols)
            qual_pick = pick_qual_field(qual_keys[:60], more_rows)
            if qual_pick:
                q_key, q_val, q_cnt = qual_pick
                base_rows = more_rows  # update reference
                print(f"  {INFO}  Found: {q_key} = {q_val!r} ({q_cnt} sites)")

    # ── Test B: qual.* alone ──────────────────────────────────────────────
    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print("\n── Test B: qual.* filter alone ─────────────────────────────────")
        qual_rows = search(
            {"logic": "AND", "rules": [{"field": q_key, "operator": "equals", "value": q_val}]},
            ["site.country", "site.city", q_key],
        )
        q_count = len(qual_rows)
        wrong_qual = [r for r in qual_rows if get_data_str(r, q_key) != q_val]
        check(f"All {q_count} rows have {q_key} = {q_val!r}",
              len(wrong_qual) == 0, f"{len(wrong_qual)} incorrect")
        check(f"qual filter reduces total ({total} → {q_count})", q_count <= total)

    # ── Test C: AND(site.country, qual.*) ─────────────────────────────────
    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print("\n── Test C: AND( site.country, qual.* ) ─────────────────────────")
        and_rows = search(
            {"logic": "AND", "rules": [
                {"field": "site.country", "operator": "equals", "value": test_country},
                {"field": q_key,          "operator": "equals", "value": q_val},
            ]},
            ["site.country", "site.city", q_key],
        )
        and_count = len(and_rows)
        wrong_ctr  = [r for r in and_rows if get_country(r) != test_country]
        wrong_qual = [r for r in and_rows if get_data_str(r, q_key) != q_val]
        check(f"AND: all {and_count} rows have country={test_country}",
              len(wrong_ctr) == 0, f"{len(wrong_ctr)} wrong country")
        check(f"AND: all {and_count} rows have {q_key}={q_val!r}",
              len(wrong_qual) == 0, f"{len(wrong_qual)} wrong qual")
        check(f"AND: count ≤ both individual filters ({and_count} ≤ min({ctr_count},{q_count}))",
              and_count <= min(ctr_count, q_count))
        print(f"  {INFO}  country={test_country}: {ctr_count}, qual={q_val!r}: {q_count}, AND: {and_count}")

    # ── Test D: OR(site.country, qual.*) ──────────────────────────────────
    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print("\n── Test D: OR( site.country, qual.* ) ──────────────────────────")
        or_rows = search(
            {"logic": "OR", "rules": [
                {"field": "site.country", "operator": "equals", "value": test_country},
                {"field": q_key,          "operator": "equals", "value": q_val},
            ]},
            ["site.country", "site.city", q_key],
        )
        or_count = len(or_rows)
        neither = [
            r for r in or_rows
            if get_country(r) != test_country and get_data_str(r, q_key) != q_val
        ]
        check("OR: every row satisfies at least one condition",
              len(neither) == 0, f"{len(neither)} satisfy neither")
        check(f"OR: count ≥ max of individual filters ({or_count} ≥ max({ctr_count},{q_count}))",
              or_count >= max(ctr_count, q_count))
        print(f"  {INFO}  country={test_country}: {ctr_count}, qual={q_val!r}: {q_count}, OR: {or_count}")

    # ── Test E: AND(site.country, sf.StageName) ───────────────────────────
    print("\n── Test E: AND( site.country, sf.StageName ) ───────────────────")
    stage_rows = search(
        {"logic": "AND", "rules": [
            {"field": "site.country", "operator": "equals", "value": test_country},
            {"field": "sf.StageName", "operator": "equals", "value": test_stage},
        ]},
        ["site.country", "site.city", "sf.StageName"],
    )
    sc = len(stage_rows)
    # StageName is in row["data"]["sf.StageName"]
    stage_country_only = sum(1 for r in base_rows
                             if get_data_str(r, "sf.StageName") == test_stage)
    wrong_ctr_e   = [r for r in stage_rows if get_country(r) != test_country]
    wrong_stage_e = [r for r in stage_rows if get_data_str(r, "sf.StageName") != test_stage]
    check(f"AND: all {sc} rows have country={test_country}",
          len(wrong_ctr_e) == 0, f"{len(wrong_ctr_e)} wrong country")
    check(f"AND: all {sc} rows have StageName={test_stage!r}",
          len(wrong_stage_e) == 0, f"{len(wrong_stage_e)} wrong stage")
    check(f"AND: count ({sc}) ≤ country count ({ctr_count})", sc <= ctr_count)
    print(f"  {INFO}  {ctr_count} are {test_country}, {stage_country_only} have stage={test_stage!r}, AND gives {sc}")

    # ── Test F: AND( site.country, sf.StageName, qual.* ) ─────────────────
    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print("\n── Test F: AND( site.country, sf.StageName, qual.* ) ───────────")
        triple_rows = search(
            {"logic": "AND", "rules": [
                {"field": "site.country", "operator": "equals", "value": test_country},
                {"field": "sf.StageName", "operator": "equals", "value": test_stage},
                {"field": q_key,          "operator": "equals", "value": q_val},
            ]},
            ["site.country", "site.city", "sf.StageName", q_key],
        )
        tc = len(triple_rows)
        wrong_3ctr   = [r for r in triple_rows if get_country(r) != test_country]
        wrong_3stage = [r for r in triple_rows if get_data_str(r, "sf.StageName") != test_stage]
        wrong_3qual  = [r for r in triple_rows if get_data_str(r, q_key) != q_val]
        check(f"Triple AND: all {tc} rows correct country", len(wrong_3ctr) == 0,
              f"{len(wrong_3ctr)} wrong")
        check(f"Triple AND: all {tc} rows correct stage", len(wrong_3stage) == 0,
              f"{len(wrong_3stage)} wrong")
        check(f"Triple AND: all {tc} rows correct qual", len(wrong_3qual) == 0,
              f"{len(wrong_3qual)} wrong")
        check(f"Triple AND: count ({tc}) ≤ pairwise AND ({sc})", tc <= sc)
        print(f"  {INFO}  country+stage: {sc}, adding qual filter: {tc}")

    # ── Test G: is_not_empty on a qual field ──────────────────────────────
    if qual_pick:
        q_key, q_val, q_cnt = qual_pick
        print("\n── Test G: qual.* is_not_empty / is_empty partition ────────────")
        ne_rows = search(
            {"logic": "AND", "rules": [{"field": q_key, "operator": "is_not_empty", "value": None}]},
            ["site.country", q_key],
        )
        em_rows = search(
            {"logic": "AND", "rules": [{"field": q_key, "operator": "is_empty",     "value": None}]},
            ["site.country", q_key],
        )
        ne_count = len(ne_rows)
        em_count = len(em_rows)
        check(f"is_not_empty ({ne_count}) + is_empty ({em_count}) = total ({total})",
              ne_count + em_count == total,
              f"sum={ne_count+em_count}, total={total}")
        wrong_ne = [r for r in ne_rows if not get_data_str(r, q_key)]
        check(f"is_not_empty: all {ne_count} rows have non-empty value",
              len(wrong_ne) == 0, f"{len(wrong_ne)} empty")
        wrong_em = [r for r in em_rows if get_data_str(r, q_key)]
        check(f"is_empty: all {em_count} rows have empty value",
              len(wrong_em) == 0, f"{len(wrong_em)} non-empty")
        print(f"  {INFO}  {ne_count} sites have {q_key} filled, {em_count} have it empty")

    # ── Test H: sf.Assignment.Name filter (was HTTP 400 bug) ─────────────
    print("\n── Test H: sf.Assignment.Name filter (no HTTP 400) ─────────────")
    # Any assignment name that exists in the org
    ASSIGNMENT_NAMES = ["Beta Preserve", "INNODIA", "preserve", "study"]
    assign_rows = None
    used_name = None
    for _aname in ASSIGNMENT_NAMES:
        try:
            _r = search(
                {"logic": "AND", "rules": [
                    {"field": "sf.Assignment.Name", "operator": "contains", "value": _aname},
                ]},
                ["sf.Assignment.Name", "extra.AssignmentsNames", "site.country"],
            )
            # Accept even 0 rows — the key check is no HTTP 400
            assign_rows = _r
            used_name = _aname
            break
        except Exception as _e:
            # If this specific name returns 0 results try next, but HTTP 400 means test fails
            if "400" in str(_e) or "400" in str(getattr(_e, "code", "")):
                check(f"sf.Assignment.Name filter: no HTTP 400 for '{_aname}'", False,
                      f"Got: {_e}")
                used_name = _aname
                break

    if used_name and assign_rows is not None:
        check(f"sf.Assignment.Name contains '{used_name}': no HTTP 400 (got {len(assign_rows)} rows)",
              True)
        # Verify rows that have assignment data actually contain the name
        rows_with_assign = [r for r in assign_rows
                            if used_name.lower() in str(r.get("data", {}).get("extra.AssignmentsNames", "")).lower()]
        if assign_rows:
            check(f"  All returned rows contain '{used_name}' in AssignmentsNames",
                  len(rows_with_assign) == len(assign_rows),
                  f"{len(rows_with_assign)}/{len(assign_rows)} match")
        print(f"  {INFO}  Assignment filter '{used_name}': {len(assign_rows)} sites")

    # ── Test I: sf.Assignment.Name equals operator (converts to contains) ─
    print("\n── Test I: sf.Assignment.Name with 'equals' operator ───────────")
    try:
        eq_rows = search(
            {"logic": "AND", "rules": [
                {"field": "sf.Assignment.Name", "operator": "equals", "value": "Beta Preserve"},
            ]},
            ["extra.AssignmentsNames", "site.country"],
        )
        check("sf.Assignment.Name equals 'Beta Preserve': no HTTP 400", True)
        print(f"  {INFO}  Assignment equals filter: {len(eq_rows)} sites")
    except Exception as _e2:
        check("sf.Assignment.Name equals 'Beta Preserve': no HTTP 400", False, str(_e2)[:120])

    # ─── Summary ─────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    if errors:
        print(f"\033[91m  FAILED — {len(errors)} error(s):\033[0m")
        for e in errors:
            print(f"  • {e}")
        sys.exit(1)
    else:
        print(f"\033[92m  ALL PASSED\033[0m")
    print(f"{'='*60}")

if __name__ == "__main__":
    run()
