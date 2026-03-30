#!/usr/bin/env python3
"""
Battery of tests for Moby activity-name filtering (handler 12d + related).

Usage:
    SF_SESSION_COOKIE="..." API_BASE="http://localhost:8000" python scripts/test_activity_filter.py

Tests cover:
  - Single activity name lookups
  - AND combinations (intersection)
  - OR combinations (union)
  - Three+ activity names
  - Different phrasings (have/with/having)
  - Country scoping
  - Non-existent activity names
  - Existing handlers that should NOT be broken (list, summary, by-country)
"""

import json
import os
import sys
import time
import requests

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
COOKIE = os.environ.get("SF_SESSION_COOKIE", "")

if not COOKIE:
    print("ERROR: Set SF_SESSION_COOKIE env var")
    sys.exit(1)


def ask(question: str) -> dict:
    r = requests.post(
        f"{API_BASE}/api/ai/chat",
        json={"messages": [{"role": "user", "content": question}]},
        headers={"Cookie": f"sf_session={COOKIE}"},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def row_count(resp: dict) -> int:
    return len((resp.get("table") or {}).get("rows") or [])


def has_column(resp: dict, key: str) -> bool:
    cols = (resp.get("table") or {}).get("columns") or []
    return any(c.get("key") == key for c in cols)


def all_rows_have(resp: dict, key: str) -> bool:
    rows = (resp.get("table") or {}).get("rows") or []
    if not rows:
        return False
    return all(r.get(key) for r in rows)


def all_matched_contain(resp: dict, *names: str) -> bool:
    """Every row's matched_activities contains ALL given name substrings."""
    rows = (resp.get("table") or {}).get("rows") or []
    if not rows:
        return False
    for r in rows:
        ma = (r.get("matched_activities") or "").lower()
        for n in names:
            if n.lower() not in ma:
                return False
    return True


def any_matched_contain(resp: dict, name: str) -> bool:
    """At least one row's matched_activities contains the name."""
    rows = (resp.get("table") or {}).get("rows") or []
    return any(name.lower() in (r.get("matched_activities") or "").lower() for r in rows)


def all_countries_equal(resp: dict, country: str) -> bool:
    rows = (resp.get("table") or {}).get("rows") or []
    if not rows:
        return False
    return all(
        (r.get("country") or "").lower() == country.lower()
        for r in rows
    )


# ──────────────────────────────────────────────────────────────
# Test definitions
# ──────────────────────────────────────────────────────────────

TESTS = []


def test(name, question, check_fn, description=""):
    TESTS.append((name, question, check_fn, description))


# ── 1. Single activity names ──────────────────────────────────

test(
    "SINGLE_DETECT",
    "show me sites that have DETECT activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "detect"),
    "Single: DETECT should return sites with DETECT in matched_activities",
)

test(
    "SINGLE_SAFEGUARD",
    "show me sites that have Safeguard activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "safeguard"),
    "Single: Safeguard",
)

test(
    "SINGLE_BARICADE",
    "show me sites that have Baricade activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "baricade"),
    "Single: Baricade",
)

test(
    "SINGLE_FABULINUS",
    "show me sites that have Fabulinus activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "fabulinus"),
    "Single: Fabulinus",
)

test(
    "SINGLE_BETA_PRESERVE",
    "show me sites that have Beta Preserve activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "beta preserve"),
    "Single: Beta Preserve (two-word name)",
)

# ── 2. AND combinations (intersection) ───────────────────────

test(
    "AND_DETECT_SAFEGUARD",
    "show me sites that have DETECT and SAFEGUARD activities",
    lambda r: row_count(r) > 0 and row_count(r) < 50 and all_matched_contain(r, "detect", "safeguard"),
    "AND: DETECT ∩ SAFEGUARD — should be small intersection",
)

test(
    "AND_BARICADE_FABULINUS",
    "show me sites that have Baricade and Fabulinus activities",
    lambda r: row_count(r) > 0 and row_count(r) < 50 and all_matched_contain(r, "baricade", "fabulinus"),
    "AND: Baricade ∩ Fabulinus",
)

test(
    "AND_DETECT_FABULINUS",
    "show me sites that have DETECT and Fabulinus activities",
    lambda r: row_count(r) >= 0 and (row_count(r) == 0 or all_matched_contain(r, "detect", "fabulinus")),
    "AND: DETECT ∩ Fabulinus",
)

# ── 3. OR combinations (union) ────────────────────────────────

test(
    "OR_DETECT_SAFEGUARD",
    "show me sites that have DETECT or SAFEGUARD activities",
    lambda r: row_count(r) > 50,
    "OR: DETECT ∪ SAFEGUARD — union should be larger than either alone",
)

test(
    "OR_BARICADE_FABULINUS",
    "show me sites that have Baricade or Fabulinus activities",
    lambda r: row_count(r) >= 42,  # Baricade alone is 42
    "OR: Baricade ∪ Fabulinus — at least as many as Baricade alone",
)

# ── 4. Three activity names ──────────────────────────────────

test(
    "AND_THREE",
    "show me sites that have DETECT and SAFEGUARD and Fabulinus activities",
    lambda r: row_count(r) >= 0 and (row_count(r) == 0 or all_matched_contain(r, "detect", "safeguard", "fabulinus")),
    "AND of 3: DETECT ∩ SAFEGUARD ∩ Fabulinus — may be very small or zero",
)

test(
    "OR_THREE",
    "show me sites that have DETECT or Baricade or Fabulinus activities",
    lambda r: row_count(r) > 44,  # Fabulinus alone is 44
    "OR of 3: should be larger than any single one",
)

# ── 5. Different phrasings ────────────────────────────────────

test(
    "PHRASING_WITH",
    "sites with DETECT and SAFEGUARD activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "detect", "safeguard"),
    "Phrasing: 'with' instead of 'have'",
)

test(
    "PHRASING_HAVING",
    "which sites having Baricade activities",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "baricade"),
    "Phrasing: 'having'",
)

