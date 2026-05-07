"""Activity-related tool implementations.

Pure move from `app.routers.ai_chat` (Phase 4 refactor). Behavior is
unchanged. Helpers live in `app.moby.helpers.*` and sibling tool modules;
all imports are direct (post-Phase-3 protocol). The functions are
re-exported by `ai_chat.py`, so callers / tests that import
`app.routers.ai_chat.tool_sites_by_activity` etc. keep working.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.moby.helpers.debug import _dbg
from app.moby.helpers.tables import _normalize_table_for_ui
from app.moby.tools.salesforce import tool_salesforce_query
from app.routers.salesforce_explorer import _build_account_map


def tool_sites_by_activity(
    sf,
    name: str,
    countries: Optional[List[str]] = None,
    exact: bool = False,
):
    if not sf:
        raise HTTPException(400, "No SF session")
    if not name or not str(name).strip():
        raise HTTPException(400, "Missing activity/opportunity name")

    # Build country filter (sanitize inputs)
    country_cond = ""
    def _clean_countries_local(arr: Optional[List[str]]) -> List[str]:
        if not arr:
            return []
        out: List[str] = []
        for c in arr:
            t = str(c or '').strip()
            if not t:
                continue
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            if any(x in t.lower() for x in ("activity","screen","stage")):
                continue
            out.append(t)
        return out
    if countries:
        safe = _clean_countries_local(countries)
        vals = ", ".join(["'" + str(c).strip().replace("'", "\\'") + "'" for c in safe if str(c).strip()])
        if vals:
            country_cond = f" AND C_Account__r.ShippingCountry IN ({vals})"

    nm = str(name).strip().replace("'", "\\'")
    # Case-insensitive matching against Assignment__c → C_Opportunity_Name__r.Name
    if exact:
        cond_name = f"C_Opportunity_Name__r.Name = '{nm}'"
    else:
        variants = {nm}
        try:
            variants.update({nm.lower(), nm.upper(), nm.title()})
            # Fuzzy: strip doubled consonants (e.g. "barricade" → "baricade")
            _dedup = re.sub(r'([bcdfghjklmnpqrstvwxyz])\1+', r'\1', nm.lower(), flags=re.I)
            if _dedup != nm.lower():
                variants.add(_dedup)
                variants.add(_dedup.title())
        except Exception:
            pass
        like_parts = [f"C_Opportunity_Name__r.Name LIKE '%{v}%'" for v in sorted(variants) if v]
        cond_name = " OR ".join(like_parts) if like_parts else f"C_Opportunity_Name__r.Name LIKE '%{nm}%'"

    # Prefer Assignment__c linkage (more accurate site participation)
    asn_soql = (
        "SELECT Id, C_Account__c, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity, "
        "C_Opportunity_Name__r.Name "
        "FROM Assignment__c "
        f"WHERE ({cond_name}) AND C_Account__c != null{country_cond}"
    )
    try:
        asn_recs = sf.query_all(asn_soql).get("records", [])
    except Exception as _e:
        asn_recs = []
    rows: List[Dict[str, Any]] = []
    if asn_recs:
        for r in asn_recs:
            acc = r.get("C_Account__r") or {}
            rows.append({
                "account_id": r.get("C_Account__c"),
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
                "activity_name": (r.get("C_Opportunity_Name__r") or {}).get("Name"),
            })
    else:
        # Fallback to Opportunity direct match if no assignments found
        import os
        rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
        rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity","RT_Activity"]
        if "Activity" not in rt_list: rt_list.append("Activity")
        if "RT_Activity" not in rt_list: rt_list.append("RT_Activity")
        rt_vals = ", ".join([f"'{x}'" for x in rt_list])
        country_sub = ""
        if country_cond:
            # convert to Account subquery filter form
            vals = re.search(r"IN \((.*)\)", country_cond)
            country_sub = f" AND ShippingCountry IN ({vals.group(1)})" if vals else ""
        soql = (
            "SELECT Id, AccountId, Name "
            "FROM Opportunity "
            "WHERE AccountId IN ("
            "  SELECT Id FROM Account "
            "  WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "    AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
            f"    {country_sub}"
            ") "
            f"AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
            f"AND ( {cond_name.replace('C_Opportunity_Name__r.', '')} ) "
            "ORDER BY Name"
        )
        recs = tool_salesforce_query(sf, soql).get("records", [])
        acc_ids = sorted({r.get("AccountId") for r in recs if r.get("AccountId")})
        acc_map = _build_account_map(sf, acc_ids) if acc_ids else {}
        for r in recs:
            aid = r.get("AccountId")
            a = acc_map.get(aid, {})
            rows.append({
                "account_id": aid,
                "site": a.get("name"),
                "country": a.get("country"),
                "city": a.get("city"),
                "activity_name": r.get("Name"),
            })
    return _normalize_table_for_ui({
        "columns": [
            {"key":"account_id","label":"Account Id"},{"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"},{"key":"activity_name","label":"Activity"}
        ],
        "rows": rows,
    })


def tool_sites_with_any_activity(
    sf,
    countries: Optional[List[str]] = None,
):
    """Return sites (clinical SubAccounts) that have any Activity via Assignments.
    Uses RT_Activity Opportunities → Assignment__c → Clinical SubAccounts.
    Optional countries filter is sanitized to avoid phrases like 'those activities'.
    """
    if not sf:
        raise HTTPException(400, "No SF session")

    import os, re
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])

    # Get all RT_Activity Opportunities (these are the Activities)
    activity_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    activities = tool_salesforce_query(sf, activity_soql).get("records", [])
    activity_ids = [a.get("Id") for a in activities if a.get("Id")]

    if not activity_ids:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
            ],
            "rows": [],
        })

    # Get Assignments linking Activities to SubAccounts
    activity_id_list = ",".join([f"'{aid}'" for aid in activity_ids])
    assignment_soql = (
        f"SELECT Id, C_Opportunity_Name__c, C_Account__c, C_Opportunity_Name__r.Name "
        f"FROM Assignment__c "
        f"WHERE C_Opportunity_Name__c IN ({activity_id_list}) "
        f"AND C_Account__c != null"
    )
    # Use sf.query_all directly (Assignment__c not in allowed SOQL objects)
    _dbg("[ASSIGNMENT QUERY] %s", assignment_soql)
    assignments = sf.query_all(assignment_soql).get("records", [])

    # Build map: AccountId → [Activity names]
    account_activities: Dict[str, List[str]] = {}
    for asn in assignments:
        acc_id = asn.get("C_Account__c")
        activity_name = (asn.get("C_Opportunity_Name__r") or {}).get("Name") or "Unknown"
        if acc_id:
            if acc_id not in account_activities:
                account_activities[acc_id] = []
            account_activities[acc_id].append(activity_name)

    if not account_activities:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
            ],
            "rows": [],
        })

    # Sanitize countries input: drop generic words and keep plausible names only
    def _clean_countries(arr: Optional[List[str]]) -> List[str]:
        if not arr:
            return []
        bad = {"those","these","this","that","activity","activities","involved","with","the"}
        out: List[str] = []
        for c in arr:
            if not c:
                continue
            t = re.sub(r"\s+", " ", str(c)).strip()
            if not t:
                continue
            # strip quoted activity fragments: e.g., Activity 'Screen' in France -> France
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            # if token contains " in ", take the last segment as country hint
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            # drop tokens that obviously aren't country names
            if any(x in t.lower() for x in ("activity","screen","with","stage")):
                continue
            if t.lower() in bad:
                continue
            if re.search(r"activities?$", t, flags=re.I):
                continue
            out.append(t)
        return out
    safe_countries = _clean_countries(countries)

    # Enrich Accounts (filter by Clinical SubAccount if needed)
    acc_ids = list(account_activities.keys())
    acc_id_list = ",".join([f"'" + str(aid) + "'" for aid in acc_ids])
    country_filter = ""
    if safe_countries:
        country_vals = ", ".join(["'" + str(c).strip().replace("'", "\\'") + "'" for c in safe_countries if str(c).strip()])
        if country_vals:
            country_filter = f" AND ShippingCountry IN ({country_vals})"

    acc_soql = (
        f"SELECT Id, Name, ShippingCity, ShippingCountry "
        f"FROM Account "
        f"WHERE Id IN ({acc_id_list}) "
        f"AND RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
        f"AND (Account_Inactive__c = false OR Account_Inactive__c = null) "
        f"AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
        f"{country_filter}"
    )
    accounts = tool_salesforce_query(sf, acc_soql).get("records", [])

    # Build rows: one row per (site, activity) pair
    rows = []
    for acc in accounts:
        acc_id = acc.get("Id")
        activities_list = account_activities.get(acc_id, [])
        for activity_name in activities_list:
            rows.append({
                "activity_name": activity_name,
                "account_id": acc_id,
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
            })

    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},{"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},{"key":"country","label":"Country"},{"key":"city","label":"City"}
        ],
        "rows": rows,
    })


def tool_list_all_activities(sf, name_like: str = None, account_name_like: str = None):
    """List Activities (RT_Activity/Activity Opportunities).
    If name_like is given, filters by Name LIKE '%name_like%' (up to 2000).
    If account_name_like is given, filters by Account.Name LIKE '%account_name_like%' (sponsor/pharma company).
    Otherwise returns up to 500 alphabetically."""
    if not sf:
        raise HTTPException(400, "No SF session")

    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity","RT_Activity"]
    if "Activity" not in rt_list:
        rt_list.append("Activity")
    if "RT_Activity" not in rt_list:
        rt_list.append("RT_Activity")
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])

    where = f"(RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity')"
    if name_like:
        safe = name_like.replace("'", "\\'")
        where += f" AND Name LIKE '%{safe}%'"
    if account_name_like:
        safe_acc = account_name_like.replace("'", "\\'")
        where += f" AND Account.Name LIKE '%{safe_acc}%'"
    limit = 2000 if (name_like or account_name_like) else 500

    soql = f"SELECT Id, Name, Account.Name FROM Opportunity WHERE {where} ORDER BY Name LIMIT {limit}"
    recs = tool_salesforce_query(sf, soql).get("records", [])

    rows = [
        {
            "activity_name": r.get("Name"),
            "sponsor": (r.get("Account") or {}).get("Name") or "",
            "activity_id": r.get("Id"),
        }
        for r in recs
    ]

    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},
            {"key":"sponsor","label":"Sponsor / Account"},
            {"key":"activity_id","label":"Activity Id"},
        ],
        "rows": rows,
    })


def tool_activities_with_countries(sf):
    """List Activities with the countries that participate (via Assignments to SubAccounts).
    Returns: Opportunity Name, Account Name, Countries (comma-separated), Site Count.
    """
    if not sf:
        raise HTTPException(400, "No SF session")

    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])

    # Get all RT_Activity Opportunities
    activity_soql = f"SELECT Id, Name, Account.Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    activities = tool_salesforce_query(sf, activity_soql).get("records", [])
    activity_ids = [a.get("Id") for a in activities if a.get("Id")]

    if not activity_ids:
        return _normalize_table_for_ui({
            "columns": [
                {"key":"opportunity_name","label":"Opportunity Name"},
                {"key":"account_name","label":"Account Name"},
                {"key":"countries","label":"Countries"},
                {"key":"site_count","label":"Site Count"},
            ],
            "rows": [],
        })

    # Get Assignments linking Activities to SubAccounts
    activity_id_list = ",".join([f"'{aid}'" for aid in activity_ids])
    assignment_soql = (
        f"SELECT C_Opportunity_Name__c, C_Account__c, C_Account__r.ShippingCountry "
        f"FROM Assignment__c "
        f"WHERE C_Opportunity_Name__c IN ({activity_id_list}) "
        f"AND C_Account__c != null"
    )
    # Use sf.query_all directly (Assignment__c not in allowed SOQL objects)
    _dbg("[ASSIGNMENT QUERY] %s", assignment_soql)
    assignments = sf.query_all(assignment_soql).get("records", [])

    # Build map: ActivityId → {countries: set, site_count: int}
    activity_map: Dict[str, Dict[str, Any]] = {}
    for a in activities:
        aid = a.get("Id")
        activity_map[aid] = {
            "opportunity_name": a.get("Name"),
            "account_name": (a.get("Account") or {}).get("Name"),
            "countries": set(),
            "site_count": 0,
        }

    # Aggregate countries and site count per Activity
    for asn in assignments:
        activity_id = asn.get("C_Opportunity_Name__c")
        country = (asn.get("C_Account__r") or {}).get("ShippingCountry")
        if activity_id in activity_map:
            if country:
                activity_map[activity_id]["countries"].add(country)
            activity_map[activity_id]["site_count"] += 1

    # Build rows
    rows = []
    for aid, data in activity_map.items():
        countries_str = ", ".join(sorted(data["countries"])) if data["countries"] else "(no sites)"
        rows.append({
            "opportunity_name": data["opportunity_name"],
            "account_name": data["account_name"],
            "countries": countries_str,
            "site_count": data["site_count"],
            "activity_id": aid,
        })

    # Sort by opportunity name
    rows = sorted(rows, key=lambda x: x.get("opportunity_name", ""))

    return _normalize_table_for_ui({
        "columns": [
            {"key":"opportunity_name","label":"Opportunity Name"},
            {"key":"account_name","label":"Account Name"},
            {"key":"countries","label":"Countries"},
            {"key":"site_count","label":"Site Count"},
            {"key":"activity_id","label":"Activity Id"},
        ],
        "rows": rows,
    })


def tool_activity_counts_by_country(sf):
    """Aggregate number of activities per country (each activity counted once per country)."""
    table = tool_activities_with_countries(sf)
    rows = table.get("rows") or []
    # country -> set(activity_id)
    agg: Dict[str, set] = {}
    for r in rows:
        aid = str(r.get("activity_id") or "")
        cstr = str(r.get("countries") or "")
        if not aid or not cstr or cstr == "(no sites)":
            continue
        for c in [t.strip() for t in cstr.split(",") if t.strip()]:
            agg.setdefault(c, set()).add(aid)
    out_rows = [{"country": k, "activities": len(v)} for k, v in agg.items()]
    out_rows = sorted(out_rows, key=lambda x: x.get("activities", 0), reverse=True)
    return {
        "columns": [{"key":"country","label":"Country"},{"key":"activities","label":"Activities"}],
        "rows": out_rows,
    }


def tool_activity_country_matrix(sf, stacked: bool = False):
    """Per-activity country counts based on Assignment__c.
    If stacked=True, returns one row per (activity,country) with 'sites' (assignments) for stacked bars.
    If stacked=False, returns per-activity totals + country list.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    act_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    acts = tool_salesforce_query(sf, act_soql).get("records", [])
    id_to_name = {a.get("Id"): a.get("Name") for a in acts if a.get("Id")}
    if not id_to_name:
        return {"columns":[],"rows":[]}
    ids_in = ", ".join([f"'{i}'" for i in id_to_name.keys()])
    asn_soql = (
        "SELECT C_Opportunity_Name__c, C_Account__r.ShippingCountry "
        f"FROM Assignment__c WHERE C_Opportunity_Name__c IN ({ids_in}) AND C_Account__c != null"
    )
    recs = sf.query_all(asn_soql).get("records", [])
    # Build counts per (activity,country)
    per_ac: Dict[tuple, int] = {}
    for r in recs:
        aid = r.get("C_Opportunity_Name__c"); c = (r.get("C_Account__r") or {}).get("ShippingCountry")
        if not aid: continue
        key = (aid, c or "")
        per_ac[key] = per_ac.get(key, 0) + 1
    if stacked:
        rows = []
        for (aid, country), cnt in per_ac.items():
            rows.append({
                "activity_name": id_to_name.get(aid) or aid,
                "country": country,
                "sites": cnt,
                "activity_id": aid,
            })
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"country","label":"Country"},{"key":"sites","label":"Sites"},{"key":"activity_id","label":"Activity Id"}],"rows": rows}
    else:
        # Aggregate totals + list of countries
        by_act: Dict[str, Dict[str, Any]] = {}
        for (aid, country), cnt in per_ac.items():
            d = by_act.setdefault(aid, {"activity_name": id_to_name.get(aid) or aid, "countries_count":0, "countries": set(), "activity_id": aid, "sites_total":0})
            d["sites_total"] += cnt
            if country:
                d["countries"].add(country)
                d["countries_count"] = len(d["countries"])
        rows = []
        for aid, d in by_act.items():
            rows.append({
                "activity_name": d["activity_name"],
                "countries_count": d.get("countries_count",0),
                "countries": ", ".join(sorted(d.get("countries", set()))),
                "sites_total": d.get("sites_total",0),
                "activity_id": aid,
            })
        rows.sort(key=lambda x: x.get("sites_total",0), reverse=True)
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"countries_count","label":"Countries"},{"key":"countries","label":"Country List"},{"key":"sites_total","label":"Sites"},{"key":"activity_id","label":"Activity Id"}],"rows": rows}


