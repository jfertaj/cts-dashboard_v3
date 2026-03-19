#!/usr/bin/env python3
"""
STEP 2 — Test Moby answers against the ground truth fixture.

For each case in fixtures/moby_ground_truth.json:
  - Sends the natural language question to POST /api/ai/chat
  - Extracts account_ids from the returned table
  - Compares with expected_account_ids using precision / recall
  - For moby_only cases: runs structural checks (has_table, min_rows, etc.)
  - Reports pass / fail / warn per case with full diff on failure

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py --fixture fixtures/moby_ground_truth.json
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py --groups A B C   # run only groups A, B, C
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py --id B01 D03     # run specific cases
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py --tags stage1    # run by tag
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_questions.py --fast           # skip moby_only
  SF_SESSION_COOKIE="<value>" API_BASE="http://localhost:8000" python scripts/test_moby_questions.py

Exit code: 0 if all pass, 1 if any fail.
"""

import sys, os, json, time, ssl, argparse, re
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("API_BASE", "https://cts-innodia-dashboard.org").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
CHAT_TIMEOUT   = int(os.environ.get("CHAT_TIMEOUT", "180"))

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode    = ssl.CERT_NONE

# ANSI colours
GREEN  = "\033[92m"; RED    = "\033[91m"; YELLOW = "\033[93m"
BLUE   = "\033[94m"; GREY   = "\033[90m"; RESET  = "\033[0m"
BOLD   = "\033[1m"

PASS_ICON = f"{GREEN}✓{RESET}"
FAIL_ICON = f"{RED}✗{RESET}"
WARN_ICON = f"{YELLOW}?{RESET}"
SKIP_ICON = f"{GREY}-{RESET}"
INFO_ICON = f"{BLUE}ℹ{RESET}"

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
    except Exception as e:
        raise RuntimeError(str(e))

def call_moby(question: str, timeout: int = CHAT_TIMEOUT) -> Optional[Dict]:
    try:
        return _req("POST", "/api/ai/chat",
                    {"messages": [{"role": "user", "content": question}]},
                    timeout=timeout)
    except Exception as e:
        return {"_error": str(e)}

# ── Extractors ─────────────────────────────────────────────────────────────────
def extract_account_ids(resp: Dict) -> Set[str]:
    """Extract account_ids from Moby's table response."""
    rows = (resp.get("table") or {}).get("rows", [])
    return {str(r["account_id"]) for r in rows if r.get("account_id")}

def extract_answer_text(resp: Dict) -> str:
    return resp.get("answer", "")

def has_table(resp: Dict) -> bool:
    rows = (resp.get("table") or {}).get("rows", [])
    return len(rows) > 0

def row_count(resp: Dict) -> int:
    return len((resp.get("table") or {}).get("rows", []))

def extract_names(resp: Dict) -> List[str]:
    rows = (resp.get("table") or {}).get("rows", [])
    return [r.get("account_name","") for r in rows if r.get("account_name")]

# ── Evaluation ─────────────────────────────────────────────────────────────────
def evaluate_exact(
    moby_ids:     Set[str],
    expected_ids: Set[str],
    tolerance:    str,
    top_n:        Optional[int] = None,
) -> Tuple[bool, Dict]:
    """
    Returns (passed, metrics_dict).
    tolerance values:
      exact        — Moby set == expected set
      subset       — Moby ⊆ expected (all Moby results are valid)
      superset     — expected ⊆ Moby (Moby found at least all expected)
      min_count    — Moby returned at least 1 result that is in expected
      ordered_top_n — top_n of Moby's ordered results are in expected
    """
    expected = set(expected_ids)
    got      = set(moby_ids)
    tp       = got & expected
    fp       = got - expected   # Moby returned but not expected
    fn       = expected - got   # Expected but Moby missed

    precision = len(tp) / len(got)      if got      else (1.0 if not expected else 0.0)
    recall    = len(tp) / len(expected) if expected  else 1.0

    metrics = {
        "precision":  round(precision, 3),
        "recall":     round(recall, 3),
        "moby_count": len(got),
        "expected_count": len(expected),
        "tp": len(tp), "fp": len(fp), "fn": len(fn),
        "extra":   sorted(fp)[:5],   # first 5 unexpected
        "missing": sorted(fn)[:5],   # first 5 missed
    }

    if tolerance == "exact":
        passed = (got == expected)
    elif tolerance == "subset":
        passed = (len(fp) == 0)       # nothing outside expected
    elif tolerance == "superset":
        passed = (len(fn) == 0)       # nothing missing
    elif tolerance == "min_count":
        passed = (len(tp) >= 1)
    elif tolerance == "ordered_top_n":
        passed = (precision >= 0.8)   # ≥80% of returned are valid
    else:
        passed = (precision >= 0.8 and recall >= 0.8)

    return passed, metrics

