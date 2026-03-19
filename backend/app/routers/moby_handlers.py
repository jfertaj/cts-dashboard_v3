"""
Moby deterministic handlers — extracted from _try_planner in ai_chat.py.

Each handler receives a HandlerContext (shared state) and an ActivityTools
container (injected callables). This avoids circular imports while keeping
handlers testable: pass mock tools in tests.

Handler priority order (same as _try_planner):
  1. handle_followup_activity_sponsor  — sponsor follow-up (pre-activity-intent)
  2. handle_assignment_sites           — "sites in/belonging to X assignment"
  3. handle_activity                   — all activity/enrollment queries

All functions return Optional[dict] — None means "I didn't handle this".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ── Shared context object ──────────────────────────────────────────────────────

@dataclass
class HandlerContext:
    """All inputs a handler may need — built once per _try_planner call."""
    query: str              # original user text (original case, for regex that need it)
    s: str                  # query.lower() — most handlers use this
    sf: Any                 # Salesforce session (None when logged out)
    last_table: Optional[Dict[str, Any]]   # payload.last_table
    last_filters: Optional[Dict[str, Any]] # _try_planner parameter
    has_table: bool
    is_followup_prefix: bool


# ── Injected tools (dependency injection — no circular imports) ────────────────

@dataclass
class ActivityTools:
    """Callable tools injected from ai_chat.py to avoid circular imports."""
    tool_sites_by_activity: Callable
    tool_sites_with_any_activity: Callable
    tool_list_all_activities: Callable
    tool_activity_country_matrix: Callable
    tool_activity_counts_by_country: Callable
    tool_activities_with_countries: Callable
    tool_activities_with_assignments_counts: Callable
    tool_activity_assignments_detailed: Callable
    tool_render_chart: Callable
    pretty_label: Callable          # _pretty_label from ai_chat.py
    dbg: Callable = field(default=lambda *a, **kw: None)  # _dbg (no-op default)


# ── Handler 1: Follow-up sponsor swap ─────────────────────────────────────────

def handle_followup_activity_sponsor(
    ctx: HandlerContext,
    tools: ActivityTools,
) -> Optional[Dict[str, Any]]:
    """
    Fires BEFORE the activity-intent check.
    When the previous table was an activities table (has 'activity_name' column)
    and the user says something like "now for Sanofi" or "what about Lilly",
    this resolves the sponsor name and returns the activity list.

    Mirrors lines 5279–5302 of ai_chat.py.
    """
    if not ctx.sf:
        return None

    _prev_cols_act = [
        c.get("key") if isinstance(c, dict) else str(c)
        for c in (ctx.last_table or {}).get("columns", [])
    ]
    if "activity_name" not in _prev_cols_act:
        return None

    _m_fu = (
        re.search(r"\bfor\s+([\w\s\-&']{2,40}?)(?:\s*\?|$|[.,;])", ctx.s, re.I) or
        re.search(r"\bnow\s+(?:for\s+|with\s+)?([\w\s\-&']{2,40}?)(?:\s*\?|$|[.,;])", ctx.s, re.I) or
        re.search(r"\bwhat\s+about\s+([\w\s\-&']{2,40}?)(?:\s*\?|$|[.,;])", ctx.s, re.I) or
        re.search(r"\bones?\s+(?:from|of|by)\s+([\w\s\-&']{2,40}?)(?:\s*\?|$|[.,;])", ctx.s, re.I)
    )
    if not _m_fu:
        return None

    _cand_fu = _m_fu.group(1).strip().rstrip(".,;?")
    _stop_fu = {"the", "a", "an", "all", "any", "some", "this", "that",
                "them", "it", "these", "those", "here", "there"}
    if _cand_fu.lower() in _stop_fu or len(_cand_fu) < 2:
        return None

    table = tools.tool_list_all_activities(ctx.sf, account_name_like=_cand_fu)
    rows = table.get("rows") or []
    ans = (
        f"Found <strong>{len(rows)}</strong> "
        f"activit{'y' if len(rows) == 1 else 'ies'} "
        f"where the sponsor / account matches <em>{_cand_fu}</em>."
    )
    return {"answer": f"<p>{ans}</p>", "table": table}


# ── Handler 2: Assignment sites ────────────────────────────────────────────────

def handle_assignment_sites(
    ctx: HandlerContext,
    tools: ActivityTools,
) -> Optional[Dict[str, Any]]:
    """
    Handles "show sites that belong to [NAME] assignment" queries.

    Fires when "assignment" is present, no km mention, and a "sites/show" token
    is present — BEFORE the _act_intent check so it works even without "activit".

    Mirrors lines 5304–5337 of ai_chat.py.
    """
    s = ctx.s
    sf = ctx.sf

    if not sf:
        return None
    if re.search(r"\d+\s*km\b", s):
        return None
    # Let Phase I/II/III clinical trial handler take priority
    if re.search(r"\bphase\s*[i1]|\bphase\s*(?:ii|2)|\bphase\s*(?:iii|3)\b", s, re.I):
        return None
    if not re.search(r"\b(sites?|sitios?|centros?|all|show|ver|todos?|see|get|which|participating|involved)\b", s, re.I):
        return None

    has_assignment_kw = bool(re.search(r"\bassignment\b", s, re.I))

    _assign_name_m = (
        re.search(r"belong(?:s|ing)?\s+to\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+assignment", s, re.I) or
        re.search(r"in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+assignment", s, re.I) or
        re.search(r"of\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+assignment", s, re.I) or
        re.search(
            r"(?:assignment|estudio|actividad)\s+(?:called\s+|named\s+)?['\"]?([\w\s'\-\(\)]{2,60}?)['\"]?\s*(?:\?|$)",
            s, re.I,
        ) or
        # "participating in X" / "involved in X" without explicit "assignment" keyword
        re.search(r"\bparticipating\s+in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s*(?:[?,!.]|$)", s, re.I) or
        re.search(r"\binvolved\s+in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s*(?:[?,!.]|assignment|activity|study|trial|$)", s, re.I)
    )
    if not _assign_name_m and not has_assignment_kw:
        return None
    if not _assign_name_m:
        return None

    # Let Explorer-based handler deal with NOT queries (not_contains not supported here)
    if re.search(r"\bnot\b|\bwithout\b|\bexcluding\b", s, re.I):
        return None

    _asgn_name = _assign_name_m.group(1).strip().rstrip(".,;?")
    _stop = {"the", "a", "an", "all", "any", "some", "some", "this", "that", "named", "called",
             "any current", "current", "active", "existing", "ongoing"}
    # Reject if any individual word in the name is a stop word (catches "any current" etc.)
    _name_words = set(_asgn_name.lower().split())
    if _asgn_name.lower() in _stop or _name_words & _stop or len(_asgn_name) < 2:
        return None

    tools.dbg("assignment-sites handler: name=%r", _asgn_name)
    try:
        tbl = tools.tool_sites_by_activity(sf, name=_asgn_name)
        rows = tbl.get("rows") or []
        if rows:
            ans = (
                f"Found <strong>{len(rows)}</strong> site(s) participating in "
                f"<em>{_asgn_name}</em>."
            )
        else:
            ans = (
                f"No sites found for assignment <em>{_asgn_name}</em>. "
                f"Check the exact name in Salesforce."
            )
        return {"answer": f"<p>{ans}</p>", "table": tbl}
    except Exception as _e_asgn:
        tools.dbg("assignment-sites handler error: %s", _e_asgn)
        return None  # Fall through to LLM


# ── Handler 3: Activity (full block) ──────────────────────────────────────────

def handle_activity(
    ctx: HandlerContext,
    tools: ActivityTools,
) -> Optional[Dict[str, Any]]:
    """
    Handles all activity/enrollment intent queries.

    Priority sub-paths (in order):
      0) activities with assignments (detailed or count)
      1a) sponsor filter
      1b) list activities (pure listing)
      2) summary / overview
      3) assignments + sites count join
      4) countries involved / country breakdown
      5) by country aggregation
      6) activities with assignments (duplicate check inside else-branch)
      7) group by country stacked/unstacked chart
      8) activities by country
      9) stacked barchart follow-up
      10) quoted name → by-name resolver
      11) chart intent fallback
      12) unquoted "belong to X activity" / "in X activity"
      13) fallthrough: sites with any activity

    Mirrors lines 5338–5657 of ai_chat.py.
    Returns None if _act_intent is False or km guard suppresses it.
    """
    s = ctx.s
    sf = ctx.sf

    # _act_intent check
    _act_intent = bool(
        re.search(r"\bactivit(?:y|ies)\b", s) or
        re.search(r"\bactivites\b", s) or
        "activit" in s
    )
    # km guard
    if _act_intent and re.search(r"\d+\s*km\b", s) and re.search(
        r"actividad|within\s+\d+\s*km|\bin\s+a?\s*\d+\s*km", s
    ):
        _act_intent = False

    if not _act_intent:
        return None

    if not sf:
        return {
            "answer": (
                "<p>⚠️ Activity/enrollment data requires an active Salesforce session. "
                "Please log in via the <strong>Salesforce</strong> menu to refresh your session.</p>"
            )
        }

    # 0) activities with assignments (before is_list_activities check)
    if re.search(r"activities?\s+with\s+assignments?", s):
        m_c = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
        countries: List[str] = []
        if m_c:
            raw = m_c.group(1).strip()
            raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
            raw = raw.replace("/", ",").replace("&", ",")
            countries = [p.strip() for p in raw.split(",") if p.strip()]
        m_q = re.search(r"['\"]([^'\"]{3,})['\"]", s)
        name_like = m_q.group(1).strip() if m_q else None
        m_last = re.search(r"last\s+(\d{1,3})\s*(days|day|months|month)", s)
        last_days = last_months = None
        if m_last:
            n = int(m_last.group(1))
            unit = m_last.group(2)
            last_days = n if unit.startswith("day") else None
            last_months = n if unit.startswith("month") else last_months
        m_since = re.search(r"since\s+(\d{4}-\d{2}-\d{2})", s)
        m_until = re.search(r"(until|to)\s+(\d{4}-\d{2}-\d{2})", s)
        since_d = m_since.group(1) if m_since else None
        until_d = m_until.group(2) if m_until else None
        wants_detail = bool(re.search(
            r"(with\s+assignments?\s+list|assignments?\s+list|\bdetailed\b|\bdetalle\b)", s
        ))
        if wants_detail:
            tbl = tools.tool_activity_assignments_detailed(
                sf, countries or None, name_like,
                last_n_days=last_days, last_n_months=last_months,
                since=since_d, until=until_d,
            )
            return {"answer": "<p>Assignments per activity.</p>", "table": tbl}
        else:
            tbl = tools.tool_activities_with_assignments_counts(
                sf, last_n_days=last_days, last_n_months=last_months,
                since=since_d, until=until_d,
            )
            return {"answer": "<p>Activities with assignment counts.</p>", "table": tbl}

    # 1a) Sponsor / account filter
    _sponsor_name: Optional[str] = None
    if re.search(r"\bsponsor\b", s, re.I):
        _m_sp = (
            re.search(r"where\s+([\w\s\-&']{2,40}?)\s+is\s+(?:the\s+)?sponsor", s, re.I) or
            re.search(r"sponsor(?:ed)?\s+by\s+([\w\s\-&']{2,40})", s, re.I) or
            re.search(r"with\s+([\w\s\-&']{2,40}?)\s+as\s+(?:the\s+)?sponsor", s, re.I) or
            re.search(r"([\w\s\-&']{2,40}?)\s+(?:is|as)\s+(?:the\s+)?sponsor", s, re.I)
        )
        if _m_sp:
            _cand_sp = _m_sp.group(1).strip().rstrip(".,;")
            _stop_sp = {"the", "a", "an", "all", "any", "some", "this", "that", "their"}
            if _cand_sp.lower() not in _stop_sp and len(_cand_sp) >= 2:
                _sponsor_name = _cand_sp
    if _sponsor_name:
        table = tools.tool_list_all_activities(sf, account_name_like=_sponsor_name)
        rows = table.get("rows") or []
        ans = (
            f"Found <strong>{len(rows)}</strong> "
            f"activit{'y' if len(rows) == 1 else 'ies'} "
            f"where the sponsor / account matches <em>{_sponsor_name}</em>."
        )
        return {"answer": f"<p>{ans}</p>", "table": table}

    # 1b) is_list_activities classification
    has_list_word = bool(re.search(r"\b(list|show|all|what|which|lista|listar|mostrar|muestra)\b", s))
    has_activity_token = ("activ" in s) or bool(re.search(r"\bactivit(?:y|ies)\b|\bactividades\b", s))
    mentions_group_or_location = bool(re.search(
        r"\b(site|sites|sitio|sitios|country|countries|ciudad|ciudades|pa[ií]s|pa[ií]ses|by|per|por)\b", s
    ))
    is_list_activities = has_list_word and has_activity_token and not mentions_group_or_location

    if is_list_activities:
        _act_name_m = re.search(r"\ball\s+([\w\s'\-]{2,40}?)\s+activit", ctx.query, re.IGNORECASE)
        _act_name_filter: str = ""
        if _act_name_m:
            _cand = _act_name_m.group(1).strip()
            if _cand.lower() not in {"the", "any", "of", "some", "existing", "current", "available"}:
                _act_name_filter = _cand
        table = tools.tool_list_all_activities(sf, name_like=_act_name_filter or None)
        rows = table.get("rows") or []
        _filter_txt = f' matching "{_act_name_filter}"' if _act_name_filter else ""
        ans = f"Found {len(rows)} activit{'y' if len(rows) == 1 else 'ies'}{_filter_txt}."
        return {"answer": f"<p>{ans}</p>", "table": table}

    # ── else branch: site/country-aware activity queries ──────────────────────

    # 2) summary / overview
    if re.search(r"\b(summary|resumen|overview)\b", s) and re.search(r"activit", s):
        tbl = tools.tool_activity_country_matrix(sf, stacked=False)
        rows = tbl.get("rows") or []
        top = sorted(rows, key=lambda x: int(x.get("sites_total") or 0), reverse=True)[:3]
        top_txt = ", ".join(
            [f"{t.get('activity_name')}: {t.get('sites_total')}" for t in top]
        ) if rows else ""
        ans = f"{len(rows)} activities. Top: {top_txt}." if rows else "Activities summary."
        return {"answer": f"<p>{ans}</p>", "table": tbl}

    # 3) assignments + site count join
    if (
        re.search(r"assignments?", s) and
        re.search(r"\bcount", s) and
        re.search(r"sites?", s)
    ):
        t_assign = tools.tool_activities_with_assignments_counts(sf)
        t_sites = tools.tool_activity_country_matrix(sf, stacked=False)
        a_rows = t_assign.get("rows") or []
        s_rows = t_sites.get("rows") or []
        amap = {
            str(r.get("activity_id") or r.get("activity_name")): r
            for r in a_rows
            if r.get("activity_id") or r.get("activity_name")
        }
        out_rows = []
        for r in s_rows:
            k = str(r.get("activity_id") or r.get("activity_name"))
            a = amap.get(k) or {}
            out_rows.append({
                "activity_name": r.get("activity_name") or a.get("activity_name"),
                "assignments": a.get("assignments") or 0,
                "sites_total": r.get("sites_total") or 0,
                "countries": r.get("countries") or a.get("countries"),
                "activity_id": r.get("activity_id") or a.get("activity_id"),
            })
        out = {
            "columns": [
                {"key": "activity_name", "label": "Activity"},
                {"key": "assignments", "label": "Assignments"},
                {"key": "sites_total", "label": "Sites"},
                {"key": "countries", "label": "Countries"},
                {"key": "activity_id", "label": "Activity Id"},
            ],
            "rows": out_rows,
        }
        return {"answer": "<p>Assignments and site counts per activity.</p>", "table": out}

    # 4) countries involved / country breakdown
    if re.search(r"countries?\s+involv(ed|idos)|countries?\s+in\b|country\s+breakdown", s):
        tbl = tools.tool_activities_with_countries(sf)
        rows_tbl = tbl.get("rows") or []
        top = sorted(rows_tbl, key=lambda x: int(x.get("site_count") or 0), reverse=True)[:3]
        top_txt = ", ".join([f"{t.get('opportunity_name')}: {t.get('site_count')}" for t in top])
        ans = (
            f"{len(rows_tbl)} activities. Top: {top_txt}."
            if rows_tbl else "Activities with participating countries."
        )
        rows_s = tools.tool_activity_country_matrix(sf, stacked=True).get("rows") or []
        countries_list = sorted({r.get("country") for r in rows_s if r.get("country")})
        stacked_data = []
        activities_list = sorted({r.get("activity_name") for r in rows_s if r.get("activity_name")})
        for act in activities_list:
            rowd: Dict[str, Any] = {"activity_name": act}
            for c in countries_list:
                rowd[c] = sum(
                    r.get("sites", 0)
                    for r in rows_s
                    if r.get("activity_name") == act and r.get("country") == c
                )
            stacked_data.append(rowd)
        viz = tools.tool_render_chart(
            "bar", stacked_data, "activity_name", countries_list[:8],
            {"title": "Countries per Activity (Stacked)"}, {},
        )
        return {"answer": f"<p>{ans}</p>", "table": tbl, "visualization": viz.get("visualization")}

    # 5) by country aggregation (simple)
    if re.search(r"\b(by|per|por)\s+(country|pa[ií]s|pa[ií]ses)\b", s):
        tbl = tools.tool_activity_counts_by_country(sf)
        rows = tbl.get("rows") or []
        total = sum(int(r.get("activities") or 0) for r in rows)
        top = sorted(rows, key=lambda x: int(x.get("activities") or 0), reverse=True)[:3]
        top_txt = ", ".join([f"{t.get('country')}: {t.get('activities')}" for t in top])
        viz = tools.tool_render_chart(
            "bar", rows, "country", ["activities"], {"title": "Activities by Country"}, {}
        )
        return {
            "answer": f"<p>Total {total}. Top: {top_txt}.</p>",
            "table": tbl,
            "visualization": viz.get("visualization"),
        }

    # 6) activities with assignments (else-branch duplicate — date filters)
    if re.search(r"activities?\s+with\s+assignments?", s):
        m_c = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
        countries_b: List[str] = []
        if m_c:
            raw = m_c.group(1).strip()
            raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
            raw = raw.replace("/", ",").replace("&", ",")
            countries_b = [p.strip() for p in raw.split(",") if p.strip()]
        m_q = re.search(r"['\"]([^'\"]{3,})['\"]", s)
        name_like_b = m_q.group(1).strip() if m_q else None
        m_last = re.search(r"last\s+(\d{1,3})\s*(days|day|months|month)", s)
        last_days_b = last_months_b = None
        if m_last:
            n = int(m_last.group(1))
            unit = m_last.group(2)
            if unit.startswith("day"):
                last_days_b = n
            else:
                last_months_b = n
        m_since = re.search(r"since\s+(\d{4}-\d{2}-\d{2})", s)
        m_until = re.search(r"until\s+(\d{4}-\d{2}-\d{2})|to\s+(\d{4}-\d{2}-\d{2})", s)
        since_db = m_since.group(1) if m_since else None
        until_db = (m_until.group(1) or m_until.group(2)) if m_until else None
        wants_detail_b = bool(re.search(
            r"(with\s+assignments?\s+list|assignments?\s+list|\bdetailed\b|\bdetalle\b)", s
        ))
        if wants_detail_b:
            tbl_b = tools.tool_activity_assignments_detailed(
                sf, countries_b or None, name_like_b,
                last_n_days=last_days_b, last_n_months=last_months_b,
                since=since_db, until=until_db,
            )
            return {"answer": "<p>Assignments per activity.</p>", "table": tbl_b}
        else:
            tbl_b = tools.tool_activities_with_assignments_counts(
                sf, last_n_days=last_days_b, last_n_months=last_months_b,
                since=since_db, until=until_db,
            )
            return {"answer": "<p>Activities with assignment counts.</p>", "table": tbl_b}

    # 7) group by opportunity/activity × country (stacked or not)
    if (
        re.search(r"\b(group|by|per|por)\b[\s\S]*\b(opportunity\s*name|activity|activit(?:y|ies))\b", s)
        and re.search(r"\bcountr|pa[ií]s", s)
    ):
        wants_stack = bool(re.search(r"\b(stack|stacked|segment|breakdown)\b", s))
        if wants_stack or re.search(r"\b(chart|graph|bar|pie)\b", s):
            tbl7 = tools.tool_activity_country_matrix(sf, stacked=True)
            rows7 = tbl7.get("rows") or []
            countries7 = sorted({r.get("country") for r in rows7 if r.get("country")})
            stacked_data7: List[Dict[str, Any]] = []
            activities7 = sorted({r.get("activity_name") for r in rows7 if r.get("activity_name")})
            for activity in activities7:
                row_data: Dict[str, Any] = {"activity_name": activity}
                for country in countries7:
                    row_data[country] = sum(
                        r.get("sites", 0)
                        for r in rows7
                        if r.get("activity_name") == activity and r.get("country") == country
                    )
                stacked_data7.append(row_data)
            viz7 = tools.tool_render_chart(
                "bar", stacked_data7, "activity_name", countries7[:8],
                {"title": "Countries per Activity (Stacked)"}, {},
            )
            viz_cols7 = [{"key": "activity_name", "label": "Activity"}] + [
                {"key": c, "label": tools.pretty_label(c)} for c in countries7[:8]
            ]
            viz_tbl7 = {"columns": viz_cols7, "rows": stacked_data7}
            return {
                "answer": f"<p>{len(activities7)} activities with country breakdown.</p>",
                "table": viz_tbl7,
                "visualization": viz7.get("visualization"),
            }
        else:
            tbl7b = tools.tool_activity_country_matrix(sf, stacked=False)
            rows7b = tbl7b.get("rows") or []
            viz7b = tools.tool_render_chart(
                "bar", rows7b, "activity_name", ["sites_total"], {"title": "Sites per Activity"}, {}
            )
            return {
                "answer": f"<p>{len(rows7b)} activities found.</p>",
                "table": tbl7b,
                "visualization": viz7b.get("visualization"),
            }

    # 8) "activities by country" explicit phrasing
    if (
        ("activ" in s and re.search(r"\bby\s+country\b", s)) or
        re.search(r"\b(activities?|activity|activites)\b[^\n]*\bby\s+country\b", s)
    ):
        tbl8_agg = tools.tool_activity_country_matrix(sf, stacked=False)
        tbl8_stack = tools.tool_activity_country_matrix(sf, stacked=True)
        rows8_s = tbl8_stack.get("rows") or []
        countries8 = sorted({r.get("country") for r in rows8_s if r.get("country")})
        stacked_data8: List[Dict[str, Any]] = []
        activities8 = sorted({r.get("activity_name") for r in rows8_s if r.get("activity_name")})
        for act in activities8:
            rowd8: Dict[str, Any] = {"activity_name": act}
            for c in countries8:
                rowd8[c] = sum(
                    r.get("sites", 0)
                    for r in rows8_s
                    if r.get("activity_name") == act and r.get("country") == c
                )
            stacked_data8.append(rowd8)
        viz8 = tools.tool_render_chart(
            "bar", stacked_data8, "activity_name", countries8[:8],
            {"title": "Countries per Activity (Stacked)"}, {},
        )
        return {
            "answer": f"<p>{len(activities8)} activities with country breakdown.</p>",
            "table": tbl8_agg,
            "visualization": viz8.get("visualization"),
        }

    # 9) stacked barchart follow-up
    if (
        re.search(r"\bstacked\b", s) and
        re.search(r"\bbar\b|\bbarchart\b|\bbar\s*chart\b", s) and
        re.search(r"activit", s)
    ):
        tbl9 = tools.tool_activity_country_matrix(sf, stacked=True)
        rows9 = tbl9.get("rows") or []
        countries9 = sorted({r.get("country") for r in rows9 if r.get("country")})
        stacked_data9: List[Dict[str, Any]] = []
        activities9 = sorted({r.get("activity_name") for r in rows9 if r.get("activity_name")})
        for activity in activities9:
            row_data9: Dict[str, Any] = {"activity_name": activity}
            for country in countries9:
                row_data9[country] = sum(
                    r.get("sites", 0)
                    for r in rows9
                    if r.get("activity_name") == activity and r.get("country") == country
                )
            stacked_data9.append(row_data9)
        viz9 = tools.tool_render_chart(
            "bar", stacked_data9, "activity_name", countries9[:8],
            {"title": "Countries per Activity (Stacked)"}, {},
        )
        viz_cols9 = [{"key": "activity_name", "label": "Activity"}] + [
            {"key": c, "label": tools.pretty_label(c)} for c in countries9[:8]
        ]
        viz_tbl9 = {"columns": viz_cols9, "rows": stacked_data9}
        return {
            "answer": f"<p>{len(activities9)} activities with country breakdown.</p>",
            "table": viz_tbl9,
            "visualization": viz9.get("visualization"),
        }

    # 10+11+12+13) country extraction for name-based lookups
    m_c2 = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", s)
    countries2: List[str] = []
    if m_c2:
        raw = m_c2.group(1).strip()
        raw = re.sub(r"\b(and|y|e)\b", ",", raw, flags=re.I)
        raw = raw.replace("/", ",").replace("&", ",")
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        countries2 = [p.title() for p in parts]

    # 10) quoted name → by-name resolver
    m_qname = re.search(
        r"participat\w*\s+.*?activity\s+['\"]([^'\"]{3,})['\"]|['\"]([^'\"]{3,})['\"]\s+activit",
        s,
    )
    if m_qname:
        name = (m_qname.group(1) or m_qname.group(2) or "").strip()
        byname = tools.tool_sites_by_activity(sf, name=name, countries=countries2 or None, exact=False)
        return {"answer": "<p>Sites by activity name.</p>", "table": byname}

    # 11) chart intent fallback
    if re.search(r"\b(chart|bar|graph|pie|visuali[sz]e)\b", s):
        tbl_act = tools.tool_activity_country_matrix(sf, stacked=False)
        rows_act = tbl_act.get("rows") or []
        viz_act = tools.tool_render_chart(
            "bar", rows_act, "activity_name", ["sites_total"], {"title": "Sites per Activity"}, {}
        )
        return {
            "answer": f"<p>{len(rows_act)} activities found.</p>",
            "table": tbl_act,
            "visualization": viz_act.get("visualization"),
        }

    # 12) unquoted "belong to X activity" / "in X activity"
    _act_belong_m = (
        re.search(r"belong(?:s|ing)?\s+to\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"of\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"for\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I)
    )
    _stop_act = {"the", "a", "an", "all", "any", "some", "this", "that"}
    if _act_belong_m:
        _act_nm = _act_belong_m.group(1).strip().rstrip(".,;?")
        if _act_nm.lower() not in _stop_act and len(_act_nm) >= 2:
            byname = tools.tool_sites_by_activity(
                sf, name=_act_nm, countries=countries2 or None, exact=False
            )
            rows_bn = byname.get("rows") or []
            if rows_bn:
                return {
                    "answer": (
                        f"<p>Found <strong>{len(rows_bn)}</strong> site(s) "
                        f"participating in <em>{_act_nm}</em>.</p>"
                    ),
                    "table": byname,
                }

    # 12b) "how many sites per/each activity" → activity site count matrix
    if re.search(r"\bhow\s+many\s+sites?\b", s) and re.search(r"\b(each|per\s+activit|in\s+each|for\s+each)\b", s):
        tbl12b = tools.tool_activity_country_matrix(sf, stacked=False)
        rows12b = tbl12b.get("rows") or []
        return {"answer": f"<p>Found <strong>{len(rows12b)}</strong> activities with site participation counts.</p>", "table": tbl12b}

    # 13) fallthrough: sites with any activity
    table = tools.tool_sites_with_any_activity(sf, countries=countries2 or None)
    rows = table.get("rows") or []
    accs = len({
        str(r.get("account_id"))
        for r in rows
        if isinstance(r, dict) and r.get("account_id")
    })
    ans = f"Found activities in {accs} account(s)."
    return {"answer": f"<p>{ans}</p>", "table": table}
