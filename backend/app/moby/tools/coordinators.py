"""Study-coordinator tool implementation.

Pure move from `app.routers.ai_chat` (Phase 4 refactor). Behavior is
unchanged. Helpers come from `app.moby.helpers.*`. Re-exported by
`ai_chat.py` so existing call sites and test mocks
(`@patch("app.routers.ai_chat.tool_study_coordinators_with_activities")`)
keep working.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from app.moby.helpers.debug import _dbg
from app.moby.helpers.tables import _normalize_table_for_ui


def tool_study_coordinators_with_activities(
    sf,
    account_ids: Optional[List[str]] = None,
    countries: Optional[List[str]] = None,
    title_contains: Optional[str] = None,
    roles: Optional[List[str]] = None,
    include_subaccounts: bool = False,
):
    """Return one row per (Account × Study Coordinator) with activities list.
    Columns: account_id, site, country, city, sc_name, sc_email, sc_phone, sc_title,
             activities (semicolon-separated), activities_count.
    """
    if not sf:
        raise HTTPException(400, "No SF session")
    # 1) Resolve candidate accounts (clinical subaccounts, active)
    inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
    acc_ids: List[str] = []
    acc_info: Dict[str, Dict[str, Any]] = {}
    parent_ids: set = set()
    if account_ids and len(account_ids):
        acc_ids = list({a for a in account_ids if a})
        if acc_ids:
            in_list = ",".join([f"'{a}'" for a in acc_ids])
            soql_acc = f"SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE Id IN ({in_list})"
            recs = sf.query_all(soql_acc).get("records", [])
            for r in recs:
                acc_info[r.get("Id")] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
    else:
        where = ["RecordType.DeveloperName='SubAccount'", "C_Type__c='Clinical'", inactive_clause]
        if countries:
            san_countries = ["'" + str(c).strip().replace("'", "\\'") + "'" for c in countries if str(c).strip()]
            esc = ", ".join(san_countries)
            if esc:
                where.append(f"ShippingCountry IN ({esc})")
        soql_acc = "SELECT Id, Name, ShippingCity, ShippingCountry, ParentId FROM Account WHERE " + " AND ".join(where) + " LIMIT 2000"
        recs = sf.query_all(soql_acc).get("records", [])
        for r in recs:
            aid = r.get("Id"); acc_ids.append(aid)
            acc_info[aid] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
            if r.get("ParentId"):
                parent_ids.add(r.get("ParentId"))
        # Also fetch parent account info so coordinator rows can display site name/country
        if parent_ids:
            try:
                parent_clause = ", ".join([f"'{p}'" for p in parent_ids])
                soql_par = f"SELECT Id, Name, ShippingCity, ShippingCountry FROM Account WHERE Id IN ({parent_clause})"
                par_recs = sf.query_all(soql_par).get("records", [])
                for r in par_recs:
                    pid = r.get("Id")
                    if pid and pid not in acc_info:
                        acc_info[pid] = {"name": r.get("Name"), "city": r.get("ShippingCity"), "country": r.get("ShippingCountry")}
            except Exception:
                pass
    if not acc_ids:
        return {"columns": [], "rows": []}
    # Build search IDs: SubAccounts + their parent Accounts (contacts may be linked to either)
    _all_search_ids = list(acc_ids) + [p for p in parent_ids if p not in set(acc_ids)]
    ids_clause = ", ".join([f"'{i}'" for i in _all_search_ids])
    # 2) Study Coordinators via ACR and Contact.Title
    roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
    role_list = roles if roles and len(roles) else roles_env
    where_title = f" OR Contact.Title LIKE '%Coordinat%'"
    if title_contains:
        te = title_contains.replace("'", "\\'")
        where_title = f" OR Contact.Title LIKE '%{te}%' {where_title}"
    sc_rows: List[Dict[str, Any]] = []
    # Coordinator patterns (multilingual)
    patterns = [
        "Coordinat",       # Coordinator, Coordinating, Co-ordinator
        "Coördinat",      # Dutch with diaeresis
        "Coordinador",    # ES
        "Coordinateur",   # FR
        "Koordinator",    # DE
    ]
    acr_title_cond = " OR ".join([f"Contact.Title LIKE '%{p}%'" for p in patterns])
    # ACR path 1: by explicit role list
    if role_list:
        role_vals = ", ".join([f"'{r}'" for r in role_list])
        soql = (
            "SELECT AccountId, ContactId, Role__c, Contact.Name, Contact.Email, Contact.Phone, Contact.Title "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals})"
        )
        sc_rows += sf.query_all(soql).get("records", [])
    # ACR path 2: by coordinator title via AccountContactRelation (catches contacts linked via ACR, not just primary AccountId)
    try:
        soql_acr_title = (
            "SELECT AccountId, ContactId, Role__c, Contact.Name, Contact.Email, Contact.Phone, Contact.Title "
            "FROM AccountContactRelation "
            f"WHERE AccountId IN ({ids_clause}) AND ({acr_title_cond})"
        )
        acr_title_recs = sf.query_all(soql_acr_title).get("records", [])
        # Deduplicate against existing sc_rows by ContactId+AccountId
        existing_keys = {(r.get("AccountId"), r.get("ContactId")) for r in sc_rows}
        for r in acr_title_recs:
            k = (r.get("AccountId"), r.get("ContactId"))
            if k not in existing_keys:
                existing_keys.add(k)
                sc_rows.append(r)
    except Exception as _e_acr:
        _dbg("WARN: ACR title query failed: %s", _e_acr)
    # Fallback: Contact by Title (direct AccountId match)
    title_cond = " OR ".join([f"Title LIKE '%{p}%'" for p in patterns])
    soql_c = (
        "SELECT Id, AccountId, Name, Email, Phone, Title FROM Contact "
        f"WHERE AccountId IN ({ids_clause}) AND ({title_cond})"
    )
    if title_contains:
        te = title_contains.replace("'", "\\'")
        soql_c = (
            "SELECT Id, AccountId, Name, Email, Phone, Title FROM Contact "
            f"WHERE AccountId IN ({ids_clause}) AND (Title LIKE '%{te}%' OR {title_cond})"
        )
    sc_contacts = sf.query_all(soql_c).get("records", [])
    # Helper: strict check coordinator by role/title
    def _is_sc(role: Optional[str], title: Optional[str]) -> bool:
        r = (role or "").lower(); t = (title or "").lower()
        positives = ("coordinat", "coördinat", "coordinador", "coordinateur", "koordinator")
        negatives = ("manager", "head", "monitor", "administrator", "admin")
        if any(n in t for n in negatives):
            # if explicitly administrative without coordinator, drop
            if not any(p in t for p in positives):
                return False
        if any(p in r for p in positives):
            return True
        return any(p in t for p in positives)
    # Build per-account coordinators list (with source); deduplicate by contact_id
    sc_by_acc: Dict[str, List[Dict[str, Any]]] = {}
    _seen_cids: set = set()  # (account_id, contact_id) dedup
    for r in sc_rows:
        aid = r.get("AccountId"); c = r.get("Contact") or {}
        cid = r.get("ContactId")
        if _is_sc(r.get("Role__c"), (c or {}).get("Title")):
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": c.get("Name"), "email": c.get("Email"), "phone": c.get("Phone"), "title": c.get("Title"),
                    "sc_source": "acr_role", "sc_role": r.get("Role__c"), "contact_id": cid,
                })
    for r in sc_contacts:
        aid = r.get("AccountId")
        cid = r.get("Id")
        _ctitle = r.get("Title") or ""
        # Include if: explicitly matched by title_contains (e.g. "Investigator"), or passes coordinator check
        _explicit_match = bool(title_contains and title_contains.lower() in _ctitle.lower())
        if _explicit_match or _is_sc(None, _ctitle):
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": r.get("Name"), "email": r.get("Email"), "phone": r.get("Phone"), "title": r.get("Title"),
                    "sc_source": "contact_title", "sc_role": None, "contact_id": cid,
                })
    # Path 3: Lead SC from Opportunity.C_Lead_Study_Coordinator_SC__c (INNODIA-specific field)
    try:
        soql_opp_sc = (
            "SELECT AccountId, C_Lead_Study_Coordinator_SC__c, "
            "C_Lead_Study_Coordinator_SC__r.Name, C_Lead_Study_Coordinator_SC__r.Email, "
            "C_Lead_Study_Coordinator_SC__r.Phone, C_Lead_Study_Coordinator_SC__r.Title "
            f"FROM Opportunity WHERE AccountId IN ({ids_clause}) "
            "AND C_Lead_Study_Coordinator_SC__c != null "
            "LIMIT 2000"
        )
        opp_sc_recs = sf.query_all(soql_opp_sc).get("records", [])
        for r in opp_sc_recs:
            aid = r.get("AccountId")
            sc_contact = r.get("C_Lead_Study_Coordinator_SC__r") or {}
            cid = r.get("C_Lead_Study_Coordinator_SC__c")
            if not cid or not aid:
                continue
            key = (aid, cid)
            if key not in _seen_cids:
                _seen_cids.add(key)
                sc_by_acc.setdefault(aid, []).append({
                    "name": sc_contact.get("Name"), "email": sc_contact.get("Email"),
                    "phone": sc_contact.get("Phone"), "title": sc_contact.get("Title"),
                    "sc_source": "opp_lead_sc", "sc_role": "Lead SC", "contact_id": cid,
                })
    except Exception as _e_opp:
        _dbg("WARN: Opp lead SC query failed: %s", _e_opp)
    # 3) Activities by Account (names only). Accept RecordType OR Type = 'Activity'
    rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
    rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()] or ["Activity"]
    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
    soql_act = (
        "SELECT Id, Name, AccountId, Type, RecordType.DeveloperName FROM Opportunity "
        f"WHERE AccountId IN ({ids_clause}) AND (RecordType.DeveloperName IN ({rt_vals}) OR Type = 'Activity') "
        "ORDER BY CreatedDate DESC LIMIT 5000"
    )
    opps = sf.query_all(soql_act).get("records", [])
    acts_by_acc: Dict[str, List[str]] = {}
    for o in opps:
        aid = o.get("AccountId"); acts_by_acc.setdefault(aid, []).append(o.get("Name"))
    # 4) Build rows: only accounts that have coordinators
    rows_out: List[Dict[str, Any]] = []
    for aid, scs in sc_by_acc.items():
        if not scs: continue
        info = acc_info.get(aid) or {}
        act_names = "; ".join(acts_by_acc.get(aid, [])[:10])
        act_count = len(acts_by_acc.get(aid, []))
        for sc in scs:
            rows_out.append({
                "account_id": aid,
                "site": info.get("name"),
                "country": info.get("country"),
                "city": info.get("city"),
                "sc_name": sc.get("name"),
                "sc_email": sc.get("email"),
                "sc_phone": sc.get("phone"),
                "sc_title": sc.get("title"),
                "sc_source": sc.get("sc_source"),
                "sc_role": sc.get("sc_role"),
                "contact_id": sc.get("contact_id"),
                "activities": act_names,
                "activities_count": act_count,
            })
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":"sc_name","label":"Coordinator"},
            {"key":"sc_email","label":"Email"},
            {"key":"sc_phone","label":"Phone"},
            {"key":"sc_title","label":"Title"},
            {"key":"sc_source","label":"Source"},
            {"key":"sc_role","label":"Role (ACR)"},
            {"key":"contact_id","label":"Contact Id"},
            {"key":"activities","label":"Activities"},
            {"key":"activities_count","label":"Activities Count"},
        ],
        "rows": rows_out,
    }
    return _normalize_table_for_ui(table)
