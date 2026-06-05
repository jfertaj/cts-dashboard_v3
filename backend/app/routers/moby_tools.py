"""
moby_tools.py — Tool dispatch registry for Moby AI chat.

Extracted from the if/elif chain in ai_chat.py.  Each tool handler is
registered via @register_tool(name) and looked up at dispatch time
through TOOL_DISPATCH.

This is a *pure refactor* — every handler behaves identically to the
original inline code.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback

# Ensure "backend" package is importable whether run from repo root (tests)
# or from backend/ dir (uvicorn).  Without this, `from app.routers...`
# fails when uvicorn starts from backend/.
_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_repo_root = os.path.dirname(_backend_dir)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Shared state passed to every tool handler."""
    db: Session
    request: Request
    sf: Any                          # simple_salesforce.Salesforce | None
    last_table: Optional[Dict[str, Any]]
    last_visualization: Optional[Dict[str, Any]]
    last_explorer_filters: Optional[Dict[str, Any]]
    msgs: List[Dict[str, Any]]       # mutable — handlers append to it
    args: Dict[str, Any]
    tool_call_id: str
    # Needed by list_activities handler (mirrors original bug: uses
    # `tool_args` which is undefined in original code — falls through
    # to None via `(tool_args or {})`)
    tool_args: Optional[Dict[str, Any]] = None


@dataclass
class ToolResult:
    """Return value from a tool handler."""
    last_table: Optional[Dict[str, Any]] = None
    last_visualization: Optional[Dict[str, Any]] = None
    last_explorer_filters: Optional[Dict[str, Any]] = None
    # If True, the handler already appended the tool msg to ctx.msgs
    handled: bool = True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOL_DISPATCH: Dict[str, Callable[[ToolContext], ToolResult]] = {}


def register_tool(name: str):
    """Decorator that registers a tool handler under *name*."""
    def decorator(fn: Callable[[ToolContext], ToolResult]):
        TOOL_DISPATCH[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Lazy import helpers — we import from ai_chat at call time to avoid
# circular imports at module load.
# ---------------------------------------------------------------------------

def _ai():
    """Lazy import of ai_chat module."""
    from app.routers import ai_chat
    return ai_chat


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

@register_tool("sql_query")
def handle_sql_query(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_sql_query, _dbg, _INDEX_CACHE,
    )
    result = ToolResult()
    try:
        _sql_raw = ctx.args.get("sql", "")
        _is_schema_query = bool(re.search(
            r"jsonb_object_keys|information_schema|pg_catalog|SHOW TABLES|DESCRIBE\s",
            _sql_raw, re.I))
        if not _is_schema_query:
            result.last_table = None
            result.last_visualization = None
        out = tool_sql_query(ctx.db, _sql_raw, ctx.args.get("params") or {})
        cols = out.get("columns") or []
        dict_rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        if not _is_schema_query:
            result.last_table = {"columns": [{"key": c, "label": c} for c in cols], "rows": dict_rows}
        # Enrich account_id by site name
        try:
            rows0 = (result.last_table.get("rows") or []) if result.last_table else []
            name_keys = [
                "site", "sites.name", "s.name", "sf.Account.Name", "Account.Name", "name", "account_name"
            ]
            wanted = {str(r[k]) for r in rows0 for k in name_keys
                      if isinstance(r, dict) and r.get(k)}
            if wanted:
                q = text("""
                    SELECT name, salesforce_account_id
                    FROM public.sites
                    WHERE name = ANY(:names)
                """)
                res = ctx.db.execute(q, {"names": list(wanted)})
                mapping = {row[0]: row[1] for row in res.fetchall() if row[1]}
                changed = False
                for r in rows0:
                    if not isinstance(r, dict):
                        continue
                    if "account_id" not in r or not r.get("account_id"):
                        nm = next((r.get(k) for k in name_keys if r.get(k)), None)
                        acc = mapping.get(str(nm)) if nm is not None else None
                        if acc:
                            r["account_id"] = acc
                            r.setdefault("sf.Account.Id", acc)
                            changed = True
                if changed:
                    col_keys = [c.get("key") for c in result.last_table.get("columns", [])]
                    if "account_id" not in col_keys:
                        result.last_table["columns"].insert(0, {"key": "account_id", "label": "Account Id"})
        except Exception as _e:
            _dbg("WARN: enrich account_id by site-name failed: %s", _e)
        _dbg("SQL RESULT: %s", json.dumps(out, default=str)[:500])
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(out, default=str)})
    except Exception as e:
        _dbg("SQL TOOL ERROR: %s\n%s", e, traceback.format_exc())
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(e)})})
    return result


