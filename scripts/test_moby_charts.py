#!/usr/bin/env python3
"""
Moby AI — Comprehensive Chart / Visualization Tests.

Tests that Moby correctly returns `visualization` objects (bar, line, pie) in response
to chart-type questions, and that chart manipulation follow-ups (group, top-N, sort)
work correctly.

Covers:
  CHART-1  Deterministic: Activities × Countries stacked bar
  CHART-2  Deterministic: Sites per activity bar chart
  CHART-3  Deterministic: HLA typing per country bar chart
  CHART-4  LLM: Newly-Diagnosed T1D adults per country bar chart
  CHART-5  LLM: Stage 1 vs Stage 2 by country (multi-series)
  CHART-6  LLM: Pie chart of sites per country
  CHART-7  Chart manipulation: group small countries → 'Others'
  CHART-8  Chart manipulation: top-N rows
  CHART-9  Chart manipulation: sort by value descending
  CHART-10 Multi-turn: chart → "show me on the map" → 🎯 button reply
  CHART-11 Spanish: "gráfico de barras" query
  CHART-12 LLM: render_chart after explorer_search (ND ≥18 by country)
  CHART-13 Deterministic: activities stacked + follow-up "make it a stacked barchart"
  CHART-14 Table→chart: Moby gets table, user asks for chart on it

Usage:
  SF_SESSION_COOKIE="<value>" python scripts/test_moby_charts.py
  SF_SESSION_COOKIE="<val>" SKIP_SLOW=1 python scripts/test_moby_charts.py
"""

import sys, os, json, time, ssl, re, traceback
import urllib.request, urllib.error
from typing import Any, Dict, List, Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode   = ssl.CERT_NONE

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("API_BASE", "https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com").rstrip("/")
SESSION_COOKIE = os.environ.get("SF_SESSION_COOKIE", "")
SKIP_SLOW      = os.environ.get("SKIP_SLOW", "0") == "1"
CHAT_TIMEOUT   = int(os.environ.get("CHAT_TIMEOUT", "120"))

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


def chat(msg: str, last_filters=None, last_table=None,
         timeout: int = CHAT_TIMEOUT) -> Dict:
    """Call Moby chat API. Optionally pass last_table for chart manipulation follow-ups."""
    body: Dict[str, Any] = {
        "messages": [{"role": "user", "content": msg}],
    }
    if last_filters is not None:
        body["last_filters"] = last_filters
    if last_table is not None:
        body["last_table"] = last_table
    return _req("POST", "/api/ai/chat", body, timeout=timeout)


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

def moby(msg: str, last_filters=None, last_table=None,
         section_name: str = "") -> Optional[Dict]:
    """Call Moby; return response or None on timeout/error."""
    try:
        return chat(msg, last_filters=last_filters, last_table=last_table)
    except Exception as e:
        err = str(e)
        label = section_name or msg[:60]
        if "timed out" in err.lower() or "timeout" in err.lower():
            fail(f"{label} — Moby responded", "timed out")
        else:
            fail(f"{label} — Moby responded", err[:120])
        return None


# ── Visualization helpers ─────────────────────────────────────────────────────

def get_viz(resp: Dict) -> Optional[Dict]:
    return resp.get("visualization") if resp else None

def tbl_rows(resp: Dict) -> List[Dict]:
    return (resp.get("table") or {}).get("rows", []) if resp else []

def col_keys(resp: Dict) -> List[str]:
    return [c.get("key","") for c in (resp.get("table") or {}).get("columns", [])] if resp else []