def tool_activities_with_assignments_counts(
    sf,
    *,
    last_n_days: Optional[int] = None,
    last_n_months: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """List activities with total assignments and participating countries.
    Supports date filters (CreatedDate) via last_n_days/last_n_months or since/until (YYYY-MM-DD).
    Returns columns: activity_name, assignments, countries, activity_id."""
    if not sf:
        raise HTTPException(400, "No SF session")
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    # Fetch Activities
    activity_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals})"
    acts = tool_salesforce_query(sf, activity_soql).get("records", [])
    id_to_name = {a.get("Id"): a.get("Name") for a in acts if a.get("Id")}
    if not id_to_name:
        return {"columns":[{"key":"activity_name","label":"Activity"},{"key":"assignments","label":"Assignments"},{"key":"countries","label":"Countries"}],"rows":[]}
    ids_in = ",".join([f"'{i}'" for i in id_to_name.keys()])
    # Assignments with Account country
    date_cond = ""
    if isinstance(last_n_days, int) and last_n_days > 0:
        date_cond = f" AND CreatedDate = LAST_N_DAYS:{int(last_n_days)}"
    elif isinstance(last_n_months, int) and last_n_months > 0:
        date_cond = f" AND CreatedDate = LAST_N_MONTHS:{int(last_n_months)}"
    else:
        rng = []
        if since:
            rng.append(f"CreatedDate >= {since}T00:00:00Z")
        if until:
            rng.append(f"CreatedDate <= {until}T23:59:59Z")
        if rng:
            date_cond = " AND " + " AND ".join(rng)
    asn_soql = (
        "SELECT C_Opportunity_Name__c, C_Account__r.ShippingCountry, CreatedDate "
        "FROM Assignment__c WHERE C_Opportunity_Name__c IN (" + ids_in + ") AND C_Account__c != null" + date_cond
    )
    recs = sf.query_all(asn_soql).get("records", [])
    agg_counts: Dict[str, int] = {}
    agg_countries: Dict[str, set] = {}
    agg_latest: Dict[str, str] = {}   # latest assignment CreatedDate per activity
    agg_earliest: Dict[str, str] = {} # earliest assignment CreatedDate per activity
    for r in recs:
        oid = r.get("C_Opportunity_Name__c")
        c = (r.get("C_Account__r") or {}).get("ShippingCountry")
        d = (r.get("CreatedDate") or "")[:10]  # YYYY-MM-DD
        if not oid:
            continue
        agg_counts[oid] = agg_counts.get(oid, 0) + 1
        if oid not in agg_countries:
            agg_countries[oid] = set()
        if c:
            agg_countries[oid].add(c)
        if d:
            if oid not in agg_latest or d > agg_latest[oid]:
                agg_latest[oid] = d
            if oid not in agg_earliest or d < agg_earliest[oid]:
                agg_earliest[oid] = d
    has_date_filter = bool(date_cond)
    rows = []
    for oid, cnt in agg_counts.items():
        earliest = agg_earliest.get(oid, "")
        latest   = agg_latest.get(oid, "")
        date_range = (f"{earliest} – {latest}" if earliest and latest and earliest != latest
                      else earliest or latest)
        rows.append({
            "activity_name": id_to_name.get(oid) or oid,
            "assignments": cnt,
            "countries": ", ".join(sorted(list(agg_countries.get(oid) or set()))),
            "date_range": date_range,
            "activity_id": oid,
        })
    rows.sort(key=lambda r: r.get("assignments", 0), reverse=True)
    cols = [
        {"key":"activity_name","label":"Activity"},
        {"key":"assignments","label":"Assignments"},
        {"key":"countries","label":"Countries"},
        {"key":"date_range","label":"Assignment Date Range"},
        {"key":"activity_id","label":"Activity Id"},
    ]
    return {"columns": cols, "rows": rows}