@register_tool("salesforce_query")
def handle_salesforce_query(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_salesforce_query, tool_explorer_search,
        _validate_soql, _normalize_table_for_ui, _pretty_label,
        _dbg, _INDEX_CACHE,
    )
    result = ToolResult()
    result.last_table = None
    result.last_visualization = None
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            soql_orig = ctx.args.get("soql", "")
            _stage12_pat = r"Stage[_\s]?[12][_\s]?Individuals|Individuals[_\s]+with[_\s]+Stage[_\s]?[12]|Stage[12]_Individuals"
            if re.search(_stage12_pat, soql_orig, re.I):
                _dbg("SOQL-REDIRECT: Stage1/2 query → redirecting to explorer_search")
                _s12_out = tool_explorer_search(
                    ctx.request,
                    filters={"logic": "OR", "rules": [
                        {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": 0},
                        {"field": "sf.C_Number_of_Stage2_Individuals_followed__c", "operator": ">", "value": 0},
                    ]},
                    columns=["sf.C_Number_of_Stage1_Individuals_followed__c",
                             "sf.C_Number_of_Stage2_Individuals_followed__c"],
                )
                result.last_table = {"columns": _s12_out.get("columns") or [], "rows": _s12_out.get("rows") or []}
                result.last_table = _normalize_table_for_ui(result.last_table)
                ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                                 "content": json.dumps(_s12_out, default=str)})
            else:
                _validate_soql(soql_orig)
                raw = tool_salesforce_query(ctx.sf, soql_orig)
                records = raw.get("records", []) if isinstance(raw, dict) else []
                flat_rows, keys = [], set()
                soql_context = {}
                alias_matches = re.findall(r"\b(AVG|COUNT|SUM|MIN|MAX)\([^)]+\)\s+(\w+)", soql_orig, re.I)
                for agg_func, alias_name in alias_matches:
                    alias_lower = alias_name.lower()
                    if alias_lower in ("total", "count"):
                        soql_context[alias_name] = "Total"
                    elif alias_lower == "sites":
                        soql_context[alias_name] = "Sites"
                    elif alias_lower == "country":
                        soql_context[alias_name] = "Country"
                    elif alias_lower == "avg" or agg_func.upper() == "AVG":
                        if "T1D" in soql_orig and "U_18" in soql_orig:
                            soql_context[alias_name] = "Average T1D Patients <18"
                        elif "T1D" in soql_orig and "O_18" in soql_orig:
                            soql_context[alias_name] = "Average T1D Patients ≥18"
                        else:
                            soql_context[alias_name] = "Average"
                if "AVG(" in soql_orig and "T1D" in soql_orig and "U_18" in soql_orig and "expr0" not in soql_context:
                    soql_context["expr0"] = "Average T1D Patients <18"
                elif "AVG(" in soql_orig and "T1D" in soql_orig and "O_18" in soql_orig and "expr0" not in soql_context:
                    soql_context["expr0"] = "Average T1D Patients ≥18"
                elif "AVG(" in soql_orig and "T1D" in soql_orig and "expr0" not in soql_context:
                    soql_context["expr0"] = "Average T1D Patients"
                elif "COUNT(" in soql_orig and "expr0" not in soql_context:
                    soql_context["expr0"] = "Total Count"
                for r in records:
                    flat = {}
                    for k, v in r.items():
                        if k == "attributes":
                            continue
                        if isinstance(v, dict) and "attributes" in v:
                            for kk, vv in v.items():
                                if kk == "attributes":
                                    continue
                                flat[f"sf.Account.{kk}"] = vv
                                keys.add(f"sf.Account.{kk}")
                        else:
                            flat[f"sf.{k}"] = v
                            keys.add(f"sf.{k}")
                    flat_rows.append(flat)
                sf_fields_map = _INDEX_CACHE.get("sf_fields") or {}
                ordered = sorted(keys)
                cols = []
                for k in ordered:
                    bare_key = k.replace("sf.", "")
                    if bare_key.lower() in soql_context:
                        label = soql_context[bare_key.lower()]
                    else:
                        label = _pretty_label(k)
                    cols.append({"key": k, "label": label})
                result.last_table = {"columns": cols, "rows": flat_rows}
                result.last_table = _normalize_table_for_ui(result.last_table)
                ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                                 "content": json.dumps(raw, default=str)})
    except Exception as e:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(e)})})
    return result