def check_viz(resp: Optional[Dict], desc: str,
              expected_type: Optional[str] = None,
              xkey_options: Optional[List[str]] = None,
              ykey_patterns: Optional[List[str]] = None,
              min_data: int = 1) -> bool:
    """
    Assert that a Moby response contains a valid visualization object.
    Returns True if all mandatory checks pass.
    """
    if resp is None:
        fail(f"{desc}: response is None")
        return False

    viz = get_viz(resp)
    if viz is None:
        fail(f"{desc}: has visualization", f"keys={list(resp.keys())}")
        fail(f"{desc}: visualization key missing — answer={str(resp.get('answer',''))[:120]}")
        return False
    ok(f"{desc}: has visualization")

    ok_all = True

    # type
    vtype = viz.get("type")
    valid_types = {"bar", "line", "pie", "scatter"}
    chk(f"{desc}: viz.type is valid ({vtype})", vtype in valid_types,
        f"type={vtype}")
    if expected_type:
        chk(f"{desc}: viz.type == '{expected_type}'", vtype == expected_type,
            f"actual type={vtype}")
        if vtype != expected_type:
            ok_all = False

    # xKey
    xkey = viz.get("xKey")
    chk(f"{desc}: viz.xKey is set", bool(xkey), f"xKey={xkey}")
    if not xkey:
        ok_all = False
    if xkey_options and xkey:
        chk(f"{desc}: xKey in expected set", xkey in xkey_options,
            f"xKey={xkey}, expected={xkey_options}")

    # yKeys
    ykeys = viz.get("yKeys") or []
    chk(f"{desc}: viz.yKeys is non-empty", isinstance(ykeys, list) and len(ykeys) > 0,
        f"yKeys={ykeys}")
    if not ykeys:
        ok_all = False
    if ykey_patterns:
        for pat in ykey_patterns:
            found = any(pat.lower() in k.lower() for k in ykeys)
            chk(f"{desc}: yKeys contain '{pat}'", found, f"yKeys={ykeys[:6]}")

    # data
    data = viz.get("data") or []
    chk(f"{desc}: viz.data has ≥{min_data} row(s)", len(data) >= min_data,
        f"data_rows={len(data)}")
    if len(data) < min_data:
        ok_all = False

    # numeric values in first yKey
    if data and ykeys:
        first_ykey = ykeys[0]
        numeric_count = sum(
            1 for row in data[:10]
            if isinstance(row.get(first_ykey), (int, float))
        )
        chk(f"{desc}: viz.data has numeric values in '{first_ykey}'",
            numeric_count > 0,
            f"numeric_count={numeric_count}/{min(10, len(data))}")
        if numeric_count == 0:
            ok_all = False

    if ok_all:
        info(f"  viz OK → type={vtype}, xKey={xkey}, yKeys={ykeys[:4]}, data_rows={len(data)}")

    return ok_all


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-1  Deterministic: Activities × Countries stacked bar
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_1_activities_stacked():
    section("CHART-1 — Deterministic: Activities × Countries stacked bar")
    resp = moby(
        "Show me all clinical activities and how many sites per country each one has. "
        "I want to see the countries involved — give me a chart.",
        section_name="CHART-1"
    )
    chk("CHART-1 — Moby responded", resp is not None, "timed out")
    if not resp:
        return

    rows = tbl_rows(resp)
    chk("CHART-1: has table with ≥1 row", len(rows) >= 1, f"{len(rows)} rows")
    info(f"Table rows: {len(rows)}, cols: {col_keys(resp)[:6]}")

    viz_ok = check_viz(resp, "CHART-1",
                       expected_type="bar",
                       xkey_options=["activity_name"],
                       min_data=1)
    if viz_ok:
        viz = get_viz(resp)
        # yKeys should be country names (not numeric field names)
        ykeys = viz.get("yKeys") or []
        info(f"yKeys (countries): {ykeys[:8]}")
        chk("CHART-1: multiple yKeys (≥2 countries)",
            len(ykeys) >= 2,
            f"yKeys count={len(ykeys)}")

    # Save for follow-up tests
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-2  Deterministic: Sites per activity (simple bar)
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_2_sites_per_activity():
    section("CHART-2 — Deterministic: Sites per activity bar chart")
    resp = moby(
        "How many sites participate in each clinical activity? "
        "Show me a bar chart of the total number of sites per activity.",
        section_name="CHART-2"
    )
    chk("CHART-2 — Moby responded", resp is not None, "timed out")
    if not resp:
        return

    rows = tbl_rows(resp)
    chk("CHART-2: has table with ≥1 row", len(rows) >= 1, f"{len(rows)} rows")

    viz_ok = check_viz(resp, "CHART-2",
                       expected_type="bar",
                       xkey_options=["activity_name"],
                       ykey_patterns=["sites"],
                       min_data=1)
    if viz_ok:
        viz = get_viz(resp)
        data = viz.get("data") or []
        info(f"Activities in chart: {[d.get('activity_name','?') for d in data[:5]]}")

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-3  Deterministic: HLA typing per country bar chart
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_3_hla_by_country():
    section("CHART-3 — Deterministic: HLA typing per country bar chart")
    resp = moby(
        "What % of sites per country perform HLA typing? Show as a bar chart by country.",
        section_name="CHART-3"
    )
    chk("CHART-3 — Moby responded", resp is not None, "timed out")
    if not resp:
        return

    rows = tbl_rows(resp)
    chk("CHART-3: has table with ≥1 row", len(rows) >= 1, f"{len(rows)} rows")

    viz_ok = check_viz(resp, "CHART-3",
                       expected_type="bar",
                       xkey_options=["country"],
                       ykey_patterns=["hla"],
                       min_data=1)
    if viz_ok:
        viz = get_viz(resp)
        data = viz.get("data") or []
        info(f"Countries in HLA chart: {[d.get('country','?') for d in data[:6]]}")

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-4  LLM: ND T1D adults per country bar chart
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_4_nd_by_country():
    if SKIP_SLOW:
        skip("CHART-4: ND T1D adults per country bar chart (LLM)"); return None

    section("CHART-4 — LLM: Newly-Diagnosed T1D adults per country bar chart")
    resp = moby(
        "Show me a bar chart of the total number of newly diagnosed adult T1D patients "
        "(over 18, last year) per country. Sort by highest first.",
        section_name="CHART-4"
    )
    chk("CHART-4 — Moby responded", resp is not None, "timed out")
    if not resp:
        return None

    check_viz(resp, "CHART-4",
              expected_type="bar",
              xkey_options=["country", "ShippingCountry"],
              min_data=5)
    info(f"Answer: {str(resp.get('answer',''))[:150]}")
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-5  LLM: Stage 1 vs Stage 2 by country (multi-series bar)
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_5_stage12_multiseries():
    if SKIP_SLOW:
        skip("CHART-5: Stage 1 vs Stage 2 multi-series bar (LLM)"); return None

    section("CHART-5 — LLM: Stage 1 vs Stage 2 by country (multi-series)")
    resp = moby(
        "Create a grouped bar chart comparing the total Stage 1 individuals followed "
        "and Stage 2 individuals followed per country. Show both series side by side.",
        section_name="CHART-5"
    )
    chk("CHART-5 — Moby responded", resp is not None, "timed out")
    if not resp:
        return None

    viz_ok = check_viz(resp, "CHART-5",
                       expected_type="bar",
                       xkey_options=["country", "ShippingCountry"],
                       min_data=3)
    if viz_ok:
        viz = get_viz(resp)
        ykeys = viz.get("yKeys") or []
        chk("CHART-5: 2 yKeys (Stage 1 + Stage 2)", len(ykeys) >= 2,
            f"yKeys={ykeys}")
        has_stage1 = any("stage1" in k.lower() or "stage_1" in k.lower() or "stage 1" in k.lower() for k in ykeys)
        has_stage2 = any("stage2" in k.lower() or "stage_2" in k.lower() or "stage 2" in k.lower() for k in ykeys)
        chk("CHART-5: Stage 1 series present", has_stage1, f"yKeys={ykeys}")
        chk("CHART-5: Stage 2 series present", has_stage2, f"yKeys={ykeys}")

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-6  LLM: Pie chart of sites per country
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_6_pie_chart():
    if SKIP_SLOW:
        skip("CHART-6: Pie chart of sites per country (LLM)"); return None

    section("CHART-6 — LLM: Pie chart of sites per country")
    resp = moby(
        "Give me a pie chart showing how clinical sites are distributed across countries. "
        "Each slice should represent one country.",
        section_name="CHART-6"
    )
    chk("CHART-6 — Moby responded", resp is not None, "timed out")
    if not resp:
        return None

    viz_ok = check_viz(resp, "CHART-6",
                       expected_type="pie",
                       xkey_options=["country", "ShippingCountry"],
                       min_data=5)
    if viz_ok:
        viz = get_viz(resp)
        data = viz.get("data") or []
        # Pie charts should cover all countries (or at least the major ones)
        chk("CHART-6: ≥10 slices in pie", len(data) >= 10, f"slices={len(data)}")

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-7  Chart manipulation: group small values → 'Others'
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_7_group_others():
    section("CHART-7 — Chart manipulation: group small countries → 'Others'")

    # Step 1: Get a sites-per-country table/chart first
    info("Step 1: Getting sites per country as baseline…")
    resp1 = moby(
        "How many clinical sites are there per country? Give me a bar chart.",
        section_name="CHART-7 Step1"
    )
    chk("CHART-7 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    chk("CHART-7 Step1: has table", tbl1 is not None and len(rows1) >= 1,
        f"{len(rows1)} rows")
    info(f"Step1: {len(rows1)} rows, viz present: {get_viz(resp1) is not None}")

    if not tbl1 or len(rows1) < 1:
        info("Skipping Step 2 — no table from Step 1")
        return

    # Step 2: Group small countries into 'Others' using last_table
    info("Step 2: Requesting 'group countries with < 5 sites as Others'…")
    resp2 = moby(
        "Group countries with less than 5 sites into Others.",
        last_table=tbl1,
        section_name="CHART-7 Step2"
    )
    chk("CHART-7 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    viz_ok = check_viz(resp2, "CHART-7",
                       min_data=1)
    if viz_ok:
        viz = get_viz(resp2)
        data = viz.get("data") or []
        xkey = viz.get("xKey") or ""
        has_others = any(
            "other" in str(row.get(xkey, "")).lower()
            for row in data
        )
        chk("CHART-7: 'Others' group present in chart", has_others,
            f"labels={[row.get(xkey,'?') for row in data[:8]]}")
        chk("CHART-7: grouped chart has fewer rows than original",
            len(data) <= len(rows1),
            f"grouped={len(data)} vs original={len(rows1)}")
        info(f"Grouped chart: {len(data)} bars, Others present: {has_others}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-8  Chart manipulation: top-N
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_8_top_n():
    section("CHART-8 — Chart manipulation: top-5 countries by ND adults")

    info("Step 1: Getting ND adults by country…")
    resp1 = moby(
        "Show me the number of newly diagnosed adult T1D patients per country as a bar chart.",
        section_name="CHART-8 Step1"
    )
    chk("CHART-8 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    chk("CHART-8 Step1: has table", tbl1 is not None and len(rows1) >= 1,
        f"{len(rows1)} rows")
    info(f"Step1: {len(rows1)} rows")

    if not tbl1 or len(rows1) < 2:
        info("Skipping Step 2 — insufficient table from Step 1")
        return

    info("Step 2: Requesting top 5 only…")
    resp2 = moby(
        "Show only the top 5 countries.",
        last_table=tbl1,
        section_name="CHART-8 Step2"
    )
    chk("CHART-8 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    viz_ok = check_viz(resp2, "CHART-8", min_data=1)
    if viz_ok:
        viz = get_viz(resp2)
        data = viz.get("data") or []
        chk("CHART-8: chart has ≤6 rows after top-5 filter",
            len(data) <= 6,
            f"data rows={len(data)}")
        info(f"Top-N chart: {len(data)} bars")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-9  Chart manipulation: sort descending
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_9_sort():
    section("CHART-9 — Chart manipulation: sort chart by value descending")

    info("Step 1: Getting sites-per-country table (unsorted)…")
    resp1 = moby(
        "How many clinical sites are there per country? Give me a bar chart.",
        section_name="CHART-9 Step1"
    )
    chk("CHART-9 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    viz1 = get_viz(resp1)
    chk("CHART-9 Step1: has chart", viz1 is not None, "visualization missing")
    info(f"Step1: {len(rows1)} rows, viz: {viz1 is not None}")

    if not tbl1 or len(rows1) < 2:
        info("Skipping Step 2 — insufficient data")
        return

    info("Step 2: Requesting sort descending…")
    resp2 = moby(
        "Sort descending by number of sites.",
        last_table=tbl1,
        section_name="CHART-9 Step2"
    )
    chk("CHART-9 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    viz_ok = check_viz(resp2, "CHART-9", min_data=1)
    if viz_ok:
        viz = get_viz(resp2)
        data = viz.get("data") or []
        ykeys = viz.get("yKeys") or []
        if data and ykeys and len(data) >= 2:
            y0 = data[0].get(ykeys[0]) or 0
            y1 = data[-1].get(ykeys[0]) or 0
            try:
                chk("CHART-9: first value ≥ last value (sorted desc)",
                    float(y0) >= float(y1),
                    f"first={y0}, last={y1}")
            except (TypeError, ValueError):
                warn("CHART-9: couldn't compare sort order numerically",
                     f"y0={y0} y1={y1}")
        info(f"Sorted chart: {len(data)} bars")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-10 Multi-turn: chart → "show me on the map" → 🎯 button reply
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_10_show_on_map():
    if SKIP_SLOW:
        skip("CHART-10: chart → 'show on map' reply (LLM)"); return

    section("CHART-10 — Multi-turn: after chart, ask to show on map")

    info("Step 1: Getting a chart of sites in Italy…")
    resp1 = moby(
        "Show me all clinical sites in Italy as a table.",
        section_name="CHART-10 Step1"
    )
    chk("CHART-10 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    lf1 = resp1.get("last_filters")
    chk("CHART-10 Step1: has table", tbl1 is not None and len(rows1) >= 1,
        f"{len(rows1)} rows")
    info(f"Step1: {len(rows1)} Italian sites")

    info("Step 2: Asking to show on the map…")
    resp2 = moby(
        "Show me those on the map.",
        last_filters=lf1,
        last_table=tbl1,
        section_name="CHART-10 Step2"
    )
    chk("CHART-10 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    answer2 = str(resp2.get("answer", "")).lower()
    chk("CHART-10: answer mentions Explorer or 🎯 button",
        any(kw in answer2 for kw in ["explorer", "🎯", "open in explorer", "filter"]),
        f"answer={answer2[:200]}")
    chk("CHART-10: answer does NOT try to render a map itself",
        "leaflet" not in answer2 and "<map" not in answer2,
        f"answer={answer2[:100]}")
    info(f"Map reply: {answer2[:200]}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-11 Spanish: "gráfico de barras" de sitios por país
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_11_spanish_bar():
    section("CHART-11 — Spanish: 'gráfico de barras' de sitios por país")
    resp = moby(
        "Dame un gráfico de barras con el número de sitios clínicos por país.",
        section_name="CHART-11"
    )
    chk("CHART-11 — Moby responded", resp is not None, "timed out")
    if not resp:
        return

    check_viz(resp, "CHART-11",
              expected_type="bar",
              xkey_options=["country", "ShippingCountry"],
              min_data=5)
    info(f"Answer: {str(resp.get('answer',''))[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-12 LLM: render_chart after explorer_search (ND ≥18 by country)
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_12_nd_explorer_chart():
    if SKIP_SLOW:
        skip("CHART-12: explorer_search + render_chart for ND (LLM)"); return

    section("CHART-12 — LLM: chart of ND ≥18 adults (explorer_search + render_chart)")
    resp = moby(
        "Search for all sites where newly diagnosed adult T1D patients (over 18) > 0 "
        "and then give me a bar chart of those values by country. "
        "Use the explorer search tool.",
        section_name="CHART-12"
    )
    chk("CHART-12 — Moby responded", resp is not None, "timed out")
    if not resp:
        return

    check_viz(resp, "CHART-12",
              expected_type="bar",
              min_data=3)
    rows = tbl_rows(resp)
    chk("CHART-12: has underlying table", len(rows) >= 1, f"{len(rows)} rows")
    info(f"Answer: {str(resp.get('answer',''))[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-13 Deterministic: activities stacked + "make stacked barchart" follow-up
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_13_stacked_followup():
    section("CHART-13 — Deterministic: activity table then stacked bar follow-up")

    info("Step 1: Get activity enrollment table (no chart)…")
    resp1 = moby(
        "Show me all clinical activities and how many sites each country has enrolled. "
        "Just give me the table.",
        section_name="CHART-13 Step1"
    )
    chk("CHART-13 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    chk("CHART-13 Step1: has table", tbl1 is not None and len(rows1) >= 1,
        f"{len(rows1)} rows")
    info(f"Step1 table: {len(rows1)} rows")

    if not tbl1 or len(rows1) < 1:
        info("Skipping Step 2 — no table")
        return

    info("Step 2: Request stacked bar chart by activity name…")
    resp2 = moby(
        "Make it a stacked bar chart by activity name.",
        last_table=tbl1,
        section_name="CHART-13 Step2"
    )
    chk("CHART-13 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    viz_ok = check_viz(resp2, "CHART-13",
                       expected_type="bar",
                       xkey_options=["activity_name"],
                       min_data=1)
    if viz_ok:
        viz = get_viz(resp2)
        ykeys = viz.get("yKeys") or []
        chk("CHART-13: multiple series (≥2 countries)", len(ykeys) >= 2,
            f"yKeys={ykeys[:6]}")
        info(f"Stacked chart: {len(ykeys)} country series")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-14 Table→chart: query returns table, user asks for chart
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_14_table_to_chart():
    if SKIP_SLOW:
        skip("CHART-14: table → 'now show as a line chart' (LLM)"); return

    section("CHART-14 — LLM: table first, then 'show as a bar chart'")

    info("Step 1: Get Stage 1 and Stage 2 by country as a plain table…")
    resp1 = moby(
        "Show me the total Stage 1 and Stage 2 individuals followed per country. "
        "Give me just the table, no chart.",
        section_name="CHART-14 Step1"
    )
    chk("CHART-14 Step1 — Moby responded", resp1 is not None, "timed out")
    if not resp1:
        return

    tbl1 = resp1.get("table")
    rows1 = tbl_rows(resp1)
    chk("CHART-14 Step1: has table", tbl1 is not None and len(rows1) >= 1,
        f"{len(rows1)} rows")
    info(f"Step1: {len(rows1)} rows, cols={col_keys(resp1)[:6]}")

    if not tbl1 or len(rows1) < 1:
        info("Skipping Step 2 — no table")
        return

    info("Step 2: Request bar chart from that table…")
    resp2 = moby(
        "Now show that as a grouped bar chart.",
        last_table=tbl1,
        section_name="CHART-14 Step2"
    )
    chk("CHART-14 Step2 — Moby responded", resp2 is not None, "timed out")
    if not resp2:
        return

    check_viz(resp2, "CHART-14",
              expected_type="bar",
              xkey_options=["country", "ShippingCountry"],
              min_data=3)
    info(f"Step2 answer: {str(resp2.get('answer',''))[:150]}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHART-15 Edge case: empty result → no viz crash
# ═══════════════════════════════════════════════════════════════════════════════

def test_chart_15_edge_cases():
    section("CHART-15 — Edge cases: graceful handling")

    # 15a: Chart of a country with no data (Monaco)
    info("15a: Chart for Monaco (likely 0 sites) — should not crash")
    resp_a = moby(
        "Show me a bar chart of clinical sites in Monaco.",
        section_name="CHART-15a"
    )
    chk("CHART-15a: Moby responded (no crash)", resp_a is not None, "timed out")
    if resp_a:
        answer_a = str(resp_a.get("answer", "")).lower()
        chk("CHART-15a: gives meaningful answer",
            bool(answer_a) and len(answer_a) > 10,
            f"answer={answer_a[:100]}")
        info(f"Monaco answer: {answer_a[:150]}")
        # Either 0 rows or graceful message — both acceptable
        rows_a = tbl_rows(resp_a)
        info(f"Monaco rows: {len(rows_a)}")

    # 15b: Ambiguous chart type — "visualize" without specifying bar/line/pie
    info("15b: 'Visualize' without chart type specified")
    resp_b = moby(
        "Visualize the number of Stage 2 individuals per country.",
        section_name="CHART-15b"
    )
    chk("CHART-15b: Moby responded", resp_b is not None, "timed out")
    if resp_b:
        answer_b = str(resp_b.get("answer", ""))
        chk("CHART-15b: has answer", bool(answer_b))
        viz_b = get_viz(resp_b)
        info(f"viz returned: {viz_b is not None}, type={viz_b.get('type') if viz_b else 'N/A'}")
        # Soft check — Moby should at least return some chart OR a table
        rows_b = tbl_rows(resp_b)
        chk("CHART-15b: has chart or table",
            viz_b is not None or len(rows_b) >= 1,
            f"viz={viz_b is not None}, rows={len(rows_b)}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    global passed, errors, warnings
    t0 = time.time()

    print(f"\n{'='*65}")
    print("  Moby AI — Chart / Visualization Integration Tests")
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

    tests = [
        ("CHART-1  Activities × Countries stacked bar",  test_chart_1_activities_stacked),
        ("CHART-2  Sites per activity bar",              test_chart_2_sites_per_activity),
        ("CHART-3  HLA typing per country bar",          test_chart_3_hla_by_country),
        ("CHART-4  ND T1D adults per country [LLM]",     test_chart_4_nd_by_country),
        ("CHART-5  Stage 1 vs Stage 2 multi-series [LLM]", test_chart_5_stage12_multiseries),
        ("CHART-6  Pie chart sites per country [LLM]",   test_chart_6_pie_chart),
        ("CHART-7  Group small countries → Others",      test_chart_7_group_others),
        ("CHART-8  Top-N chart manipulation",            test_chart_8_top_n),
        ("CHART-9  Sort chart descending",               test_chart_9_sort),
        ("CHART-10 Chart → 'show on map' [LLM]",         test_chart_10_show_on_map),
        ("CHART-11 Spanish: gráfico de barras",          test_chart_11_spanish_bar),
        ("CHART-12 explorer_search + render_chart [LLM]", test_chart_12_nd_explorer_chart),
        ("CHART-13 Activity stacked bar follow-up",      test_chart_13_stacked_followup),
        ("CHART-14 Table → bar chart [LLM]",             test_chart_14_table_to_chart),
        ("CHART-15 Edge cases / graceful handling",      test_chart_15_edge_cases),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as e:
            fail(f"Test '{name}' crashed", str(e))
            traceback.print_exc()
        time.sleep(1)

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