def tool_activity_assignments_detailed(
    sf,
    countries: Optional[List[str]] = None,
    activity_contains: Optional[str] = None,
    *,
    last_n_days: Optional[int] = None,
    last_n_months: Optional[int] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
):
    """Return detailed assignment rows for Activities (RT_Activity), with optional filters:
    - countries: list[str] to filter Account.ShippingCountry IN (...)
    - activity_contains: substring to filter Activity Name (LIKE '%...%')
    - last_n_days / last_n_months / since / until filters on CreatedDate
    Columns: activity_name, opportunity_id, account_id, account_name, country, city, stage, type, assignment_id, created.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    import os, re
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["RT_Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    # Get activity ids (optionally filter by name)
    name_filter = ""
    if activity_contains and str(activity_contains).strip():
        esc = str(activity_contains).strip().replace("'", "\\'")
        name_filter = f" AND Name LIKE '%{esc}%'"
    act_soql = f"SELECT Id, Name FROM Opportunity WHERE RecordType.DeveloperName IN ({rt_vals}){name_filter}"
    acts = tool_salesforce_query(sf, act_soql).get("records", [])
    ids = [a.get("Id") for a in acts if a.get("Id")]
    if not ids:
        return _normalize_table_for_ui({"columns":[{"key":"activity_name","label":"Activity"},{"key":"opportunity_id","label":"Opportunity Id"},{"key":"account_id","label":"Account Id"},{"key":"account_name","label":"Account"},{"key":"country","label":"Country"},{"key":"city","label":"City"},{"key":"stage","label":"Stage"},{"key":"type","label":"Type"},{"key":"assignment_id","label":"Assignment Id"},{"key":"created","label":"Created"}],"rows":[]})
    def _chunks(lst, n=120):
        buf=[]
        for x in lst:
            if x: buf.append(x)
            if len(buf)>=n:
                yield buf; buf=[]
        if buf: yield buf
    # sanitize countries
    def _clean_countries(arr: Optional[List[str]]) -> List[str]:
        bad = {"those","these","this","that","activities","activity","with","involved","the"}
        out=[]
        for c in (arr or []):
            t=str(c or '').strip()
            if not t:
                continue
            # strip quoted activity fragments before country
            t = re.sub(r"(?i)^activity\s+['\"]?.+?['\"]?\s+in\s+", "", t)
            if " in " in t.lower():
                t = t.split(" in ")[-1].strip()
            if t.lower() in bad:
                continue
            if any(x in t.lower() for x in ("activity","screen","stage")):
                continue
            if re.search(r"activities?$", t, flags=re.I):
                continue
            out.append(t)
        return out
    safe_c = _clean_countries(countries)
    rows = []
    for chunk in _chunks(ids):
        ids_in = ", ".join([f"'{i}'" for i in chunk])
        where = [f"C_Opportunity_Name__c IN ({ids_in})", "C_Account__c != null"]
        if safe_c:
            cvals = ", ".join(["'" + s.replace("'","\\'") + "'" for s in safe_c])
            where.append(f"C_Account__r.ShippingCountry IN ({cvals})")
        if activity_contains and str(activity_contains).strip():
            esc = str(activity_contains).strip().replace("'", "\\'")
            where.append(f"C_Opportunity_Name__r.Name LIKE '%{esc}%'")
        # Date filters
        if isinstance(last_n_days, int) and last_n_days > 0:
            where.append(f"CreatedDate = LAST_N_DAYS:{int(last_n_days)}")
        elif isinstance(last_n_months, int) and last_n_months > 0:
            where.append(f"CreatedDate = LAST_N_MONTHS:{int(last_n_months)}")
        else:
            if since:
                where.append(f"CreatedDate >= {since}T00:00:00Z")
            if until:
                where.append(f"CreatedDate <= {until}T23:59:59Z")
        soql = (
            "SELECT Id, Name, C_Account__c, C_Account__r.Name, C_Account__r.ShippingCountry, C_Account__r.ShippingCity, "
            "C_Opportunity_Name__c, C_Opportunity_Name__r.Name, C_Assignment_Stage__c, Assignment_Type__c, CreatedDate "
            "FROM Assignment__c WHERE " + " AND ".join(where) + " ORDER BY CreatedDate DESC"
        )
        recs = sf.query_all(soql).get("records", [])
        for r in recs:
            rows.append({
                "activity_name": (r.get("C_Opportunity_Name__r") or {}).get("Name"),
                "opportunity_id": r.get("C_Opportunity_Name__c"),
                "account_id": r.get("C_Account__c"),
                "account_name": (r.get("C_Account__r") or {}).get("Name"),
                "country": (r.get("C_Account__r") or {}).get("ShippingCountry"),
                "city": (r.get("C_Account__r") or {}).get("ShippingCity"),
                "stage": r.get("C_Assignment_Stage__c"),
                "type": r.get("Assignment_Type__c"),
                "assignment_id": r.get("Id"),
                "created": r.get("CreatedDate"),
            })
    return _normalize_table_for_ui({
        "columns": [
            {"key":"activity_name","label":"Activity"},
            {"key":"opportunity_id","label":"Opportunity Id"},
            {"key":"account_id","label":"Account Id"},
            {"key":"account_name","label":"Account"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":"stage","label":"Stage"},
            {"key":"type","label":"Type"},
            {"key":"assignment_id","label":"Assignment Id"},
            {"key":"created","label":"Created"},
        ],
        "rows": rows,
    })


def tool_activity_sites_by_country(sf):
    if not sf:
        raise HTTPException(400, "No SF session")
    # Get AccountIds of Activities; then enrich Accounts and aggregate in Python
    import os
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
    if not rt_list:
        rt_list = ["Activity", "RT_Activity"]
    if "Activity" not in rt_list: rt_list.append("Activity")
    if "RT_Activity" not in rt_list: rt_list.append("RT_Activity")
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    soql = (
        "SELECT AccountId "
        "FROM Opportunity "
        "WHERE AccountId IN ("
        "  SELECT Id FROM Account "
        "  WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
        "    AND (Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null) "
        ") "
        f"AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
        "AND AccountId != null"
    )
    recs = tool_salesforce_query(sf, soql).get("records", [])
    acc_ids = sorted({r.get("AccountId") for r in recs if r.get("AccountId")})
    acc_map = _build_account_map(sf, acc_ids) if acc_ids else {}
    counts: Dict[str, int] = {}
    for aid in acc_ids:
        country = (acc_map.get(aid) or {}).get("country")
        if not country:
            continue
        counts[country] = counts.get(country, 0) + 1
    rows = [{"country": k, "sites": v} for k, v in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]
    return {"columns": [{"key":"country","label":"Country"},{"key":"sites","label":"Sites"}], "rows": rows}
