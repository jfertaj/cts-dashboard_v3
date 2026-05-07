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