# soql_query is the new TOOLS_SPEC name; salesforce_query handler stays registered
TOOL_DISPATCH["soql_query"] = TOOL_DISPATCH["salesforce_query"]


@register_tool("salesforce_account_extras")
def handle_salesforce_account_extras(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_salesforce_account_extras
    result = ToolResult()
    if not ctx.sf:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": "No active Salesforce session"})})
    else:
        out = tool_salesforce_account_extras(ctx.sf, ctx.args.get("account_id", ""))
        cols = out.get("columns") or []
        dict_rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        result.last_table = {"columns": [{"key": c, "label": c} for c in cols], "rows": dict_rows}
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(out, default=str)})
    return result


@register_tool("explorer_set_filters")
def handle_explorer_set_filters(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_explorer_set_filters
    result = ToolResult()
    out = tool_explorer_set_filters(ctx.request, ctx.args)
    ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                     "content": json.dumps(out)})
    return result


@register_tool("salesforce_account_contacts")
def handle_salesforce_account_contacts(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_salesforce_account_contacts, _is_valid_sf_id, _first_account_id_from_table,
    )
    result = ToolResult()
    if not ctx.sf:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": "No active Salesforce session"})})
    else:
        acc_arg = ctx.args.get("account_id", "") or ""
        acc_id = acc_arg.strip()
        if not _is_valid_sf_id(acc_id, prefix="001"):
            inferred = _first_account_id_from_table(ctx.last_table)
            if _is_valid_sf_id(inferred, prefix="001"):
                acc_id = inferred
            else:
                ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                                 "content": json.dumps({"error": "Missing or invalid account_id"})})
                return result
        out = tool_salesforce_account_contacts(
            ctx.sf,
            acc_id,
            bool(ctx.args.get("include_subaccounts") or False),
            ctx.args.get("role_contains"),
            ctx.args.get("roles"),
            ctx.args.get("title_contains"),
            ctx.args.get("country"),
            ctx.args.get("city"),
        )
        result.last_table = out
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    return result


