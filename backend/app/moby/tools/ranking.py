"""Ranking tool implementation (`tool_rank_sites`).

Pure move from `app.routers.ai_chat` (Phase 4 refactor). Behavior is
unchanged. Re-exported by `ai_chat.py` so existing call sites and test
mocks (`@patch("app.routers.ai_chat.tool_rank_sites")`) keep working.
"""
from __future__ import annotations

from typing import Literal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.moby.helpers.labels import _pretty_label
from app.moby.helpers.metrics import _resolve_metric
from app.moby.helpers.tables import _normalize_table_for_ui
from app.moby.tools.aggregates import tool_sql_query
from app.moby.tools.salesforce import tool_salesforce_query


def tool_rank_sites(
    db: Session,
    sf,
    metric: str,
    top_n: int = 5,
    order: Literal["asc", "desc"] = "desc",
):
    """
    Generic Top-N ranking across SF and site_qual.
    Returns a normalized table with account_id, site, country, city and the metric column.
    """
    meta = _resolve_metric(metric, db)
    dir_sql = "ASC" if str(order).lower() == "asc" else "DESC"

    # -- site_qual path (warehouse) --
    if meta.get("source") == "site_qual":
        key = meta.get("key")
        sql = f"""
            SELECT
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(
                    NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric,
                    0
                ) AS metric
            FROM public.sites s
            LEFT JOIN public.site_qual sq ON sq.site_id = s.id
            ORDER BY metric {dir_sql} NULLS LAST
            LIMIT :top_n
        """
        out = tool_sql_query(db, sql, {"key": key, "top_n": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        table = {
            "columns": [
                {"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},
                {"key":"country","label":"Country"},
                {"key":"city","label":"City"},
                {"key":f"qual.{key}","label":_pretty_label(f"qual.{key}")},
            ],
            "rows": [
                {
                    "account_id": r.get("account_id"),
                    "site": r.get("site"),
                    "country": r.get("country"),
                    "city": r.get("city"),
                    f"qual.{key}": r.get("metric")
                } for r in rows
            ],
        }
        return _normalize_table_for_ui(table)
    elif meta.get("source") == "profiling_kv":
        key = meta.get("key")
        sql = f"""
            SELECT
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(
                    NULLIF(regexp_replace(p.value, '[^0-9\\.\\-]', '', 'g'), '')::numeric,
                    0
                ) AS "{key}"
            FROM public.sites s
            LEFT JOIN public.profiling_kv p
              ON p.site_id = s.id AND p.key = :key
            ORDER BY "{key}" {dir_sql} NULLS LAST
            LIMIT :top_n
        """
        out = tool_sql_query(db, sql, {"key": key, "top_n": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        metric_key = f"qual.{key}"
        for r in rows:
            if key in r:
                r[metric_key] = r.pop(key)
        table = {
            "columns": [
                {"key":"account_id","label":"Account Id"},
                {"key":"site","label":"Account Name"},
                {"key":"country","label":"Country"},
                {"key":"city","label":"City"},
                {"key":metric_key,"label":_pretty_label(metric_key)},
            ],
            "rows": rows,
        }
        return _normalize_table_for_ui(table)

    # -- SF path --
    field = meta.get("field")
    if not sf:
        raise HTTPException(400, "No active Salesforce session for SF ranking")
    soql = f"""
        SELECT
            Account.Id,
            Account.Name,
            Account.ShippingCountry,
            Account.ShippingCity,
            MAX({field}) metric
        FROM Opportunity
        WHERE {field} != null
        GROUP BY Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity
        ORDER BY metric {dir_sql} NULLS LAST
        LIMIT {int(top_n)}
    """
    raw = tool_salesforce_query(sf, soql)
    records = raw.get("records", []) if isinstance(raw, dict) else []
    rows = []
    for r in records:
        acc = r.get("Account") or {}
        rows.append({
            "account_id": acc.get("Id"),
            "site": acc.get("Name"),
            "country": acc.get("ShippingCountry"),
            "city": acc.get("ShippingCity"),
            f"sf.{field}": r.get("expr0") or r.get("metric"),
        })
    table = {
        "columns": [
            {"key":"account_id","label":"Account Id"},
            {"key":"site","label":"Account Name"},
            {"key":"country","label":"Country"},
            {"key":"city","label":"City"},
            {"key":f"sf.{field}","label":_pretty_label(f"sf.{field}")},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)