test(
    "PHRASING_HAVE_QUESTION",
    "which sites have Fabulinus activities?",
    lambda r: row_count(r) > 0 and all_matched_contain(r, "fabulinus"),
    "Phrasing: question mark at end",
)

# ── 6. Country scoping ───────────────────────────────────────

test(
    "COUNTRY_DETECT_ITALY",
    "show me sites that have DETECT activities in Italy",
    lambda r: row_count(r) > 0 and all_countries_equal(r, "Italy") and all_matched_contain(r, "detect"),
    "Country: DETECT in Italy only",
)

test(
    "COUNTRY_BARICADE_GERMANY",
    "show me sites that have Baricade activities in Germany",
    lambda r: row_count(r) > 0 and all_countries_equal(r, "Germany") and all_matched_contain(r, "baricade"),
    "Country: Baricade in Germany only",
)

# ── 7. Non-existent activity ─────────────────────────────────

test(
    "NONEXISTENT",
    "show me sites that have XYZNONEXISTENT activities",
    lambda r: row_count(r) == 0,
    "Non-existent activity name → 0 rows",
)

test(
    "AND_REAL_PLUS_FAKE",
    "show me sites that have DETECT and XYZNONEXISTENT activities",
    lambda r: row_count(r) == 0,
    "AND with non-existent → 0 rows (intersection with empty set)",
)

# ── 8. Consistency checks ────────────────────────────────────

test(
    "AND_SUBSET_OF_SINGLE",
    None,  # special: will be handled in runner
    None,
    "AND result ⊆ each single result (DETECT∩SAFEGUARD ≤ min(DETECT, SAFEGUARD))",
)

test(
    "OR_SUPERSET_OF_SINGLE",
    None,
    None,
    "OR result ≥ max of each single result",
)

# ── 9. Existing handlers NOT broken ──────────────────────────

test(
    "LIST_ACTIVITIES",
    "list all activities",
    lambda r: row_count(r) > 0 and has_column(r, "activity_name"),
    "Existing handler: list all activities still works",
)

