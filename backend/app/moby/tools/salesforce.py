"""Salesforce-direct tool implementations for Moby.

Pure move from `app.routers.ai_chat` (Phase 2 refactor). Behavior is
unchanged. Each function depends on private helpers that still live in
`ai_chat` (`_dbg`, `_validate_soql`, `_ensure_soql_has_account_id`,
`_sanitize_soql_basic`, `_account_extras_core`, `_pretty_label`,
`tool_sql_query`). To avoid an import cycle at module-load time
(ai_chat re-exports these tool functions back), we resolve those helpers
lazily inside each function via `from app.routers import ai_chat as _ai`.

The functions are re-exported by `ai_chat.py`, so existing tests that
patch `app.routers.ai_chat.tool_salesforce_query` etc. keep working.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from app.moby.helpers.debug import _dbg
from app.moby.helpers.labels import _pretty_label
from app.moby.helpers.soql import _ensure_soql_has_account_id, _sanitize_soql_basic, _validate_soql


from fastapi import HTTPException
from sqlalchemy.orm import Session


def tool_salesforce_query(sf, soql: str):
    from app.routers import ai_chat as _ai
    _dbg("SOQL (raw) >>> %s", soql)
    soql_plus = _ensure_soql_has_account_id(soql)
    fixed = _sanitize_soql_basic(soql_plus)
    if fixed != soql:
        _dbg("SOQL (fixed) >>> %s", fixed)
    _validate_soql(fixed, sf)
    raw = sf.query_all(fixed)
    _dbg("SOQL <<< records=%d", len(raw.get("records", [])) if isinstance(raw, dict) else -1)
    return raw


def tool_salesforce_account_extras(sf, account_id: str):
    from app.routers import ai_chat as _ai
    _dbg("SF extras >>> account_id=%s", account_id)
    if not account_id:
        raise HTTPException(400, "Missing account_id")
    data = _ai._account_extras_core(sf, account_id)
    flat = {
        "account_id": data.get("account_id"),
        "member_name": (data.get("member") or {}).get("name"),
        "pi_name": (data.get("pi") or {}).get("name"),
        "pi_email": (data.get("pi") or {}).get("email"),
        "pi_phone": (data.get("pi") or {}).get("phone"),
        "cs_clinical_site": (data.get("csContribution") or {}).get("INNODIA_Clinical_Trial_Site__c"),
        "cs_referral_outreach": (data.get("csContribution") or {}).get("Referral_Outreach_Site_Non_CTS__c"),
        "cs_eligible_detect": (data.get("csContribution") or {}).get("Elegible_for_DETECT_Site__c"),
        "assignments_count": int(len(data.get("assignments") or [])),
        "new_dx_u18": data.get("newDxUnder18"),
        "new_dx_o18": data.get("newDxOver18"),
    }
    _dbg("SF extras <<< member=%s | PI=%s | assignments=%d | new_u18=%s | new_o18=%s",
             flat.get("member_name"), flat.get("pi_name"),
             flat.get("assignments_count", 0), str(flat.get("new_dx_u18")), str(flat.get("new_dx_o18")))
    return {"columns": list(flat.keys()), "rows": [[flat[k] for k in flat.keys()]]}


def tool_group_count_sf(
    sf,
    by: List[Literal["country", "city"]] = ["country"],
):
    if not sf:
        raise HTTPException(400, "No SF session")
    by = by or ["country"]
    dim = by[0]
    inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
    if dim == "country":
        soql = (
            "SELECT ShippingCountry country, COUNT(Id) "
            "FROM Account "
            "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "AND ShippingCountry != null AND " + inactive_clause + " "
            "GROUP BY ShippingCountry ORDER BY COUNT(Id) DESC"
        )
        raw = tool_salesforce_query(sf, soql)
        recs = raw.get("records", []) if isinstance(raw, dict) else []
        rows = [{"country": r.get("country"), "count": r.get("expr0") or r.get("count") or r.get("COUNT")} for r in recs]
        return {"columns": [{"key": "country", "label": "Country"}, {"key": "count", "label": "Count"}], "rows": rows}
    else:
        soql = (
            "SELECT ShippingCity city, ShippingCountry country, COUNT(Id) "
            "FROM Account "
            "WHERE RecordType.DeveloperName='SubAccount' AND C_Type__c='Clinical' "
            "AND ShippingCity != null AND " + inactive_clause + " "
            "GROUP BY ShippingCity, ShippingCountry ORDER BY COUNT(Id) DESC"
        )
        raw = tool_salesforce_query(sf, soql)
        recs = raw.get("records", []) if isinstance(raw, dict) else []
        rows = [{"city": r.get("city"), "country": r.get("country"), "count": r.get("expr0") or r.get("count")} for r in recs]
        return {"columns": [{"key": "city", "label": "City"}, {"key": "country", "label": "Country"}, {"key": "count", "label": "Count"}], "rows": rows}


def tool_group_agg_sf(
    sf,
    by: List[Literal["country", "city"]] = ["country"],
    field: str = "",
    agg: Literal["sum", "max", "avg"] = "avg",
):
    from app.routers import ai_chat as _ai
    if not sf:
        raise HTTPException(400, "No SF session")
    if not field:
        raise HTTPException(400, "Missing field")
    func = {"sum": "SUM", "max": "MAX", "avg": "AVG"}[agg]
    dim = (by or ["country"])[0]
    grp_field = "Account.ShippingCountry" if dim == "country" else "Account.ShippingCity"
    _validate_soql(f"SELECT {field} FROM Opportunity", sf)
    soql = f"""
        SELECT {grp_field} grp, {func}({field}) value
        FROM Opportunity
        WHERE {field} != null AND {grp_field} != null
        GROUP BY {grp_field}
        ORDER BY value DESC
    """
    raw = tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    out_rows = []
    for r in recs:
        g = r.get("grp") or r.get("expr0")
        val = r.get("value") or r.get("expr1")
        if dim == "country":
            out_rows.append({"country": g, f"sf.{field}": val})
        else:
            out_rows.append({"city": g, f"sf.{field}": val})
    cols = (
        [{"key": "country", "label": "Country"}] if dim == "country" else [{"key": "city", "label": "City"}]
    ) + [{"key": f"sf.{field}", "label": _pretty_label(f"sf.{field}") + f" ({agg})"}]
    return {"columns": cols, "rows": out_rows}


def tool_time_series_sf(
    sf,
    field: str,
    date_field: str = "CloseDate",
    period: Literal["month", "quarter", "year"] = "month",
    agg: Literal["sum", "max", "avg"] = "sum",
    last_n: Optional[int] = None,
):
    from app.routers import ai_chat as _ai
    if not sf:
        raise HTTPException(400, "No SF session")
    func = {"sum": "SUM", "max": "MAX", "avg": "AVG"}[agg]
    per_fn = {"month": "CALENDAR_MONTH", "quarter": "CALENDAR_QUARTER", "year": "CALENDAR_YEAR"}[period]
    where = f"WHERE {field} != null"
    if last_n and period in ("month", "quarter"):
        where += " AND LastModifiedDate = LAST_N_MONTHS:%d" % (last_n if period == "month" else last_n * 3)
    soql = f"""
        SELECT {per_fn}({date_field}) per, {func}({field}) metric
        FROM Opportunity
        {where}
        GROUP BY {per_fn}({date_field})
        ORDER BY {per_fn}({date_field})
    """
    _validate_soql(f"SELECT {date_field} FROM Opportunity", sf)
    raw = tool_salesforce_query(sf, soql)
    recs = raw.get("records", []) if isinstance(raw, dict) else []
    rows = []
    for r in recs:
        rows.append({"period": r.get("expr0") or r.get("per"), f"sf.{field}": r.get("expr1") or r.get("metric")})
    return {"columns": [{"key": "period", "label": "Period"}, {"key": f"sf.{field}", "label": _pretty_label(f"sf.{field}")}], "rows": rows}


def tool_salesforce_account_contacts(sf, account_id: str, include_subaccounts: bool = False, role_contains: Optional[str] = None, roles: Optional[List[str]] = None, title_contains: Optional[str] = None, country: Optional[str] = None, city: Optional[str] = None):
    """Fetch contacts by Account OR by geography when no Account Id is given.
    - If account_id is valid: returns contacts for that Account (and optionally child Accounts).
    - Else if country/city is provided (or a non-Id account_id that looks like a place), search Accounts in that geography
      restricted to Clinical SubAccounts, then return contacts filtered by roles/title.
    Returns a normalized table with account_id, site, contact_id, contact_name, email, phone, title, department, role.
    """
    import os
    from app.routers import ai_chat as _ai
    from app.moby.helpers.tables import _normalize_table_for_ui

    def _list_contacts_for_accounts(ids: List[str]) -> List[Dict[str, Any]]:
        rows_local: List[Dict[str, Any]] = []
        if not ids:
            return rows_local
        ids_clause = ", ".join([f"'{i}'" for i in set(ids)])
        roles_env = [s.strip() for s in (os.environ.get("SF_CONTACT_ROLES", "").split(",")) if s.strip()]
        role_list = roles if roles and len(roles) else roles_env
        if role_list:
            role_vals = ", ".join([f"'{r}'" for r in role_list])
            soql = (
                "SELECT Id, AccountId, ContactId, Role__c, "
                "Contact.Name, Contact.Email, Contact.Phone, Contact.Title, Contact.Department, "
                "Account.Name "
                "FROM AccountContactRelation "
                f"WHERE AccountId IN ({ids_clause}) AND Role__c IN ({role_vals}) "
            )
            if title_contains:
                title_escaped = title_contains.replace("'", "\\'")
                # ampliar variantes: 'Study Coord', 'Coordinator', 'Co-ordinator', prefijo 'Coordinat'
                soql += (
                    f" AND (Contact.Title LIKE '%{title_escaped}%'"
                    f" OR Contact.Title LIKE '%Coordinat%')"
                )
            soql += " ORDER BY AccountId, Contact.Name"
            raw = sf.query_all(soql)
            for r in raw.get("records", []):
                acc = r.get("Account") or {}
                c = r.get("Contact") or {}
                rows_local.append({
                    "account_id": r.get("AccountId"),
                    "site": acc.get("Name"),
                    "contact_id": r.get("ContactId"),
                    "contact_name": c.get("Name"),
                    "email": c.get("Email"),
                    "phone": c.get("Phone"),
                    "title": c.get("Title"),
                    "department": c.get("Department"),
                    "role": r.get("Role__c"),
                })
            # Fallback/Union: si pedimos por título, incluye también Contact por Title
            if title_contains:
                fields = ["Id", "Name", "Email", "Phone", "Title", "Department", "AccountId", "Account.Name"]
                tc = title_contains.replace("'", "\'")
                where = f"AccountId IN ({ids_clause}) AND Title LIKE '%{tc}%'"
                soql2 = f"SELECT {', '.join(fields)} FROM Contact WHERE {where} ORDER BY AccountId, Name"
                raw2 = sf.query_all(soql2)
                for r in raw2.get("records", []):
                    acc = r.get("Account") or {}
                    rows_local.append({
                        "account_id": r.get("AccountId"),
                        "site": acc.get("Name"),
                        "contact_id": r.get("Id"),
                        "contact_name": r.get("Name"),
                        "email": r.get("Email"),
                        "phone": r.get("Phone"),
                        "title": r.get("Title"),
                        "department": r.get("Department"),
                    })
        else:
            fields = ["Id", "Name", "Email", "Phone", "Title", "Department", "AccountId", "Account.Name"]
            where = f"AccountId IN ({ids_clause})"
            if role_contains:
                rc = role_contains.replace("'", "\'")
                where += f" AND (Title LIKE '%{rc}%' OR Department LIKE '%{rc}%')"
            if title_contains:
                tc = title_contains.replace("'", "\'")
                where += f" AND (Title LIKE '%{tc}%' OR Title LIKE '%Coordinat%')"
            soql = f"SELECT {', '.join(fields)} FROM Contact WHERE {where} ORDER BY AccountId, Name"
            raw = sf.query_all(soql)
            for r in raw.get("records", []):
                acc = r.get("Account") or {}
                rows_local.append({
                    "account_id": r.get("AccountId"),
                    "site": acc.get("Name"),
                    "contact_id": r.get("Id"),
                    "contact_name": r.get("Name"),
                    "email": r.get("Email"),
                    "phone": r.get("Phone"),
                    "title": r.get("Title"),
                    "department": r.get("Department"),
                })
        # De-duplicar por contact_id
        uniq = {}
        for r in rows_local:
            cid = r.get("contact_id") or r.get("ContactId") or r.get("Id")
            uniq[cid or len(uniq)] = r
        return list(uniq.values())

    rows: List[Dict[str, Any]] = []

    # Path A: valid Account Id
    if account_id and _ai._is_valid_sf_id(account_id, prefix="001"):
        account_ids = [account_id]
        try:
            if include_subaccounts:
                rt_sub_cfg = os.environ.get("SF_ACCOUNT_RT_SUB", "SubAccount").strip()
                rt_list = [s.strip() for s in rt_sub_cfg.split(",") if s.strip()]
                if not rt_list:
                    rt_list = ["SubAccount"]
                if len(rt_list) == 1:
                    rt_clause = f"RecordType.DeveloperName = '{rt_list[0]}'"
                else:
                    rt_vals = ", ".join([f"'{x}'" for x in rt_list])
                    rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
                acct_type_field = os.environ.get("SF_ACCOUNT_TYPE_FIELD", "C_Type__c").strip() or "C_Type__c"
                clinical_val = os.environ.get("SF_ACCOUNT_TYPE_CLINICAL", "Clinical").strip() or "Clinical"
                soql_children = (
                    "SELECT Id FROM Account "
                    f"WHERE ParentId = '{account_id}' AND {rt_clause} "
                    f"AND {acct_type_field} = '{clinical_val}'"
                )
                res = sf.query_all(soql_children)
                for rec in res.get("records", []):
                    cid = rec.get("Id")
                    if cid:
                        account_ids.append(cid)
        except Exception as e:
            _dbg("WARN: listing subaccounts failed: %s", e)
        rows = _list_contacts_for_accounts(account_ids)
    else:
        # Path B: Geography search (country/city) when no valid Account Id
        geo_country = (country or "").strip() or (account_id if account_id and len(account_id) > 2 else "")
        geo_city = (city or "").strip()
        rt_sub_cfg = os.environ.get("SF_ACCOUNT_RT_SUB", "SubAccount").strip()
        rt_list = [s.strip() for s in rt_sub_cfg.split(",") if s.strip()] or ["SubAccount"]
        if len(rt_list) == 1:
            rt_clause = f"RecordType.DeveloperName = '{rt_list[0]}'"
        else:
            rt_vals = ", ".join([f"'{x}'" for x in rt_list])
            rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
        acct_type_field = os.environ.get("SF_ACCOUNT_TYPE_FIELD", "C_Type__c").strip() or "C_Type__c"
        clinical_val = os.environ.get("SF_ACCOUNT_TYPE_CLINICAL", "Clinical").strip() or "Clinical"
        inactive_clause = "(Account_Inactive__c = false OR Account_Inactive__c = null) AND (Subaccount_Inactive__c = false OR Subaccount_Inactive__c = null)"
        where_parts = [rt_clause, f"{acct_type_field} = '{clinical_val}'", inactive_clause]
        if geo_country:
            gc = geo_country.replace("'", "\'")
            where_parts.append(f"ShippingCountry = '{gc}'")
        if geo_city:
            gcity = geo_city.replace("'", "\'")
            where_parts.append(f"ShippingCity = '{gcity}'")
        if not geo_country and not geo_city:
            # Global search across all clinical subaccounts (limited)
            soql_accounts = "SELECT Id FROM Account WHERE " + " AND ".join(where_parts) + " LIMIT 1000"
        else:
            soql_accounts = "SELECT Id FROM Account WHERE " + " AND ".join(where_parts) + " LIMIT 500"
        acc_res = sf.query_all(soql_accounts)
        acc_ids = [r.get("Id") for r in acc_res.get("records", []) if r.get("Id")]
        rows = _list_contacts_for_accounts(acc_ids)

    # Enrich with Activities per Account (Activity Opportunities)
    try:
        acc_ids = sorted({str(r.get("account_id")) for r in rows if r.get("account_id")})
        if acc_ids:
            def _chunks(xs, n=120):
                buf=[]
                for x in xs:
                    if x: buf.append(x)
                    if len(buf)>=n:
                        yield buf; buf=[]
                if buf: yield buf
            act_by_acc: Dict[str, List[str]] = {}
            for ch in _chunks(acc_ids):
                ids_in = ", ".join(f"'{x}'" for x in ch)
                soql = (
                    "SELECT AccountId, Name FROM Opportunity "
                    f"WHERE AccountId IN ({ids_in}) AND (RecordType.DeveloperName = 'Activity' OR Type = 'Activity')"
                )
                res = sf.query_all(soql)
                for r0 in res.get("records", []):
                    aid = str(r0.get("AccountId") or "")
                    nm = (r0.get("Name") or "").strip()
                    if not aid or not nm:
                        continue
                    lst = act_by_acc.setdefault(aid, [])
                    if nm in lst or len(lst) >= 15:
                        continue
                    lst.append(nm)
            if act_by_acc:
                for r in rows:
                    aid = str(r.get("account_id") or "")
                    names = act_by_acc.get(aid) or []
                    if names:
                        r["activities_names"] = "; ".join(names)
                        r["activities_count"] = len(names)
    except Exception as _e:
        pass

    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"contact_id","label":"Contact Id"},
            {"key":"contact_name","label":"Contact Name"},
            {"key":"email","label":"Email"},
            {"key":"phone","label":"Phone"},
            {"key":"title","label":"Title"},
            {"key":"department","label":"Department"},
            {"key":"role","label":"Role"},
            {"key":"activities_names","label":"Activities"},
            {"key":"activities_count","label":"Activities Count"},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)


def tool_salesforce_assignments(sf, account_ids: List[str], active_only: bool = False, last_n_months: Optional[int] = None):
    """Return assignments per Account using the existing extras core.
    Filters: active_only by simple heuristic on stage; last_n_months by created date when available.
    Also falls back to Opportunities with RecordType.DeveloperName='Activity' when the Assignment__c object is not populated.
    """
    from app.routers import ai_chat as _ai
    from app.moby.helpers.tables import _normalize_table_for_ui
    if not account_ids:
        raise HTTPException(400, "Missing account_ids")
    out_rows: List[Dict[str, Any]] = []
    from datetime import datetime, timedelta
    cutoff = None
    if last_n_months and last_n_months > 0:
        cutoff = datetime.utcnow() - timedelta(days=int(last_n_months)*30)
    for aid in account_ids:
        acc_name: Optional[str] = None
        try:
            data = _ai._account_extras_core(sf, aid)
            acc_name = (data.get("member") or {}).get("name") or data.get("account_name")
            assignments = data.get("assignments") or []
            for a in assignments:
                name = a.get("name") or a.get("opportunity_name") or a.get("id")
                stage = a.get("stage") or a.get("type") or ""
                created = a.get("created")
                if cutoff and created:
                    try:
                        dt = datetime.fromisoformat(str(created).replace("Z","+00:00"))
                        if dt < cutoff:
                            continue
                    except Exception:
                        pass
                if active_only and isinstance(stage, str) and stage:
                    if any(s in stage.lower() for s in ("closed", "won", "lost", "inactive")):
                        continue
                out_rows.append({
                    "account_id": aid,
                    "site": acc_name,
                    "assignment_name": name,
                    "stage": a.get("stage"),
                    "type": a.get("type"),
                    "opportunity_name": a.get("opportunity_name"),
                    "created": created,
                })
        except Exception as e:
            _dbg("WARN: extras for %s failed: %s", aid, e)
        # Fallback: Opportunities with RecordType 'Activity'
        try:
            fields = ["Id","Name","StageName","Type","CreatedDate","RecordType.DeveloperName"]
            import os
            rt_cfg = os.environ.get("SF_RT_ACTIVITY", "Activity,RT_Activity").strip()
            rt_list = [s.strip() for s in rt_cfg.split(",") if s.strip()]
            # Ensure both Activity and RT_Activity are always included
            base = {"Activity", "RT_Activity"}
            rt_list = list(base | set(rt_list))
            rt_vals = ", ".join([f"'{x}'" for x in rt_list])
            rt_clause = f"RecordType.DeveloperName IN ({rt_vals})"
            soql = (
                f"SELECT {', '.join(fields)} FROM Opportunity "
                f"WHERE AccountId = '{aid}' AND {rt_clause} "
                f"ORDER BY CreatedDate DESC LIMIT 200"
            )
            recs = sf.query_all(soql).get("records", [])
            for r in recs:
                created = r.get("CreatedDate")
                if cutoff and created:
                    try:
                        dt = datetime.fromisoformat(str(created).replace("Z","+00:00"))
                        if dt < cutoff:
                            continue
                    except Exception:
                        pass
                stage = r.get("StageName")
                if active_only and isinstance(stage, str) and stage:
                    if any(s in stage.lower() for s in ("closed", "won", "lost", "inactive")):
                        continue
                out_rows.append({
                    "account_id": aid,
                    "site": acc_name,
                    "assignment_name": r.get("Name"),
                    "stage": stage,
                    "type": r.get("Type"),
                    "opportunity_name": r.get("Name"),
                    "created": created,
                })
        except Exception as e:
            _dbg("WARN: activity opp fallback failed for %s: %s", aid, e)
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"assignment_name","label":"Assignment"},
            {"key":"stage","label":"Stage"},
            {"key":"type","label":"Type"},
            {"key":"opportunity_name","label":"Opportunity"},
            {"key":"created","label":"Created"},
        ],
        "rows": out_rows,
    }
    return _normalize_table_for_ui(table)


def tool_sql_query_fill_sf(
    db: Session,
    sf,
    sql: str,
    account_fields: List[str],
    params: Optional[Dict[str, Any]] = None,
):
    """Ejecuta SQL (debe devolver account_id) y rellena columnas Account.* desde SF en lote."""
    from app.routers import ai_chat as _ai
    base = _ai.tool_sql_query(db, sql, params or {})
    cols = base.get("columns") or []
    rows = [{cols[i]: v for i, v in enumerate(r)} for r in base.get("rows") or []]
    ids = list({str(r.get("account_id")) for r in rows if r.get("account_id")})
    if sf and ids and account_fields:
        fields = [f for f in account_fields if f and f != "Id"]
        ids_clause = ', '.join([f"'{i}'" for i in ids])
        soql = f"SELECT Id, {', '.join(fields)} FROM Account WHERE Id IN ({ids_clause})"
        accs = tool_salesforce_query(sf, soql).get("records", [])
        m = {a.get("Id"): a for a in accs}
        for r in rows:
            aid = str(r.get("account_id") or "")
            a = m.get(aid) or {}
            for f in fields:
                r[f"sf.Account.{f}"] = a.get(f)
    return {"columns": [{"key": k, "label": _pretty_label(k)} for k in (list(rows[0].keys()) if rows else cols)], "rows": rows}
