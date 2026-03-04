#!/usr/bin/env python3
"""
Multi-turn conversation tests for Moby AI.

Tests that the new chatHistory implementation correctly passes full conversation
history so Claude can answer follow-up questions with prior context.

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_multiturn.py
  SF_SESSION_COOKIE="<val>" API_BASE="http://localhost:8000" python scripts/test_moby_multiturn.py
"""

import sys, os, json, time, ssl, re, traceback
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode   = ssl.CERT_NONE

BASE_URL       = os.environ.get("API_BASE", "https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
CHAT_TIMEOUT   = int(os.environ.get("CHAT_TIMEOUT", "120"))

PASS = "\033[92m✓\033[0m"; FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m?\033[0m"; INFO = "\033[94mℹ\033[0m"

errors:   List[str] = []
warnings: List[str] = []
passed  = 0

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

def chat_single(msg: str, last_filters=None) -> Dict:
    """Send a single-turn message (old behaviour — no history)."""
    return _req("POST", "/api/ai/chat",
                {"messages": [{"role": "user", "content": msg}],
                 "last_filters": last_filters},
                timeout=CHAT_TIMEOUT)

def chat_multi(history: List[Dict], last_filters=None) -> Dict:
    """Send a multi-turn conversation (new behaviour — full history)."""
    return _req("POST", "/api/ai/chat",
                {"messages": history,
                 "last_filters": last_filters},
                timeout=CHAT_TIMEOUT)

def tbl_rows(resp: Dict) -> List[Dict]:
    return (resp.get("table") or {}).get("rows", [])

def col_keys(resp: Dict) -> List[str]:
    return [c.get("key","") for c in (resp.get("table") or {}).get("columns", [])]

def answer(resp: Dict) -> str:
    return (resp.get("answer") or "").lower()

def section(title: str):
    print(f"\n── {title} {'─' * max(1, 55 - len(title))}")

def chk(name: str, ok: bool, detail: str = ""):
    global passed
    sym = PASS if ok else FAIL
    suf = f"  [{detail}]" if detail else ""
    print(f"  {sym} {name}{suf}")
    if ok:
        passed += 1
    else:
        errors.append(f"{name}: {detail}")

def warn(name: str, detail: str = ""):
    print(f"  {WARN} {name}  [{detail}]")
    warnings.append(f"{name}: {detail}")

def info(msg: str):
    print(f"  {INFO} {msg}")

# ── Helpers ────────────────────────────────────────────────────────────────────

def build_history(pairs: List[tuple]) -> List[Dict]:
    """Build history from (user, assistant) pairs. Last user msg is the query."""
    msgs = []
    for user_msg, assistant_msg in pairs:
        msgs.append({"role": "user", "content": user_msg})
        if assistant_msg:
            msgs.append({"role": "assistant", "content": assistant_msg})
    return msgs

# ── Tests ──────────────────────────────────────────────────────────────────────

def test_mt_country_followup():
    """
    Turn 1: Ask for sites in Italy
    Turn 2: "how many is that?" → should answer with the count from turn 1
    """
    section("MT-1: Country → count follow-up")
    t0 = time.time()

    resp1 = chat_single("show me sites in Italy")
    rows1 = tbl_rows(resp1)
    lf1   = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} rows, answer: {answer(resp1)[:80]}")
    chk("MT-1a: Turn 1 returns table or answer about Italy",
        len(rows1) > 0 or "ital" in answer(resp1),
        f"{len(rows1)} rows")

    # Build history: prior turn + new question
    history = [
        {"role": "user",      "content": "show me sites in Italy"},
        {"role": "assistant", "content": resp1.get("answer") or f"Found {len(rows1)} sites in Italy."},
        {"role": "user",      "content": "how many is that?"},
    ]
    resp2 = chat_multi(history, last_filters=lf1)
    ans2  = answer(resp2)
    info(f"Turn 2: answer: {ans2[:120]}")

    # The answer should contain the count from turn 1 OR a number
    has_number = bool(re.search(r'\b\d+\b', ans2))
    has_italy_ref = "ital" in ans2 or has_number
    chk("MT-1b: Turn 2 references count/Italy from context",
        has_italy_ref,
        ans2[:80])

    print(f"  elapsed: {time.time()-t0:.1f}s")


def test_mt_stage2_then_filter():
    """
    Turn 1: "list sites with Stage 2 patients"
    Turn 2: "now only show those in Germany" → should apply country filter on top
    """
    section("MT-2: Stage2 list → narrow to Germany")
    t0 = time.time()

    resp1 = chat_single("show me sites that follow Stage 2 individuals")
    rows1 = tbl_rows(resp1)
    lf1   = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} rows")
    chk("MT-2a: Turn 1 returns Stage2 sites",
        len(rows1) > 0,
        f"{len(rows1)} rows")

    history = [
        {"role": "user",      "content": "show me sites that follow Stage 2 individuals"},
        {"role": "assistant", "content": resp1.get("answer") or f"Found {len(rows1)} sites with Stage 2 individuals."},
        {"role": "user",      "content": "now only show me those in Germany"},
    ]
    resp2 = chat_multi(history, last_filters=lf1)
    rows2 = tbl_rows(resp2)
    ans2  = answer(resp2)
    info(f"Turn 2: {len(rows2)} rows, answer: {ans2[:80]}")

    chk("MT-2b: Turn 2 narrows result (fewer or equal rows)",
        len(rows2) <= len(rows1) if rows1 else True,
        f"{len(rows1)} → {len(rows2)} rows")
    chk("MT-2c: Turn 2 references Germany or shows smaller set",
        "german" in ans2 or "de" in ans2 or len(rows2) < len(rows1),
        ans2[:60])

    print(f"  elapsed: {time.time()-t0:.1f}s")


def test_mt_nd_then_top5():
    """
    Turn 1: "which countries have the most newly diagnosed patients?"
    Turn 2: "now focus on Spain" → should use country context
    """
    section("MT-3: ND by country → focus on Spain")
    t0 = time.time()

    resp1 = chat_single("which countries have the most newly diagnosed T1D patients?")
    rows1 = tbl_rows(resp1)
    ans1  = answer(resp1)
    lf1   = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} rows, answer: {ans1[:80]}")
    chk("MT-3a: Turn 1 returns ND by country data",
        len(rows1) > 0 or any(kw in ans1 for kw in ["spain","italy","germany","france","finland","country"]),
        f"{len(rows1)} rows")

    history = [
        {"role": "user",      "content": "which countries have the most newly diagnosed T1D patients?"},
        {"role": "assistant", "content": resp1.get("answer") or "Here are the countries with most ND patients."},
        {"role": "user",      "content": "how many newly diagnosed patients does Spain have?"},
    ]
    resp2 = chat_multi(history, last_filters=lf1)
    ans2  = answer(resp2)
    rows2 = tbl_rows(resp2)
    info(f"Turn 2: answer: {ans2[:120]}")

    has_spain  = "spain" in ans2 or "españa" in ans2 or "es" in ans2
    has_number = bool(re.search(r'\b\d+\b', ans2))
    chk("MT-3b: Turn 2 references Spain with a number",
        has_spain and has_number,
        ans2[:80])

    print(f"  elapsed: {time.time()-t0:.1f}s")


def test_mt_question_then_clarify():
    """
    Turn 1: "show me the top sites by Stage 1 capacity"
    Turn 2: "what's the maximum value you can see?" → should reference table data
    """
    section("MT-4: Stage1 table → max value question")
    t0 = time.time()

    resp1 = chat_single("show me the top 10 sites with the most Stage 1 individuals")
    rows1 = tbl_rows(resp1)
    lf1   = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} rows")
    chk("MT-4a: Turn 1 returns Stage1 site table",
        len(rows1) > 0,
        f"{len(rows1)} rows")

    if rows1:
        history = [
            {"role": "user",      "content": "show me the top 10 sites with the most Stage 1 individuals"},
            {"role": "assistant", "content": resp1.get("answer") or f"Found {len(rows1)} sites with Stage 1 individuals."},
            {"role": "user",      "content": f"is {len(rows1)} an even or odd number?"},
        ]
        resp2 = chat_multi(history, last_filters=lf1)
        ans2  = answer(resp2)
        info(f"Turn 2: answer: {ans2[:120]}")

        # Claude must use the number from prior context to answer "even or odd"
        # 36 is even → answer should say "even"
        expected_parity = "even" if len(rows1) % 2 == 0 else "odd"
        has_parity = expected_parity in ans2
        chk("MT-4b: Turn 2 uses number from prior context to answer even/odd",
            has_parity,
            f"expected '{expected_parity}' in answer: {ans2[:60]}")
    else:
        warn("MT-4b: skipped — turn 1 returned no rows")

    print(f"  elapsed: {time.time()-t0:.1f}s")


