# backend/app/routers/explorer.py
from __future__ import annotations
from typing import Any, Dict, List, Literal, Set, Tuple
from fastapi import APIRouter, Request

# Reutilizamos utilidades del router salesforce_explorer
from app.routers.salesforce_explorer import (
    _get_sf, _sf_query_all, map_bootstrap,
)

router = APIRouter(prefix="/api/explorer", tags=["explorer"])

# ------------------ modelos (tipados del frontend) ------------------

class FieldDef(Dict[str, Any]): pass
class FilterRule(Dict[str, Any]): pass
class FilterGroup(Dict[str, Any]): pass

# ------------------ catálogo de campos ------------------

# Campos “site” que siempre mostramos en filtros
SITE_FIELDS: List[Tuple[str, str]] = [
    ("site.country", "Country"),
    ("site.city",    "City"),
]

# Campos de Account que queremos exponer como [sf] en el selector de columnas
SF_ACCOUNT_FIELDS: List[Tuple[str, str]] = [
    ("Account.BillingCity",      "Account.BillingCity"),
    ("Account.BillingCountry",   "Account.BillingCountry"),
    ("Account.BillingLatitude",  "Account.BillingLatitude"),
    ("Account.BillingLongitude", "Account.BillingLongitude"),
    ("Account.Id",               "Account.Id"),
    ("Account.Name",             "Account.Name"),
]

@router.get("/fields")
def explorer_fields():
    """
    Devuelve los campos disponibles para filtros/columnas en el Explorer.
    Incluye los 'site.*' y los '[sf] …' de Account.
    """
    fields: List[FieldDef] = []
    # site.*
    for key, label in SITE_FIELDS:
        fields.append({"key": key, "label": label, "type": "string", "source": "site"})
    # sf.*
    for key, label in SF_ACCOUNT_FIELDS:
        fields.append({"key": f"sf.{key}", "label": label, "type": "string", "source": "sf"})
    return {"fields": fields}

# ------------------ utilidades ------------------

def _fetch_accounts_for_columns(request: Request, account_ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """
    Devuelve {account_id: {Account.<campo>: valor}} sólo para las columnas SF.
    """
    if not account_ids:
        return {}

    sf = _get_sf(request)

    # Construimos la lista de campos reales de Account a pedir
    account_field_list = sorted(
        { k.split("Account.", 1)[1] for k,_ in SF_ACCOUNT_FIELDS if k.startswith("Account.") }
    )
    acc_fields = "Id, " + ", ".join(account_field_list)

    def chunk(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    out: Dict[str, Dict[str, Any]] = {}
    for group in chunk(list(account_ids), 150):
        vals = ", ".join(f"'{x}'" for x in group)
        soql = f"SELECT {acc_fields} FROM Account WHERE Id IN ({vals})"
        for acc in _sf_query_all(sf, soql):
            out[acc["Id"]] = acc
    return out

# ------------------ búsqueda principal ------------------

@router.post("/search")
def explorer_search(payload: Dict[str, Any], request: Request):
    """
    Construye:
      - points: para el mapa (lat/lng + badges)
      - rows:   para la tabla (Account + tipo de oportunidad)
    Las columnas que empiezan por 'sf.' se rellenan con campos de Account.
    """
    # 1) Arrancamos de tu bootstrap (ya trae abiertas y cerradas)
    bootstrap = map_bootstrap(request)  # lista de dicts con lat/lng, city/country y badges

    # 2) points (lo que espera tu MapView)
    points = []
    account_ids: Set[str] = set()
    for b in bootstrap:
        account_ids.add(b["account_id"])
        points.append({
            "lat": b.get("latitude"),
            "lng": b.get("longitude"),
            "account_id": b["account_id"],
            "account_name": b.get("site"),
            "city": b.get("city"),
            "country": b.get("country"),
            "badges": {
                "profiling":     bool(b.get("hasProfiling")),
                "qualification": bool(b.get("hasQualification")),
            },
        })

    # 3) rows (cada tipo de oportunidad que el site tenga)
    rows = []
    for b in bootstrap:
        if b.get("hasProfiling"):
            rows.append({
                "account_id":   b["account_id"],
                "account_name": b.get("site"),
                "country":      b.get("country"),
                "city":         b.get("city"),
                "opportunity_type": "profiling",
                "data": {},  # se rellenará con columnas seleccionadas
            })
        if b.get("hasQualification"):
            rows.append({
                "account_id":   b["account_id"],
                "account_name": b.get("site"),
                "country":      b.get("country"),
                "city":         b.get("city"),
                "opportunity_type": "qualification",
                "data": {},
            })

    # 4) Completar columnas seleccionadas (sólo sf.*)
    cols: List[str] = list(payload.get("columns") or [])
    sf_cols = [c for c in cols if c.startswith("sf.")]

    if sf_cols and account_ids:
        # Traer Accounts para las columnas
        accounts = _fetch_accounts_for_columns(request, account_ids)
        for r in rows:
            acc = accounts.get(r["account_id"], {})
            data = {}
            for col in sf_cols:
                # col p.ej. 'sf.Account.BillingCity' -> Account.BillingCity
                key = col[3:]
                # en acc el campo es sólo la parte después de 'Account.'
                acc_key = key.split("Account.", 1)[1] if "Account." in key else key
                data[col] = acc.get(acc_key)
            r["data"] = data

    return {"points": points, "rows": rows}