test(
    "ACTIVITIES_BY_COUNTRY",
    "activities by country",
    lambda r: row_count(r) > 0,
    "Existing handler: activities by country still works",
)

test(
    "ACTIVITY_SUMMARY",
    "activity summary",
    lambda r: row_count(r) > 0,
    "Existing handler: activity summary still works",
)

test(
    "QUOTED_ACTIVITY",
    'sites in the "DETECT Pilot Sites" activity',
    lambda r: row_count(r) > 0,
    "Existing handler: quoted activity name still works",
)


# ──────────────────────────────────────────────────────────────
# Runner
# ──────────────────────────────────────────────────────────────

def main():
    passed = 0
    failed = 0
    errors = 0
    results_cache: dict = {}

    print(f"\n{'='*70}")
    print(f"  Moby Activity Filter Test Battery")
    print(f"  API: {API_BASE}")
    print(f"{'='*70}\n")

    for name, question, check_fn, desc in TESTS:
        # ── Special consistency checks ──
        if name == "AND_SUBSET_OF_SINGLE":
            # Uses cached results
            and_count = results_cache.get("AND_DETECT_SAFEGUARD", -1)
            detect_count = results_cache.get("SINGLE_DETECT", -1)
            safeguard_count = results_cache.get("SINGLE_SAFEGUARD", -1)
            if and_count < 0 or detect_count < 0 or safeguard_count < 0:
                print(f"  SKIP  {name:40s} — prerequisite tests missing")
                continue
            ok = and_count <= min(detect_count, safeguard_count)
            status = "PASS" if ok else "FAIL"
            detail = f"AND={and_count} ≤ min(DETECT={detect_count}, SAFEGUARD={safeguard_count})"
            if ok:
                passed += 1
                print(f"  \033[32mPASS\033[0m  {name:40s} {detail}")
            else:
                failed += 1
                print(f"  \033[31mFAIL\033[0m  {name:40s} {detail}")
            continue

        if name == "OR_SUPERSET_OF_SINGLE":
            or_count = results_cache.get("OR_DETECT_SAFEGUARD", -1)
            detect_count = results_cache.get("SINGLE_DETECT", -1)
            safeguard_count = results_cache.get("SINGLE_SAFEGUARD", -1)
            if or_count < 0 or detect_count < 0 or safeguard_count < 0:
                print(f"  SKIP  {name:40s} — prerequisite tests missing")
                continue
            ok = or_count >= max(detect_count, safeguard_count)
            detail = f"OR={or_count} ≥ max(DETECT={detect_count}, SAFEGUARD={safeguard_count})"
            if ok:
                passed += 1
                print(f"  \033[32mPASS\033[0m  {name:40s} {detail}")
            else:
                failed += 1
                print(f"  \033[31mFAIL\033[0m  {name:40s} {detail}")
            continue

        # ── Normal tests ──
        try:
            t0 = time.time()
            resp = ask(question)
            elapsed = time.time() - t0
            n = row_count(resp)
            results_cache[name] = n
            ok = check_fn(resp)
            if ok:
                passed += 1
                print(f"  \033[32mPASS\033[0m  {name:40s} rows={n:4d}  ({elapsed:.1f}s)")
            else:
                failed += 1
                answer = resp.get("answer", "")[:120]
                print(f"  \033[31mFAIL\033[0m  {name:40s} rows={n:4d}  ({elapsed:.1f}s)")
                print(f"        {desc}")
                print(f"        answer: {answer}")
        except Exception as e:
            errors += 1
            print(f"  \033[33mERROR\033[0m {name:40s} {e}")

    print(f"\n{'='*70}")
    total = passed + failed + errors
    print(f"  Results: {passed}/{total} passed, {failed} failed, {errors} errors")
    print(f"{'='*70}\n")

    sys.exit(0 if failed == 0 and errors == 0 else 1)


if __name__ == "__main__":
    main()