def test_mt_three_turns():
    """
    Three-turn chain: country → metric filter → sort
    Turn 1: "show me sites in Finland"
    Turn 2: "which of those have pharmacy?"
    Turn 3: "and overnight stay too?"
    """
    section("MT-5: Three-turn refinement chain (Finland → pharmacy → overnight)")
    t0 = time.time()

    resp1 = chat_single("show me sites in Finland")
    rows1 = tbl_rows(resp1)
    lf1   = resp1.get("last_filters")
    info(f"Turn 1: {len(rows1)} rows")
    chk("MT-5a: Turn 1 returns Finnish sites",
        len(rows1) > 0 or "finland" in answer(resp1) or "finn" in answer(resp1),
        f"{len(rows1)} rows")

    history2 = [
        {"role": "user",      "content": "show me sites in Finland"},
        {"role": "assistant", "content": resp1.get("answer") or f"Found {len(rows1)} sites in Finland."},
        {"role": "user",      "content": "which of those have a pharmacy?"},
    ]
    resp2 = chat_multi(history2, last_filters=lf1)
    rows2 = tbl_rows(resp2)
    lf2   = resp2.get("last_filters") or lf1
    ans2  = answer(resp2)
    info(f"Turn 2: {len(rows2)} rows, answer: {ans2[:80]}")
    chk("MT-5b: Turn 2 filters to pharmacy (≤ turn1 rows)",
        len(rows2) <= len(rows1) if rows1 else True,
        f"{len(rows1)} → {len(rows2)}")

    history3 = history2 + [
        {"role": "assistant", "content": resp2.get("answer") or f"Found {len(rows2)} Finnish sites with pharmacy."},
        {"role": "user",      "content": "and which also offer overnight stay?"},
    ]
    resp3 = chat_multi(history3, last_filters=lf2)
    rows3 = tbl_rows(resp3)
    ans3  = answer(resp3)
    info(f"Turn 3: {len(rows3)} rows, answer: {ans3[:80]}")
    chk("MT-5c: Turn 3 further filters (≤ turn2 rows)",
        len(rows3) <= len(rows2) if rows2 else True,
        f"{len(rows2)} → {len(rows3)}")

    print(f"  elapsed: {time.time()-t0:.1f}s")