@register_tool("salesforce_assignments")
def handle_salesforce_assignments(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_salesforce_assignments
    result = ToolResult()
    if not ctx.sf:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": "No active Salesforce session"})})
    else:
        out = tool_salesforce_assignments(
            ctx.sf,
            ctx.args.get("account_ids") or [],
            bool(ctx.args.get("active_only") or False),
            ctx.args.get("last_n_months"),
        )
        result.last_table = out
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    return result


@register_tool("group_count")
def handle_group_count(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_group_count, tool_group_count_agg, tool_group_count_sf
    result = ToolResult()
    try:
        by = ctx.args.get("by") or ["country"]
        aggregation = ctx.args.get("aggregation") or ctx.args.get("agg") or "count"
        source = ctx.args.get("source") or "explorer"

        if source == "salesforce":
            # Delegate to SF-based counting
            out_table = tool_group_count_sf(ctx.sf, by)
        elif aggregation in ("sum", "avg", "ratio", "ratio_exists"):
            # Delegate to aggregation handler
            agg_val = "ratio_exists" if aggregation == "ratio" else aggregation
            out_table = tool_group_count_agg(
                ctx.db,
                by,
                ctx.args.get("metric"),
                agg_val,
            )
        else:
            # Default: simple count
            out_table = tool_group_count(
                ctx.db,
                by,
                ctx.args.get("where") or {},
            )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result

# Keep old names as aliases
TOOL_DISPATCH["group_count_agg"] = TOOL_DISPATCH["group_count"]
TOOL_DISPATCH["group_count_sf"] = TOOL_DISPATCH["group_count"]


@register_tool("list_activities")
def handle_list_activities(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_list_all_activities
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            # Original code uses `tool_args` (undefined variable) — preserved as-is
            _la_acc = (ctx.tool_args or {}).get("account_name_like")
            out_table = tool_list_all_activities(ctx.sf, account_name_like=_la_acc or None)
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activities_with_countries")
def handle_activities_with_countries(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_activities_with_countries
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            out_table = tool_activities_with_countries(ctx.sf)
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activity_counts_by_country")
def handle_activity_counts_by_country(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_activity_counts_by_country
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            out_table = tool_activity_counts_by_country(ctx.sf)
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activity_country_matrix")
def handle_activity_country_matrix(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_activity_country_matrix
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            out_table = tool_activity_country_matrix(ctx.sf, bool(ctx.args.get("stacked") or False))
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activities_sites")
def handle_activities_sites(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_sites_with_any_activity
    result = ToolResult()
    try:
        out_table = tool_sites_with_any_activity(
            ctx.sf,
            ctx.args.get("countries") or [],
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activities_by_name")
def handle_activities_by_name(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_sites_by_activity
    result = ToolResult()
    try:
        out_table = tool_sites_by_activity(
            ctx.sf,
            ctx.args.get("name", ""),
            ctx.args.get("countries") or [],
            bool(ctx.args.get("exact") or False),
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("sf_aggregate")
def handle_sf_aggregate(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_group_agg_sf, tool_time_series_sf
    result = ToolResult()
    try:
        mode = ctx.args.get("mode") or "aggregate"
        if mode == "time_series":
            out_table = tool_time_series_sf(
                ctx.sf,
                ctx.args.get("field", ""),
                ctx.args.get("date_field") or "CloseDate",
                ctx.args.get("period") or "month",
                ctx.args.get("agg") or "sum",
                ctx.args.get("last_n"),
            )
        else:
            out_table = tool_group_agg_sf(
                ctx.sf,
                ctx.args.get("by") or ["country"],
                ctx.args.get("field") or "",
                ctx.args.get("agg") or "avg",
            )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result

# Keep old names as aliases
TOOL_DISPATCH["group_agg_sf"] = TOOL_DISPATCH["sf_aggregate"]
TOOL_DISPATCH["time_series_sf"] = TOOL_DISPATCH["sf_aggregate"]


@register_tool("activities_with_assignments_counts")
def handle_activities_with_assignments_counts(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_activities_with_assignments_counts
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            out_table = tool_activities_with_assignments_counts(
                ctx.sf,
                last_n_days=ctx.args.get("last_n_days"),
                last_n_months=ctx.args.get("last_n_months"),
                since=ctx.args.get("since"),
                until=ctx.args.get("until"),
            )
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("activity_assignments_detailed")
def handle_activity_assignments_detailed(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_activity_assignments_detailed
    result = ToolResult()
    try:
        if not ctx.sf:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": "No active Salesforce session"})})
        else:
            out_table = tool_activity_assignments_detailed(
                ctx.sf,
                ctx.args.get("countries") or [],
                ctx.args.get("activity_contains"),
                last_n_days=ctx.args.get("last_n_days"),
                last_n_months=ctx.args.get("last_n_months"),
                since=ctx.args.get("since"),
                until=ctx.args.get("until"),
            )
            result.last_table = out_table
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("sql_query_fill_sf")
def handle_sql_query_fill_sf(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_sql_query_fill_sf
    result = ToolResult()
    try:
        out_table = tool_sql_query_fill_sf(
            ctx.db,
            ctx.sf,
            ctx.args.get("sql", ""),
            ctx.args.get("account_fields") or [],
            ctx.args.get("params") or {},
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("contacts_by_group")
def handle_contacts_by_group(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_contacts_by_group
    result = ToolResult()
    try:
        out_table = tool_contacts_by_group(
            ctx.sf,
            ctx.args.get("roles") or [],
            ctx.args.get("title_contains"),
            ctx.args.get("group_by") or "country",
            int(ctx.args.get("top_n") or 1),
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("study_coordinators_with_activities")
def handle_study_coordinators_with_activities(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_study_coordinators_with_activities
    result = ToolResult()
    try:
        out_table = tool_study_coordinators_with_activities(
            ctx.sf,
            ctx.args.get("account_ids") or [],
            ctx.args.get("countries") or [],
            ctx.args.get("title_contains"),
            ctx.args.get("roles") or [],
            bool(ctx.args.get("include_subaccounts") or False),
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("assignment_contact_report")
def handle_assignment_contact_report(ctx: ToolContext) -> ToolResult:
    from app.services.assignment_report import AssignmentFilters, fetch_report
    result = ToolResult()
    if not ctx.sf:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": "No active Salesforce session"})})
        return result
    a = ctx.args or {}
    f = AssignmentFilters(
        studies=a.get("studies") or [],
        stages=a.get("stages") or [],
        referral_only=bool(a.get("referral_only") or False),
        roles=a.get("roles") or [],
        exclude_countries=a.get("exclude_countries") or [],
        include_countries=a.get("include_countries") or [],
    )
    try:
        result.last_table = fetch_report(ctx.sf, f)
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True, "rows": len(result.last_table["rows"])})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("qual_search")
def handle_qual_search(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_qual_search
    result = ToolResult()
    try:
        out_table = tool_qual_search(
            ctx.db,
            ctx.args.get("text", ""),
            int(ctx.args.get("limit") or 50),
        )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("manipulate_data")
def handle_manipulate_data(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_manipulate_data, tool_render_chart, _dbg
    result = ToolResult()
    # Carry forward current state — manipulate_data may update both
    result.last_table = ctx.last_table
    result.last_visualization = ctx.last_visualization
    try:
        manipulated = tool_manipulate_data(
            ctx.last_table,
            ctx.args.get("operation", "group_others"),
            ctx.args.get("threshold"),
            ctx.args.get("value_column"),
            ctx.args.get("label_column"),
        )
        if "rows" in manipulated:
            result.last_table = manipulated
            _dbg("Updated last_table with manipulated data")
            try:
                if result.last_visualization and isinstance(result.last_visualization, dict):
                    vtype = result.last_visualization.get("type") or "bar"
                    xk = result.last_visualization.get("xKey") or manipulated.get("label_column")
                    yks = result.last_visualization.get("yKeys") or ([manipulated.get("value_column")] if manipulated.get("value_column") else [])
                    data = manipulated.get("rows") or (result.last_table.get("rows") if isinstance(result.last_table, dict) else [])
                    out_v = tool_render_chart(vtype, data, xk or "country", yks or [])
                    result.last_visualization = out_v.get("visualization")
                    _dbg("Re-rendered chart after manipulate_data with same axes")
            except Exception as _re:
                _dbg("WARN: could not re-render chart: %s", _re)
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(manipulated)})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("render_chart")
def handle_render_chart(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_render_chart, _pretty_label, _dbg
    result = ToolResult()
    chart_data = ctx.args.get("data")
    if not chart_data or len(chart_data) == 0:
        if ctx.last_table and ctx.last_table.get("rows"):
            chart_data = ctx.last_table.get("rows")
            _dbg("render_chart: Using last_table data (%d rows)", len(chart_data))
    elif isinstance(chart_data, list) and len(chart_data) > 0:
        sample_country = chart_data[0].get("Country") or chart_data[0].get("country")
        if sample_country and sample_country.upper() in ("USA", "CANADA", "UK"):
            if ctx.last_table and ctx.last_table.get("rows"):
                chart_data = ctx.last_table.get("rows")
                _dbg("render_chart: Detected fake data (USA/Canada/UK), using last_table data (%d rows)", len(chart_data))

    out = tool_render_chart(
        ctx.args.get("kind"),
        chart_data,
        ctx.args.get("xKey"),
        ctx.args.get("yKeys") or [],
        ctx.args.get("meta") or {},
        ctx.args.get("options") or {},
    )
    result.last_visualization = out.get("visualization")
    try:
        xk = ctx.args.get("xKey")
        yks = ctx.args.get("yKeys") or []
        data = chart_data or []
        if xk and isinstance(data, list):
            cols = [{"key": str(xk), "label": _pretty_label(str(xk))}] + [
                {"key": str(y), "label": _pretty_label(str(y))} for y in yks
            ]
            result.last_table = {"columns": cols, "rows": data}
    except Exception as _e:
        _dbg("WARN: could not cache viz table for show table: %s", _e)
    ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                     "content": json.dumps(out)})
    return result


@register_tool("explorer_search")
def handle_explorer_search(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_explorer_search, _normalize_table_for_ui, _dbg,
    )
    result = ToolResult()
    try:
        _raw_filters = ctx.args.get("filters")
        _rules = (_raw_filters.get("rules") or []) if isinstance(_raw_filters, dict) else []
        _injected_cols = list(ctx.args.get("columns") or [])
        if not _rules and not (isinstance(_raw_filters, dict) and _raw_filters.get("logic")):
            _last_user = ""
            for _m in reversed(ctx.msgs):
                if _m.get("role") == "user":
                    _last_user = str(_m.get("content") or "").lower()
                    break
            _injected_rules = []
            _injected_logic = "AND"
            if re.search(r"\bstage\s*[12]\b|\bscreening.program\b|\bpre.?symptomatic\b", _last_user):
                _injected_rules = [
                    {"field": "sf.C_Number_of_Stage1_Individuals_followed__c", "operator": ">", "value": 0},
                    {"field": "sf.C_Number_of_Stage2_Individuals_followed__c", "operator": ">", "value": 0},
                ]
                _injected_logic = "OR"
                for _sf_col in ["sf.C_Number_of_Stage1_Individuals_followed__c", "sf.C_Number_of_Stage2_Individuals_followed__c"]:
                    if _sf_col not in _injected_cols:
                        _injected_cols.append(_sf_col)
            else:
                _country_map = {
                    "spain": "Spain", "france": "France", "germany": "Germany", "italy": "Italy",
                    "portugal": "Portugal", "uk": "United Kingdom", "england": "United Kingdom",
                    "belgium": "Belgium", "netherlands": "Netherlands", "austria": "Austria",
                    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark", "finland": "Finland",
                    "poland": "Poland", "czech": "Czech Republic", "hungary": "Hungary",
                    "greece": "Greece", "croatia": "Croatia", "romania": "Romania", "bulgaria": "Bulgaria",
                    "switzerland": "Switzerland", "ireland": "Ireland", "slovakia": "Slovakia",
                }
                for k, v in _country_map.items():
                    if re.search(rf"\b{k}\b", _last_user):
                        _injected_rules.append({"field": "site.country", "operator": "equals", "value": v})
                        break
                if re.search(r"\bpharmac", _last_user):
                    _injected_rules.append({"field": "qual.3_6__is_your_pharmacy_on_site_or_off_campus", "operator": "equals", "value": "On-site"})
                if re.search(r"\bovernight", _last_user):
                    _injected_rules.append({"field": "qual.3_5_2__overnight_stay", "operator": "equals", "value": "Yes"})
            if _injected_rules:
                _dbg("FILTER-INJECT: empty filters → injecting %d rules (logic=%s) from user msg", len(_injected_rules), _injected_logic)
                _raw_filters = {"logic": _injected_logic, "rules": _injected_rules}
            else:
                _dbg("FILTER-INJECT: empty filters, no keywords detected → passing empty (all sites)")
        used_filters = _raw_filters if isinstance(_raw_filters, dict) and _raw_filters else {"logic": "AND", "rules": []}
        out = tool_explorer_search(
            ctx.request,
            filters=used_filters,
            columns=_injected_cols,
        )
        result.last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
        result.last_table = _normalize_table_for_ui(result.last_table)
        result.last_explorer_filters = used_filters
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(out, default=str)})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("nearest_filtered_sites")
def handle_nearest_filtered_sites(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_nearest_filtered_sites, _normalize_table_for_ui,
    )
    result = ToolResult()
    try:
        out = tool_nearest_filtered_sites(
            ctx.request,
            location=ctx.args.get("location", ""),
            filters=ctx.args.get("filters") or {"logic": "AND", "rules": []},
            top_n=int(ctx.args.get("top_n") or 10),
            max_km=float(ctx.args.get("max_km") or 1000),
            db=ctx.db,
        )
        result.last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
        result.last_table = _normalize_table_for_ui(result.last_table)
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(out, default=str)})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("explorer_within_drive_km")
def handle_explorer_within_drive_km(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_explorer_within_drive_km, _normalize_table_for_ui,
        _first_account_id_from_table,
    )
    result = ToolResult()
    base_id = ctx.args.get("base_account_id") or _first_account_id_from_table(ctx.last_table)
    if not base_id:
        ctx.msgs.append({
            "role": "tool", "tool_call_id": ctx.tool_call_id,
            "content": json.dumps({"error": "Missing base_account_id and no previous results to infer it"})
        })
    else:
        try:
            out = tool_explorer_within_drive_km(
                ctx.request,
                base_account_id=base_id,
                max_km=float(ctx.args.get("max_km") or 120),
                filters=ctx.args.get("filters") or {},
                columns=ctx.args.get("columns") or [],
            )
            result.last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
            result.last_table = _normalize_table_for_ui(result.last_table)
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps(out, default=str)})
        except Exception as ee:
            ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                             "content": json.dumps({"error": str(ee)})})
    return result


@register_tool("rank_sites")
def handle_rank_sites(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import tool_rank_sites, tool_rank_sites_by_group
    result = ToolResult()
    try:
        group_by = ctx.args.get("group_by")
        if group_by:
            # Delegate to per-group ranking
            out_table = tool_rank_sites_by_group(
                ctx.db,
                ctx.sf,
                ctx.args.get("metric", ""),
                group_by,
                int(ctx.args.get("top_n") or 3),
                ctx.args.get("order") or "desc",
            )
        else:
            out_table = tool_rank_sites(
                ctx.db,
                ctx.sf,
                ctx.args.get("metric", ""),
                int(ctx.args.get("top_n") or 5),
                ctx.args.get("order") or "desc",
            )
        result.last_table = out_table
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"ok": True})})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result

# Keep old name as alias
TOOL_DISPATCH["rank_sites_by_group"] = TOOL_DISPATCH["rank_sites"]


@register_tool("members_search")
def handle_members_search(ctx: ToolContext) -> ToolResult:
    from app.routers.ai_chat import (
        tool_members_search, _normalize_table_for_ui,
    )
    result = ToolResult()
    try:
        _mf = ctx.args.get("filters") or {"logic": "AND", "rules": []}
        _include_detail = bool(ctx.args.get("include_detail", False))
        out = tool_members_search(ctx.request, filters=_mf, include_detail=_include_detail)
        result.last_table = {"columns": out.get("columns") or [], "rows": out.get("rows") or []}
        result.last_table = _normalize_table_for_ui(result.last_table)
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps(out, default=str)})
    except Exception as ee:
        ctx.msgs.append({"role": "tool", "tool_call_id": ctx.tool_call_id,
                         "content": json.dumps({"error": str(ee)})})
    return result


# ---------------------------------------------------------------------------
# Dispatch entry point
# ---------------------------------------------------------------------------

def dispatch_tool(
    name: str,
    ctx: ToolContext,
) -> ToolResult:
    """
    Look up *name* in TOOL_DISPATCH and execute the handler.

    Returns a ToolResult.  If the tool name is unknown, appends an error
    message to ctx.msgs and returns an empty ToolResult.
    """
    handler = TOOL_DISPATCH.get(name)
    if handler is None:
        ctx.msgs.append({
            "role": "tool",
            "tool_call_id": ctx.tool_call_id,
            "content": json.dumps({"error": f"Unknown tool {name}"}),
        })
        return ToolResult()
    return handler(ctx)
