"""Pretty-label helper extracted from ai_chat.py.

Phase 3 refactor — pure move, no behavior changes. Lazy imports for the
catalog cache + sibling helpers avoid circular deps with ai_chat.
"""
from __future__ import annotations

import re


def _pretty_label(key: str) -> str:
    """
    Convierte claves técnicas en etiquetas legibles (no cambia las keys reales).
    """
    from app.routers.ai_chat import (
        _INDEX_CACHE,
        _apply_common_rewrites,
        _normalize,
        _prettify_sf_field_name,
    )

    k = key or ""
    # sf.* -> usa label de catálogo cuando exista
    if k.startswith("sf."):
        core = k[3:]
        # Mapa rápido para Account.*
        if core == "Account.Name":       return "Account Name"
        if core == "Account.Id":         return "Account Id"
        if core == "Account.ShippingCountry": return "Country"
        if core == "Account.ShippingCity":    return "City"
        # Detectar expresiones de agregación de Salesforce (expr0, expr1, etc.)
        if re.match(r"^expr\d+$", core, re.I):
            return "Average"  # genérico para AVG, COUNT, etc.
        # intenta catálogo
        lbl = (_INDEX_CACHE.get("sf_fields") or {}).get(_normalize(core), {}) or {}
        if isinstance(lbl, dict) and lbl.get("label"):
            return lbl["label"]
        # último recurso: heurística específica para API names
        return _prettify_sf_field_name(core)
    # Detectar expresiones agregadas sin prefijo sf. (expr0, expr1)
    if re.match(r"^expr\d+$", k, re.I):
        return "Average"
    # qual.* -> humaniza
    if k.startswith("qual."):
        base = k.split(".",1)[1]
        base = re.sub(r"__c$", "", base).replace("_", " ")
        return _apply_common_rewrites(base)
    if k.startswith("profil.") or k.startswith("profiling."):
        base = k.split(".",1)[1]
        base = re.sub(r"__c$", "", base).replace("_", " ")
        return _apply_common_rewrites(base)
    # extra.* y otros
    if k.startswith("extra."):
        return k.split(".",1)[1].replace("_"," ").title()
    if k in ("site","city","country","account_id","distance_km"):
        return {"site":"Account Name","city":"City","country":"Country","account_id":"Account Id","distance_km":"Distance (km)"}[k]
    # Account fields sin prefijo (ShippingCity, ShippingCountry)
    if k == "ShippingCity": return "City"
    if k == "ShippingCountry": return "Country"
    if k.lower() == "shippingcity": return "City"
    if k.lower() == "shippingcountry": return "Country"
    # por defecto humaniza
    return _apply_common_rewrites(re.sub(r"[_]+"," ",k))