def run_structural_checks(resp: Dict, checks: List[str]) -> Tuple[bool, List[str]]:
    """Run moby_only structural checks. Returns (all_passed, list_of_failure_msgs)."""
    failures = []
    answer   = extract_answer_text(resp)
    n_rows   = row_count(resp)

    for check in checks:
        if check == "has_answer":
            if not answer or len(answer.strip()) < 10:
                failures.append("has_answer: answer is empty or too short")
            elif any(w in answer.lower() for w in ("couldn't","sorry","i don't","no data","don't have")):
                failures.append(f"has_answer: Moby refused/failed: {answer[:80]}")
        elif check == "has_table":
            if n_rows == 0:
                failures.append("has_table: table is empty")
        elif check.startswith("min_rows_"):
            n = int(check.split("_")[-1])
            if n_rows < n:
                failures.append(f"min_rows_{n}: only {n_rows} rows returned")
        elif check == "mentions_country":
            # Check that any country name appears in the answer
            countries = ["germany","france","italy","spain","belgium","poland","uk","netherlands"]
            if not any(c in answer.lower() for c in countries):
                failures.append("mentions_country: no country name found in answer")

    return len(failures) == 0, failures

# ── Printer helpers ────────────────────────────────────────────────────────────
def _bar(label: str, value: float, width: int = 20) -> str:
    filled = int(value * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{label} [{bar}] {value:.0%}"

def print_case_result(
    case_id: str, desc: str, question: str, passed: bool, elapsed: float,
    category: str, metrics: Optional[Dict] = None, failures: List[str] = None,
    moby_names: List[str] = None, skipped: bool = False, error: str = None,
):
    icon = SKIP_ICON if skipped else (PASS_ICON if passed else FAIL_ICON)
    status = "SKIP" if skipped else ("PASS" if passed else "FAIL")
    colour = GREY if skipped else (GREEN if passed else RED)
    print(f"\n  {icon} {BOLD}[{case_id}]{RESET} {desc}")
    print(f"     Q: {GREY}\"{question[:90]}\"{RESET}")

    if error:
        print(f"     {RED}ERROR: {error[:120]}{RESET}")
        return

    if skipped:
        return

    print(f"     {colour}{status}{RESET}  ({elapsed:.1f}s)", end="")

    if metrics:
        p, r = metrics["precision"], metrics["recall"]
        got, exp = metrics["moby_count"], metrics["expected_count"]
        print(f"  |  Moby={got}  Expected={exp}  "
              f"P={GREEN if p>=0.9 else YELLOW if p>=0.7 else RED}{p:.0%}{RESET}  "
              f"R={GREEN if r>=0.9 else YELLOW if r>=0.7 else RED}{r:.0%}{RESET}")
        if not passed:
            if metrics["extra"]:
                print(f"     {YELLOW}Extra (unexpected):{RESET} {metrics['extra']}")
            if metrics["missing"]:
                print(f"     {YELLOW}Missing:{RESET}          {metrics['missing']}")
    else:
        print()

    if failures:
        for f in failures:
            print(f"     {RED}✗ {f}{RESET}")

    if moby_names and len(moby_names) <= 6:
        print(f"     {GREY}Returned: {moby_names}{RESET}")
    elif moby_names:
        print(f"     {GREY}Returned ({len(moby_names)}): {moby_names[:4]} …{RESET}")

# ══════════════════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", default="fixtures/moby_ground_truth.json")
    parser.add_argument("--groups", nargs="*", default=[],
                        help="Only run groups, e.g. --groups A B C")
    parser.add_argument("--id", nargs="*", dest="ids", default=[],
                        help="Run specific case IDs, e.g. --id B01 D03")
    parser.add_argument("--tags", nargs="*", default=[],
                        help="Run cases with any of these tags, e.g. --tags stage1 moby")
    parser.add_argument("--fast", action="store_true",
                        help="Skip moby_only cases (only run verifiable ones)")
    parser.add_argument("--precision-threshold", type=float, default=0.85,
                        help="Min precision to PASS (default 0.85)")
    parser.add_argument("--recall-threshold", type=float, default=0.85,
                        help="Min recall to PASS (default 0.85)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between Moby calls (default 2.0, be gentle)")
    args = parser.parse_args()

    P_THRESH = args.precision_threshold
    R_THRESH = args.recall_threshold

    if not SESSION_COOKIE:
        print(f"{FAIL_ICON}  SF_SESSION_COOKIE is not set. Export it first.")
        sys.exit(1)

    fixture_path = Path(__file__).parent.parent / args.fixture
    if not fixture_path.exists():
        print(f"{FAIL_ICON}  Fixture not found: {fixture_path}")
        print(f"  Run first: SF_SESSION_COOKIE=\"...\" python scripts/generate_ground_truth.py")
        sys.exit(1)

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    all_cases = fixture.get("cases", {})

    print(f"\n{'═'*65}")
    print(f"  {BOLD}CTS Dashboard — Moby Question Test Runner{RESET}")
    print(f"  API:     {BASE_URL}")
    print(f"  Fixture: {fixture_path.name}  (generated {fixture.get('generated_at','?')[:19]})")
    print(f"  Cases:   {len(all_cases)} total")
    print(f"  Thresholds: P≥{P_THRESH:.0%}  R≥{R_THRESH:.0%}")
    print(f"{'═'*65}")

    # ── Filter cases ───────────────────────────────────────────────────────────
    selected = list(all_cases.items())

    if args.groups:
        gs = {g.upper() for g in args.groups}
        selected = [(cid, c) for cid, c in selected if cid[0].upper() in gs]
        print(f"  Groups filter: {gs}  → {len(selected)} cases")

    if args.ids:
        id_set = set(args.ids)
        selected = [(cid, c) for cid, c in selected if cid in id_set]
        print(f"  ID filter: {id_set}  → {len(selected)} cases")

    if args.tags:
        tag_set = set(args.tags)
        selected = [(cid, c) for cid, c in selected
                    if tag_set & set(c.get("tags", []))]
        print(f"  Tag filter: {tag_set}  → {len(selected)} cases")

    if args.fast:
        selected = [(cid, c) for cid, c in selected if c.get("category") != "moby_only"]
        print(f"  Fast mode (no moby_only)  → {len(selected)} cases")

    if not selected:
        print(f"\n{WARN_ICON}  No cases selected. Check --groups/--id/--tags.")
        sys.exit(0)

    print(f"\n  Running {len(selected)} cases …")

    # ── Track results ──────────────────────────────────────────────────────────
    n_pass = n_fail = n_skip = n_error = 0
    failed_ids   = []
    results_log  = []

    current_group = None

    for cid, case_data in selected:
        # Print group header
        group_letter = cid[0]
        if group_letter != current_group:
            current_group = group_letter
            group_name = {
                "A": "Country Filters",
                "B": "SF Field Filters (metrics, status, pipeline)",
                "C": "Qualification Data",
                "D": "Activity / Assignment",
                "E": "Complex Multi-Filter",
                "F": "Geographic / Nearby",
                "G": "Members View",
                "H": "Moby Natural Language (structural)",
            }.get(group_letter, group_letter)
            print(f"\n{'─'*65}")
            print(f"  {BOLD}GROUP {group_letter} — {group_name}{RESET}")
            print(f"{'─'*65}")

        category = case_data.get("category", "explorer_filter")
        desc     = case_data.get("description", cid)
        question = case_data.get("question", "")

        # Skip cases that had generation errors
        if case_data.get("error") and category != "moby_only":
            print_case_result(cid, desc, question, False, 0,
                              category, error=f"Ground truth error: {case_data['error'][:80]}",
                              skipped=False)
            n_error += 1
            failed_ids.append(cid)
            results_log.append({"id": cid, "status": "error", "reason": "ground_truth_error"})
            continue

        # ── Call Moby ──────────────────────────────────────────────────────────
        t0 = time.time()
        resp = call_moby(question)
        elapsed = time.time() - t0

        if resp is None or resp.get("_error"):
            err = (resp or {}).get("_error","timeout")
            print_case_result(cid, desc, question, False, elapsed, category, error=err)
            n_error += 1
            failed_ids.append(cid)
            results_log.append({"id": cid, "status": "error", "reason": err[:80]})
            time.sleep(args.delay)
            continue

        moby_ids   = extract_account_ids(resp)
        moby_names = extract_names(resp)

        # ── Evaluate ───────────────────────────────────────────────────────────
        if category == "moby_only":
            checks  = case_data.get("checks", ["has_answer"])
            passed, failures = run_structural_checks(resp, checks)
            print_case_result(cid, desc, question, passed, elapsed, category,
                              failures=failures if not passed else [],
                              moby_names=moby_names)

        elif category in ("explorer_filter", "members"):
            expected = set(case_data.get("expected_account_ids", []))
            tolerance = case_data.get("tolerance", "exact")

            if not expected:
                # Expected 0 results — check Moby also returned 0
                passed  = (len(moby_ids) == 0)
                metrics = {"precision":1.0,"recall":1.0,"moby_count":len(moby_ids),
                           "expected_count":0,"tp":0,"fp":len(moby_ids),"fn":0,
                           "extra":sorted(moby_ids)[:5],"missing":[]}
            else:
                passed, metrics = evaluate_exact(moby_ids, expected, tolerance)
                # Apply P/R thresholds for non-exact tolerances
                if tolerance not in ("exact",) and metrics["expected_count"] > 0:
                    passed = (metrics["precision"] >= P_THRESH and
                              metrics["recall"]    >= R_THRESH)

            print_case_result(cid, desc, question, passed, elapsed, category,
                              metrics=metrics, moby_names=moby_names)

        elif category == "nearby":
            all_known = set(case_data.get("all_known_account_ids", []))
            tolerance = case_data.get("tolerance", "subset")
            top_n     = case_data.get("top_n")

            passed, metrics = evaluate_exact(moby_ids, all_known, tolerance, top_n=top_n)
            # For nearby: mainly check Moby returned something and it's all valid sites
            if len(moby_ids) == 0:
                passed = False
                metrics["fn"] = 1  # signal something was expected

            print_case_result(cid, desc, question, passed, elapsed, category,
                              metrics=metrics, moby_names=moby_names)

        else:
            passed = True
            print_case_result(cid, desc, question, True, elapsed, category,
                              skipped=True)
            n_skip += 1

        if category != "moby_only" or True:  # always track
            if passed:
                n_pass += 1
            else:
                n_fail += 1
                failed_ids.append(cid)

        results_log.append({
            "id": cid, "status": "pass" if passed else "fail",
            "elapsed": round(elapsed, 1),
            "moby_rows": row_count(resp),
        })

        time.sleep(args.delay)

    # ── Final summary ──────────────────────────────────────────────────────────
    total = n_pass + n_fail + n_error
    pct   = f"{n_pass/total:.0%}" if total > 0 else "—"

    print(f"\n{'═'*65}")
    print(f"  {BOLD}RESULTS{RESET}")
    print(f"{'═'*65}")
    print(f"  {GREEN}{BOLD}{n_pass} PASS{RESET}  |  "
          f"{RED}{BOLD}{n_fail} FAIL{RESET}  |  "
          f"{RED}{n_error} ERROR{RESET}  |  "
          f"{GREY}{n_skip} SKIP{RESET}  "
          f"({pct} pass rate)")

    if failed_ids:
        print(f"\n  Failed cases:")
        for fid in failed_ids:
            c = all_cases.get(fid, {})
            print(f"    {FAIL_ICON}  {fid}  —  {c.get('description','')}")
        print(f"\n  Re-run only failed cases:")
        print(f"    SF_SESSION_COOKIE=\"$SF_SESSION_COOKIE\" \\")
        print(f"    python scripts/test_moby_questions.py --id {' '.join(failed_ids)}")

    # ── Group summary ──────────────────────────────────────────────────────────
    group_stats: Dict[str, Dict] = {}
    for entry in results_log:
        g = entry["id"][0]
        if g not in group_stats:
            group_stats[g] = {"pass": 0, "fail": 0, "error": 0}
        group_stats[g][entry["status"] if entry["status"] in ("pass","fail","error") else "pass"] += 1

    if len(group_stats) > 1:
        print(f"\n  By group:")
        for g in sorted(group_stats):
            s = group_stats[g]
            tot = s["pass"] + s["fail"] + s["error"]
            icon = PASS_ICON if s["fail"]+s["error"] == 0 else FAIL_ICON
            print(f"    {icon} Group {g}:  {s['pass']}/{tot} pass"
                  + (f"  ({s['fail']} fail)" if s["fail"] else "")
                  + (f"  ({s['error']} error)" if s["error"] else ""))

    print(f"\n  Tips:")
    if n_fail > 0:
        print(f"  • Lower thresholds:  --precision-threshold 0.7 --recall-threshold 0.7")
        print(f"  • Re-run one group:  --groups H   (moby natural language)")
    print(f"  • Skip slow groups:  --fast   (skip moby_only)")
    print(f"  • Run one question:  --id B01")
    print(f"{'═'*65}\n")

    sys.exit(0 if n_fail == 0 and n_error == 0 else 1)


if __name__ == "__main__":
    main()
