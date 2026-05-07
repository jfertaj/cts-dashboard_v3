"""Table validation + UI normalization helpers extracted from ai_chat.py.

Phase 3 refactor — pure moves, no behavior changes. Lazy imports break the
circular dep with `app.routers.ai_chat`.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _ok_table(name: str) -> bool:
    from app.routers.ai_chat import ALLOWED_CTES, ALLOWED_TABLES

    name = (name or "").strip().strip('"')
    # Permitir CTEs (solo nombre sin schema)
    if name.lower() in {c.lower() for c in ALLOWED_CTES}:
        return True
    if "." not in name:
        name = f"public.{name}"
    norm_allowed = {t if t.startswith("public.") else f"public.{t}" for t in ALLOWED_TABLES}
    return name.lower() in norm_allowed


def _normalize_table_for_ui(table: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Asegura que cada fila tenga:
      - account_id (si hay sf.Account.Id o salesforce_account_id, etc.)
      - sf.Account.Id (si hay account_id)
      - site / sf.Account.Name cuando es detectable
      - country / city (desprefijando sitios comunes)
    No cambia los labels, solo agrega claves adicionales en rows (y añade columns si no existen).
    """
    from app.moby.helpers.labels import _pretty_label

    if not table or not isinstance(table, dict):
        return table
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not isinstance(rows, list):
        return table

    col_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in cols]
    col_set = set([str(k) for k in col_keys])

    def add_col(k: str):
        if k not in col_set:
            cols.append({"key": k, "label": _pretty_label(k)})
            col_set.add(k)

    id_candidates = [
        "sf.Account.Id", "sf.AccountId", "Account.Id",
        "account_id",
        "salesforce_account_id", "sites.salesforce_account_id", "s.salesforce_account_id",
        "sf_account_id", "sf.account.id", "sf_accountid",
    ]
    name_candidates = [
        "sf.Account.Name", "Account.Name", "sf.Name", "name", "sites.name", "s.name", "account_name",
    ]
    country_candidates = ["country", "sites.country", "s.country", "sf.Account.ShippingCountry", "Account.ShippingCountry"]
    city_candidates    = ["city", "sites.city", "s.city", "sf.Account.ShippingCity", "Account.ShippingCity"]

    norm_rows = []
    for r in rows:
        rd = dict(r) if isinstance(r, dict) else {}
        acc_id_val = next((rd[k] for k in id_candidates if k in rd and rd.get(k)), None)
        if acc_id_val:
            rd.setdefault("account_id", acc_id_val)
            add_col("account_id")
        site_name = next((rd[k] for k in name_candidates if k in rd and rd.get(k)), None)
        if site_name:
            rd.setdefault("site", site_name)
            add_col("site")
        for keys, std in ((country_candidates, "country"), (city_candidates, "city")):
            val = next((rd[k] for k in keys if k in rd and rd.get(k) not in (None, "")), None)
            if val is not None:
                rd.setdefault(std, val); add_col(std)
        norm_rows.append(rd)
    # --- De-duplicación y normalización final de columnas visibles ---
    # Preferimos claves amigables y eliminamos los equivalentes sf.Account.*
    friendly = {
        "sf.Account.Id": "account_id",
        "sf.AccountId": "account_id",
        "Account.Id": "account_id",
        "salesforce_account_id": "account_id",
        "sf.Account.Name": "site",
        "Account.Name": "site",
        "account_name": "site",
        "sf.Name": "site",
        "name": "site",
        "sf.Account.ShippingCountry": "country",
        "Account.ShippingCountry": "country",
        "ShippingCountry": "country",
        "sf.Account.ShippingCity": "city",
        "Account.ShippingCity": "city",
        "ShippingCity": "city",
    }

    # 1) Recoge el orden original de claves visto en 'cols'
    orig_keys = [c.get("key") if isinstance(c, dict) else str(c) for c in cols]

    # 2) Calcula cuáles amigables existen realmente (por datos o por columnas)
    present = set()
    for r in norm_rows:
        if isinstance(r, dict):
            present.update(k for k, v in r.items() if v is not None)
    present.update(orig_keys)

    preferred = [k for k in ("account_id", "site", "country", "city", "distance_km") if k in present]

    # 3) Elimina duplicados y mapea sf.Account.* → amigables
    def _normalize_key(k: str) -> str:
        return friendly.get(k, k)

    seen = set()
    final_keys = []

    # a) siempre primero las preferidas si existen
    for k in preferred:
        if k not in seen:
            seen.add(k); final_keys.append(k)

    # b) el resto respetando orden original, filtrando equivalentes sf.Account.*
    for k in orig_keys:
        nk = _normalize_key(k)
        # omite las sf.Account.* mapeadas cuando ya está su amigable
        if nk in preferred and nk in seen:
            continue
        if nk not in seen:
            seen.add(nk); final_keys.append(nk)

    # 4) Construye columnas finales con etiquetas bonitas
    cols = [{"key": k, "label": _pretty_label(k)} for k in final_keys]

    table["columns"] = cols
    table["rows"] = norm_rows
    return table
