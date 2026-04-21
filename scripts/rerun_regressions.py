#!/usr/bin/env python3
"""Re-run the 10 regressions identified in the 2026-04-21 bulk eval.

Reads the original baseline YAML for the question text and expected row count,
hits the local backend, writes results to docs/moby-regression-rerun.json,
and prints a per-question PASS/FAIL classification so triage iterations are
easy to compare.

Usage:
    SF_SESSION_COOKIE="..." python scripts/rerun_regressions.py
"""
from __future__ import annotations
import json, os, pathlib, re, sys, time, urllib.request

import yaml

REGRESSION_IDS = [
    "SC01",  # CTS sites in Germany, Italy, Belgium — expected ~79, got 0, "I wasn't able to answer"
    "SC06",  # France + HLA typing — expected ~9, got 0 rows (text had 9 but no table)
    "SC09",  # NOT completed profiling — expected ~187, got 0
    "SC10",  # autoantibody testing — expected ~30, got 1 (over-strict filter)
    "CL05",  # CTS sites in Romania or Bulgaria — expected ~1, got 0
    "SS01",  # profiling form uploaded to DB — expected ~91, got 0
    "SS03",  # C_Profiling_Complete is true — expected ~155, got 0 (self-diagnosed date-type mismatch but did not re-query)
    "SS04",  # CTS-validated but not in assignment — expected ~56, got 0
    "RE01",  # table of Stage 1/2 by country — got "No previous table available" (math handler fired without table)
    "FA01",  # 20 sites overnight + pharmacy — expected ~34, got 0 ("I need one more detail")
]

API_BASE   = os.getenv("API_BASE", "http://localhost:8000")
COOKIE_VAL = os.getenv("SF_SESSION_COOKIE", "")
ROOT       = pathlib.Path(__file__).resolve().parents[1]
BASELINE   = ROOT / "docs" / "moby-bulk-eval-initial.yaml"
OUT        = ROOT / "docs" / "moby-regression-rerun.json"

_ROW_RE = re.compile(r"(\d+)\s*(?:row|site|countr|activit|institution|coordinator)", re.I)


def ask(question: str) -> dict | None:
    payload = json.dumps({"messages": [{"role": "user", "content": question}]}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/api/ai/chat",
        data=payload,
        headers={"Content-Type": "application/json", "Cookie": f"sf_session={COOKIE_VAL}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def extract(result) -> dict:
    if result is None:
        return {"status": "ERROR", "error": "response body was null", "rows": 0, "has_table": False, "answer_preview": ""}
    if not isinstance(result, dict):
        return {"status": "ERROR", "error": f"unexpected type {type(result).__name__}", "rows": 0, "has_table": False, "answer_preview": ""}
    if "error" in result:
        return {"status": "ERROR", "error": result["error"][:200], "rows": 0, "has_table": False, "answer_preview": ""}
    ans = result.get("answer", "") or ""
    ans_text = re.sub(r"<[^>]+>", "", ans).strip()[:300]
    tbl = result.get("table") or {}
    rows = tbl.get("rows") or []
    return {
        "status": "OK",
        "rows": len(rows),
        "has_table": bool(rows),
        "answer_preview": ans_text,
    }


def classify(expected_rows: int | None, info: dict) -> tuple[str, str]:
    """Return (verdict, reason). PASS if we got a comparable answer."""
    if info["status"] != "OK":
        return "FAIL", f"status={info['status']}; {info.get('error','')[:120]}"
    prev = info.get("answer_preview", "")
    if "Your credit" in prev:
        return "FAIL", "credit exhausted"
    actual = int(info.get("rows") or 0)
    if expected_rows is None:
        # unknown target — accept as PASS if we got a table
        return ("PASS" if info["has_table"] else "FAIL",
                "no baseline count; got table" if info["has_table"] else "no baseline count; no table")
    if actual == 0 and expected_rows > 0:
        return "FAIL", f"expected ~{expected_rows} rows, got 0 | {prev[:150]!r}"
    if actual * 10 < expected_rows:
        return "FAIL", f"expected ~{expected_rows}, got {actual} (>1 OOM drop)"
    return "PASS", f"expected ~{expected_rows}, got {actual}"


def main() -> int:
    if not COOKIE_VAL:
        print("ERROR: set SF_SESSION_COOKIE", file=sys.stderr)
        return 2

    base_map = {e["id"]: e for e in (yaml.safe_load(open(BASELINE)) or {}).get("entries", [])}
    missing = [qid for qid in REGRESSION_IDS if qid not in base_map]
    if missing:
        print(f"ERROR: missing baseline entries for {missing}", file=sys.stderr)
        return 2

    results, n_pass, n_fail = [], 0, 0
    for i, qid in enumerate(REGRESSION_IDS, 1):
        b = base_map[qid]
        question = b["question"]
        expected_m = _ROW_RE.search(b.get("observed_behavior", "") or "")
        expected = int(expected_m.group(1)) if expected_m else None

        print(f"[{i}/{len(REGRESSION_IDS)}] {qid}: {question[:70]}...", flush=True)
        t0 = time.time()
        info = extract(ask(question))
        dt = time.time() - t0
        verdict, reason = classify(expected, info)
        print(f"  {verdict} ({dt:.1f}s) — {reason}", flush=True)

        results.append({
            "id": qid,
            "question": question,
            "expected_rows": expected,
            "elapsed_s": round(dt, 1),
            "verdict": verdict,
            "reason": reason,
            **info,
        })
        n_pass += int(verdict == "PASS")
        n_fail += int(verdict == "FAIL")

    OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n{n_pass}/{len(REGRESSION_IDS)} PASS — results in {OUT}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
