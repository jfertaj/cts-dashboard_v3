"""
Moby deterministic handlers — extracted from _try_planner in ai_chat.py.

Each handler receives a HandlerContext (shared state) and an ActivityTools
container (injected callables). This avoids circular imports while keeping
handlers testable: pass mock tools in tests.

Handler priority order (same as _try_planner):
  1. handle_followup_activity_sponsor       — sponsor follow-up (pre-activity-intent)
  1b. handle_multi_activity_intersection   — "sites in BOTH X and Y" (must precede 2)
  2. handle_assignment_sites               — "sites in/belonging to X assignment"
  3. handle_activity                       — all activity/enrollment queries

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


# ── Handler 1b: Multi-activity intersection ────────────────────────────────────

def handle_multi_activity_intersection(
    ctx: HandlerContext,
    tools: ActivityTools,
) -> Optional[Dict[str, Any]]:
    """
    Handles "sites in BOTH activity A AND activity B" intersection queries.

    MUST fire before handle_assignment_sites: without this handler, that function
    would silently extract only the first activity name and return a wrong result.

    Approach: two tool_sites_by_activity calls + Python set intersection.
    A single SOQL WHERE with AND on two different C_Opportunity_Name__r.Name
    values ALWAYS returns 0 rows — each Assignment__c row belongs to exactly
    one activity.
    """
    s = ctx.s
    sf = ctx.sf

    if not sf:
        return None

    # Require an explicit intersection-intent signal (keeps false-positive rate near zero)
    if not re.search(
        r"\bboth\b|\band\s+also\b|\balso\s+(?:in|participat)|\bintersect\b|\bcommon\b",
        s, re.I,
    ):
        return None
    # Require site / activity context
    if not re.search(r"\bsite[s]?\b|\bactivit|\bassignment|\bparticipat|\binvolved", s, re.I):
        return None

    _name_a: Optional[str] = None
    _name_b: Optional[str] = None

    # Pattern A: "both [the] NAME [activity/assignment] and [the] NAME [activity/assignment]"
    _m = re.search(
        r"\bboth\s+(?:the\s+)?([\w\s'\-]{2,40}?)\s*(?:activity|assignment)?\s*"
        r"(?:and|,)\s+(?:the\s+)?([\w\s'\-]{2,40}?)\s*(?:activity|assignment|\?|$)",
        s, re.I,
    )
    if _m:
        _name_a, _name_b = _m.group(1).strip(), _m.group(2).strip()

    if not (_name_a and _name_b):
        # Pattern B: "in [the] NAME [activity/assignment] that [also] [participate] in [the] NAME"
        _m = re.search(
            r"\bin\s+(?:the\s+)?([\w\s'\-]{2,40}?)\s*(?:activity|assignment)?\s+"
            r"(?:that\s+(?:also\s+)?(?:participat[a-z]+\s+in\b|(?:are\s+)?in\b)|"
            r"and\s+also\s+(?:participat[a-z]+\s+in\b|in\b)|"
            r",\s*also\s+(?:participat[a-z]+\s+in\b|in\b))"
            r"\s+(?:the\s+)?([\w\s'\-]{2,40}?)\s*(?:activity|assignment|\?|$)",
            s, re.I,
        )
        if _m:
            _name_a, _name_b = _m.group(1).strip(), _m.group(2).strip()

    if not (_name_a and _name_b):
        return None

    # Cleanup both names: strip trailing keywords, punctuation, stop words
    _stop = {"the", "a", "an", "both", "any", "all", "this", "that", "named", "called"}
    for _orig, _attr in ((_name_a, "a"), (_name_b, "b")):
        _n = re.sub(r'\s+(?:assignment[s]?|opportunit(?:y|ies)|activit(?:y|ies))\s*$', '', _orig, flags=re.I).strip()
        _n = _n.rstrip(".,;?")
        if _n.lower() in _stop or len(_n) < 2:
            return None
        if _attr == "a":
            _name_a = _n
        else:
            _name_b = _n

    if _name_a.lower() == _name_b.lower():
        return None  # degenerate: same name twice

    # Guard: clinical stage labels are not activity names — let Stage1/Stage2 handler handle these
    _stage_pat = re.compile(r"^stage\s*[12]\b|^stage\s*(?:one|two)\b", re.I)
    if _stage_pat.match(_name_a) or _stage_pat.match(_name_b):
        return None

    # Guard: clinical capability phrases are not activity names — let qual/SF handler handle these
    _clinical_pat = re.compile(
        r"\b(hla|typing|overnight|pharmacy|biobank|t1d|patients|per\s+year)\b", re.I
    )
    if _clinical_pat.search(_name_a) or _clinical_pat.search(_name_b):
        return None

    tools.dbg("multi-activity intersection: %r ∩ %r", _name_a, _name_b)
    try:
        tbl_a = tools.tool_sites_by_activity(sf, name=_name_a)
        tbl_b = tools.tool_sites_by_activity(sf, name=_name_b)
        rows_a = tbl_a.get("rows") or []
        rows_b = tbl_b.get("rows") or []

        ids_a = {r.get("account_id") for r in rows_a if r.get("account_id")}
        ids_b = {r.get("account_id") for r in rows_b if r.get("account_id")}
        ids_common = ids_a & ids_b

        rows_common = [r for r in rows_a if r.get("account_id") in ids_common]

        if rows_common:
            ans = (
                f"Found <strong>{len(rows_common)}</strong> site(s) participating in "
                f"both <em>{_name_a}</em> and <em>{_name_b}</em>."
            )
        elif not rows_a:
            ans = (
                f"No sites found for <em>{_name_a}</em> — check the exact name in Salesforce. "
                f"<em>{_name_b}</em> has {len(rows_b)} site(s)."
            )
        elif not rows_b:
            ans = (
                f"No sites found for <em>{_name_b}</em> — check the exact name in Salesforce. "
                f"<em>{_name_a}</em> has {len(rows_a)} site(s)."
            )
        else:
            ans = (
                f"No sites participate in both <em>{_name_a}</em> ({len(rows_a)} site(s)) "
                f"and <em>{_name_b}</em> ({len(rows_b)} site(s)) — the two activities share no common sites."
            )

        tbl_out = {
            "columns": tbl_a.get("columns") or [
                {"key": "account_id", "label": "Account Id"},
                {"key": "site", "label": "Account Name"},
                {"key": "country", "label": "Country"},
                {"key": "city", "label": "City"},
            ],
            "rows": rows_common,
        }
        return {"answer": f"<p>{ans}</p>", "table": tbl_out}
    except Exception as _e:
        tools.dbg("multi-activity intersection error: %s", _e)
        return None  # fall through to handle_assignment_sites


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
    # Let km-of-assignment / nearest handler take priority for "near/close/within" queries
    if re.search(r"\bnear(?:\s+to)?\b|\bclos(?:e|est)\b|\bwithin\b|\baround\b", s, re.I):
        return None
    # Let sponsor/company handler take priority
    if re.search(r"\bsponsore?(?:d|s|ing)?\b", s, re.I):
        return None
    # Let Phase I/II/III clinical trial handler take priority
    if re.search(r"\bphase\s*[i1]|\bphase\s*(?:ii|2)|\bphase\s*(?:iii|3)\b", s, re.I):
        return None
    if not re.search(r"\b(sites?|sitios?|centros?|all|show|ver|todos?|see|get|which|participating|involved)\b", s, re.I):
        return None

    has_assignment_kw = bool(re.search(r"\b(?:assignment|opportunity)\b", s, re.I))

    _assign_name_m = (
        re.search(r"belong(?:s|ing)?\s+to\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+(?:assignment|opportunity)", s, re.I) or
        re.search(r"in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+(?:assignment|opportunity)", s, re.I) or
        # "participating in/to X" checked BEFORE "of" to avoid greedy "of the sites participating to X" false match
        re.search(r"\bparticipating\s+(?:in|to)\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s*(?:[?,!.]|$)", s, re.I) or
        re.search(r"\binvolved\s+in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s*(?:[?,!.]|assignment|activity|study|trial|$)", s, re.I) or
        re.search(r"of\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+(?:assignment|opportunity)", s, re.I) or
        re.search(
            r"(?:assignment|opportunity|estudio|actividad)\s+(?:called\s+|named\s+)?['\"]?([\w\s'\-\(\)]{2,60}?)['\"]?\s*(?:\?|$)",
            s, re.I,
        )
    )
    if not _assign_name_m and not has_assignment_kw:
        return None
    if not _assign_name_m:
        return None

    # Let Explorer-based handler deal with NOT queries (not_contains not supported here)
    if re.search(r"\bnot\b|\bwithout\b|\bexcluding\b", s, re.I):
        return None

    _asgn_name = _assign_name_m.group(1).strip().rstrip(".,;?")
    # Strip trailing keywords if captured as part of the name
    _asgn_name = re.sub(r'\s+(?:assignment[s]?|opportunit(?:y|ies)|activit(?:y|ies))\s*$', '', _asgn_name, flags=re.I).strip()
    # Multi-name queries ("X and Y", "X or Y") → let handler 12d handle them
    if re.search(r"\b(?:and|or)\b", _asgn_name, re.I):
        return None
    _stop = {"the", "a", "an", "all", "any", "some", "this", "that", "named", "called",
             "any current", "current", "active", "existing", "ongoing"}
    # Reject if the full name is a stop word, or if it's suspiciously long (likely a false pattern match)
    _name_words = set(_asgn_name.lower().split())
    if _asgn_name.lower() in _stop or len(_name_words) > 6 or len(_asgn_name) < 2:
        return None
    # Guard: reject "in [Country]" — country/region is a geographic filter, not an activity name
    _COUNTRY_NAMES = {
        "germany", "france", "italy", "spain", "belgium", "netherlands", "austria",
        "portugal", "poland", "switzerland", "sweden", "denmark", "norway", "finland",
        "ireland", "czech republic", "hungary", "romania", "bulgaria", "croatia",
        "slovenia", "serbia", "slovakia", "lithuania", "latvia", "estonia", "uk",
        "united kingdom", "greece", "turkey", "ukraine", "europe", "eastern europe",
        "western europe", "central europe", "scandinavia", "nordics",
    }
    _name_lower = _asgn_name.lower().strip()
    # "in germany", "in France", etc.
    if re.match(r"^in\s+", _name_lower):
        _after_in = re.sub(r"^in\s+", "", _name_lower)
        if _after_in in _COUNTRY_NAMES:
            return None
    # bare country name captured as assignment name
    if _name_lower in _COUNTRY_NAMES:
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
        "activit" in s or
        # "assignments where [company] is a sponsor" — treat as activity intent
        (re.search(r"\bassignment[s]?\b", s) and re.search(r"\bsponsore?(?:d|s|ing)?\b", s, re.I))
    )
    # km guard
    if _act_intent and re.search(r"\d+\s*km\b", s) and re.search(
        r"actividad|within\s+\d+\s*km|\bin\s+a?\s*\d+\s*km", s
    ):
        _act_intent = False
    # near-site guard: "near the sites involved in [activity]" → km-of-assignment handler
    if _act_intent and re.search(r"\bnear(?:\s+to)?\b|\bclos(?:e|est)\b|\baround\b", s, re.I) and \
            re.search(r"\bsites?\s+(?:involved|assigned|participating)\b|\binvolved\s+in\b", s, re.I):
        return None

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
            raw = re.sub(r"\b(and|or|y|e)\b", ",", raw, flags=re.I)
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

    # 1a) Sponsor / account filter — matches "sponsor", "sponsored by", "sponsoring"
    _sponsor_name: Optional[str] = None
    if re.search(r"\bsponsore?(?:d|s|ing)?\b", s, re.I):
        _m_sp = (
            re.search(r"where\s+([\w\s\-&']{2,40}?)\s+is\s+(?:(?:a|the)\s+)?sponsor", s, re.I) or
            re.search(r"sponsor(?:ed)?\s+by\s+([\w\s\-&']{2,40})", s, re.I) or
            re.search(r"with\s+([\w\s\-&']{2,40}?)\s+as\s+(?:(?:a|the)\s+)?sponsor", s, re.I) or
            re.search(r"([\w\s\-&']{2,40}?)\s+(?:is|as)\s+(?:(?:a|the)\s+)?sponsor", s, re.I) or
            re.search(r"(?:activities?|assignments?)\s+sponsor(?:ed)?\s+by\s+([\w\s\-&']{2,40})", s, re.I) or
            re.search(r"sites?\s+involved\s+in\s+(?:activities?|assignments?)\s+sponsor(?:ed)?\s+by\s+([\w\s\-&']{2,40})", s, re.I)
        )
        if _m_sp:
            _cand_sp = _m_sp.group(1).strip().rstrip(".,;?")
            _stop_sp = {"the", "a", "an", "all", "any", "some", "this", "that", "their"}
            if _cand_sp.lower() not in _stop_sp and len(_cand_sp) >= 2:
                _sponsor_name = _cand_sp
    if _sponsor_name:
        # Find activities by sponsor
        act_tbl = tools.tool_list_all_activities(sf, account_name_like=_sponsor_name)
        act_rows = act_tbl.get("rows") or []
        if not act_rows:
            return {"answer": f"<p>No activities found with sponsor matching <em>{_sponsor_name}</em>.</p>", "table": act_tbl}
        # If query asks for sites ("how many sites", "which sites"), fetch sites per activity
        _wants_sites = bool(re.search(r"\bsite[s]?\b|\bhow\s+many\b|\bwhich\b|\binvolved\b", s, re.I))
        if _wants_sites:
            # Collect unique sites across all sponsor activities using tool_sites_by_activity
            _seen_aids: set = set()
            _site_rows: list = []
            for _ar in act_rows:
                _aname = _ar.get("activity_name") or ""
                if not _aname:
                    continue
                _st = tools.tool_sites_by_activity(sf, name=_aname)
                for _sr in (_st.get("rows") or []):
                    _aid = str(_sr.get("account_id") or "")
                    if _aid and _aid not in _seen_aids:
                        _seen_aids.add(_aid)
                        _site_rows.append(_sr)
            _act_names_str = ", ".join(
                [f"<em>{r.get('activity_name','')}</em>" for r in act_rows]
            )
            _site_tbl = _st.__class__({  # reuse normalized structure
                "columns": [
                    {"key": "site", "label": "Site"},
                    {"key": "city", "label": "City"},
                    {"key": "country", "label": "Country"},
                    {"key": "activity_name", "label": "Activity"},
                ],
                "rows": _site_rows,
            }) if False else {"columns": [
                {"key": "account_name", "label": "Site"},
                {"key": "city", "label": "City"},
                {"key": "country", "label": "Country"},
                {"key": "activity_name", "label": "Activity"},
            ], "rows": _site_rows}
            return {
                "answer": (
                    f"<p>Found <strong>{len(_site_rows)}</strong> unique site(s) involved in "
                    f"{len(act_rows)} activit{'y' if len(act_rows)==1 else 'ies'} "
                    f"sponsored by <em>{_sponsor_name}</em>: {_act_names_str}.</p>"
                ),
                "table": _site_tbl,
            }
        # Otherwise just return the activity list
        ans = (
            f"Found <strong>{len(act_rows)}</strong> "
            f"activit{'y' if len(act_rows) == 1 else 'ies'} "
            f"where the sponsor / account matches <em>{_sponsor_name}</em>."
        )
        return {"answer": f"<p>{ans}</p>", "table": act_tbl}

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
            raw = re.sub(r"\b(and|or|y|e)\b", ",", raw, flags=re.I)
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
        raw = re.sub(r"\b(and|or|y|e)\b", ",", raw, flags=re.I)
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

    # 12) unquoted "belong to X activity" / "in X activity" / "assigned to X activity"
    _act_belong_m = (
        re.search(r"\bassigned\s+to\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"belong(?:s|ing)?\s+to\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"in\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"of\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I) or
        re.search(r"for\s+(?:the\s+)?([\w\s'\-\(\)]{2,60}?)\s+activit", s, re.I)
    )
    _stop_act = {"the", "a", "an", "all", "any", "some", "this", "that"}
    if _act_belong_m:
        _act_nm = _act_belong_m.group(1).strip().rstrip(".,;?")
        # Multi-name queries ("X and Y", "X or Y") → skip to 12d
        if re.search(r"\b(?:and|or)\b", _act_nm, re.I):
            _act_belong_m = None
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

    # 12c) Multi-country AND coverage (GQ18):
    #   "which activities have sites in Germany, France AND Italy?"
    #   Requires 2+ countries joined by AND (not OR) + activity coverage intent.
    #   Uses tool_activities_with_countries which returns {activity → countries_set},
    #   then Python set filter: required_countries ⊆ activity_countries.
    #   Must fire before sub-path 13 (fallthrough) which uses OR semantics.
    #
    #   Own country extraction (not countries2) because m_c2 uses '$' which fails
    #   when the query ends with '?' — the most common case for GQ18-style questions.
    _s12c = s.rstrip("?!.")
    _m_awc = re.search(r"\bin\s+([a-zA-ZÀ-ÿ'\- ,/&]+)$", _s12c)
    _countries12c: List[str] = []
    if _m_awc:
        _raw12c = _m_awc.group(1).strip()
        _raw12c = re.sub(r"\b(and|y|e)\b", ",", _raw12c, flags=re.I)
        _raw12c = _raw12c.replace("/", ",").replace("&", ",")
        _countries12c = [p.strip().title() for p in _raw12c.split(",") if p.strip()]
    # Guard: if any extracted "country" looks like an activity keyword, skip 12c
    _countries12c_has_activity_word = any(
        re.search(r"\bactivit", c, re.I) for c in _countries12c
    )
    _is_multi_country_and_coverage = (
        len(_countries12c) >= 2
        and not re.search(r"\bor\b", s, re.I)
        and not _countries12c_has_activity_word
        and re.search(r"\bhave\b|\bwith\b|\bcontain\b|\bhaving\b|\bcovering\b|\bparticipat", s, re.I)
    )
    if _is_multi_country_and_coverage:
        tbl_awc = tools.tool_activities_with_countries(sf)
        rows_awc = tbl_awc.get("rows") or []
        required = {c.strip().lower() for c in _countries12c}
        filtered_awc = []
        for _row in rows_awc:
            cs = _row.get("countries") or ""
            if cs == "(no sites)":
                continue
            row_countries = {c.strip().lower() for c in cs.split(",") if c.strip()}
            if required <= row_countries:  # all required countries present (AND semantics)
                filtered_awc.append(_row)
        _country_list = ", ".join(_countries12c)
        if filtered_awc:
            _n = len(filtered_awc)
            _ans_awc = (
                f"Found <strong>{_n}</strong> activit{'y' if _n == 1 else 'ies'} "
                f"with sites in all of: <em>{_country_list}</em>."
            )
        else:
            _ans_awc = f"No activities have sites in all of: <em>{_country_list}</em>."
        _tbl_awc_out = {
            "columns": tbl_awc.get("columns") or [
                {"key": "opportunity_name", "label": "Activity"},
                {"key": "countries", "label": "Countries"},
                {"key": "site_count", "label": "Site Count"},
            ],
            "rows": filtered_awc,
        }
        return {"answer": f"<p>{_ans_awc}</p>", "table": _tbl_awc_out}

    # 12d) "sites that have DETECT and SAFEGUARD activities"
    #      "sites with Baricade activities"
    #      "sites that belong to both DETECT and Safeguard activities"
    #      "sites participating in DETECT and Fabulinus activities"
    #      Captures activity name(s) before "activit*" from various phrasings.
    #      Multiple names joined by "and"/"or"/","  → AND or OR semantics.
    _m_have_act = (
        re.search(r"\b(?:have|with|having)\s+([\w\s'\-\(\),&/]+?)\s+activit", s, re.I) or
        re.search(r"\bbelong\w*\s+to\s+(?:both\s+)?([\w\s'\-\(\),&/]+?)\s+activit", s, re.I) or
        re.search(r"\bparticipat\w+\s+in\s+(?:both\s+)?([\w\s'\-\(\),&/]+?)\s+activit", s, re.I) or
        re.search(r"\binvolved\s+in\s+(?:both\s+)?([\w\s'\-\(\),&/]+?)\s+activit", s, re.I) or
        re.search(r"\bin\s+(?:both\s+)([\w\s'\-\(\),&/]+?)\s+activit", s, re.I)
    )
    if _m_have_act:
        _raw_names = _m_have_act.group(1).strip()
        # strip leading stop-words
        _raw_names = re.sub(r"^(?:the|a|an|any|some|both)\s+", "", _raw_names, flags=re.I)
        # detect OR vs AND
        _has_or = bool(re.search(r"\bor\b", _raw_names, re.I))
        # split on "and", "or", ",", "&", "/"
        _parts_act = re.split(r"\s+and\s+|\s+or\s+|[,&/]", _raw_names, flags=re.I)
        _act_names = [p.strip().rstrip(".,;?") for p in _parts_act if p.strip()]
        _stop_names = {"the", "a", "an", "all", "any", "some", "this", "that", "no", "each"}
        _act_names = [n for n in _act_names if n.lower() not in _stop_names and len(n) >= 2]
        if _act_names:
            # Filter countries2: remove entries that overlap with extracted activity names
            # (prevents "participating in DETECT or Safeguard activities" from treating
            # "detect"/"safeguard" as country filters)
            _act_lower = {n.lower() for n in _act_names}
            _clean_countries = [
                c for c in (countries2 or [])
                if not any(a in c.lower() for a in _act_lower)
                and "activit" not in c.lower()
            ] or None
            # Lookup sites for each activity name
            _sets_by_name: Dict[str, set] = {}
            _rows_by_acc: Dict[str, dict] = {}
            _act_label_by_acc: Dict[str, set] = {}
            for _aname in _act_names:
                _tbl_a = tools.tool_sites_by_activity(sf, name=_aname, countries=_clean_countries, exact=False)
                _arows = _tbl_a.get("rows") or []
                _aid_set: set = set()
                for _r in _arows:
                    _aid = str(_r.get("account_id") or "")
                    if _aid:
                        _aid_set.add(_aid)
                        _rows_by_acc.setdefault(_aid, _r)
                        _act_label_by_acc.setdefault(_aid, set()).add(
                            _r.get("activity_name") or _aname
                        )
                _sets_by_name[_aname] = _aid_set
            # Combine: AND (intersection) or OR (union)
            if _has_or:
                _final_aids = set().union(*_sets_by_name.values()) if _sets_by_name else set()
            else:
                _all_sets = list(_sets_by_name.values())
                _final_aids = _all_sets[0].intersection(*_all_sets[1:]) if _all_sets else set()
            _out_rows = []
            for _aid in sorted(_final_aids):
                _base = dict(_rows_by_acc.get(_aid, {}))
                _base["matched_activities"] = "; ".join(sorted(_act_label_by_acc.get(_aid, set())))
                _out_rows.append(_base)
            _names_str = ", ".join(f"<em>{n}</em>" for n in _act_names)
            _logic_word = "or" if _has_or else "and"
            _ans_12d = (
                f"Found <strong>{len(_out_rows)}</strong> site(s) with "
                f"{_names_str} activities ({_logic_word} match)."
            )
            _cols_12d = [
                {"key": "site", "label": "Site"},
                {"key": "city", "label": "City"},
                {"key": "country", "label": "Country"},
                {"key": "matched_activities", "label": "Matched Activities"},
            ]
            return {"answer": f"<p>{_ans_12d}</p>", "table": {"columns": _cols_12d, "rows": _out_rows}}

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