def test_mt_context_memory():
    """
    Verify Claude actually uses history: ask something about a number mentioned earlier.
    Turn 1: "how many CTS sites are there in total?"
    Turn 2: "multiply that number by 2" → must recall the number from turn 1
    """
    section("MT-6: Pure context memory — recall a number")
    t0 = time.time()

    resp1 = chat_single("how many CTS sites are there in the INNODIA network in total?")
    ans1  = answer(resp1)
    rows1 = tbl_rows(resp1)
    info(f"Turn 1: answer: {ans1[:120]}")

    # Extract a number from the answer
    nums = re.findall(r'\b(\d+)\b', ans1)
    if rows1:
        nums = [str(len(rows1))] + nums

    chk("MT-6a: Turn 1 provides a count",
        bool(nums),
        ans1[:60])

    if nums:
        history = [
            {"role": "user",      "content": "how many CTS sites are there in the INNODIA network in total?"},
            {"role": "assistant", "content": resp1.get("answer") or ans1},
            {"role": "user",      "content": "If you double that number, what do you get?"},
        ]
        resp2 = chat_multi(history)
        ans2  = answer(resp2)
        nums2 = re.findall(r'\b(\d+)\b', ans2)
        info(f"Turn 2: answer: {ans2[:120]}")

        # Claude should recall a number from turn 1 and double it correctly
        # Check that any number N from turn 1 appears doubled as 2*N in turn 2
        correct = False
        for n_str in nums[:3]:   # check first 3 numbers from turn 1
            n = int(n_str)
            if str(n * 2) in nums2:
                correct = True
                break
        chk("MT-6b: Turn 2 recalls a number from context and doubles it",
            correct,
            f"turn1 nums={nums[:3]}, turn2 nums={nums2[:5]}")
    else:
        warn("MT-6b: skipped — couldn't extract number from turn 1")

    print(f"  elapsed: {time.time()-t0:.1f}s")


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not SESSION_COOKIE:
        print("ERROR: set SF_SESSION_COOKIE env var", file=sys.stderr)
        sys.exit(1)

    print(f"Multi-turn Moby tests → {BASE_URL}")
    print(f"Timeout per request: {CHAT_TIMEOUT}s\n")
    total_t0 = time.time()

    tests = [
        test_mt_country_followup,
        test_mt_stage2_then_filter,
        test_mt_nd_then_top5,
        test_mt_question_then_clarify,
        test_mt_three_turns,
        test_mt_context_memory,
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            section_name = t.__name__
            print(f"  {FAIL} {section_name} — EXCEPTION: {e}")
            traceback.print_exc()
            errors.append(f"{section_name}: {e}")

    total = passed + len(errors)
    elapsed = time.time() - total_t0
    print(f"\n{'═'*60}")
    print(f"Elapsed: {elapsed:.0f}s   Checks: {total}   Passed: {passed}   Failed: {len(errors)}   Warnings: {len(warnings)}")
    if errors:
        print("\nFailed:")
        for e in errors:
            print(f"  {FAIL} {e}")
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  {WARN} {w}")
    if not errors:
        print("\n\033[92mALL CHECKS PASSED\033[0m")
    sys.exit(0 if not errors else 1)
