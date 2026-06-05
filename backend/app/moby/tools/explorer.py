"""Explorer-side tool implementations for Moby.

Pure move from `app.routers.ai_chat` (Phase 2 refactor). Behavior is
unchanged. Each function depends on private helpers that still live in
`ai_chat` (`_dbg`, `_pretty_label`, `_resolve_metric`,
`_normalize_table_for_ui`, `tool_sql_query`, `tool_salesforce_query`).
To avoid an import cycle at module-load time (ai_chat re-exports these
tool functions back), we resolve those helpers lazily inside each function
via `from app.routers import ai_chat as _ai`.

The functions are re-exported by `ai_chat.py`, so existing tests that
patch `app.routers.ai_chat.tool_explorer_search` etc. keep working.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Literal, Optional
from app.moby.helpers.debug import _dbg
from app.moby.helpers.labels import _pretty_label
from app.moby.helpers.metrics import _resolve_metric
from app.moby.helpers.tables import _normalize_table_for_ui


import httpx
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.moby.config import EXPLORER_DRIVE_KM_PATH, EXPLORER_SEARCH_PATH
from app.routers.moby_planner import validate_filter_group


def tool_explorer_within_drive_km(
    request: Request,
    base_account_id: str,
    max_km: float,
    filters: Optional[Dict[str, Any]] = None,
    columns: Optional[List[str]] = None,
):
    """
    Proxy interno al endpoint /api/explorer/search/within-drive-km, preservando sesión/cookies.
    """
    url = f"http://127.0.0.1:8000{EXPLORER_DRIVE_KM_PATH}"
    payload = {
        "base_account_id": base_account_id,
        "max_km": max_km,
        "filters": filters or {"logic": "AND", "rules": []},
        "columns": columns or [],
    }
    # Reenvía cookies/sesión para que _get_sf use la sesión actual
    headers = {}
    if request:
        ck = request.headers.get("cookie")
        if ck: headers["cookie"] = ck
        auth = request.headers.get("authorization")
        if auth: headers["authorization"] = auth
    with httpx.Client(timeout=60.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"drive-km failed: {resp.text}")
    data = resp.json()
    # Construimos tabla básica desde data['rows'] (colapsadas por cuenta)
    rows = data.get("rows") or []
    cols: List[Dict[str, str]] = []
    if rows:
        keys = sorted({k for r in rows for k in r.keys()})
        cols = [{"key": k, "label": k} for k in keys]
    return {"columns": cols, "rows": rows, "meta": data.get("meta"), "base": data.get("base")}


def tool_explorer_search(
    request: Request,
    filters: Dict[str, Any],
    columns: Optional[List[str]] = None,
):
    """
    Proxy interno al endpoint /api/explorer/search.
    Acepta FilterGroup con reglas qual.*, sf.* y site.* y devuelve una tabla unificada.
    """
    # Phase 4: validate FilterGroup before executing — surface errors to Claude so it can fix them
    if filters:
        _vl_errors = validate_filter_group(filters)
        if _vl_errors:
            _dbg("WARN: invalid FilterGroup from tool call: %s", _vl_errors)
            return {
                "error": "invalid_filter_group",
                "validation_errors": _vl_errors,
                "message": (
                    f"The filter group has {len(_vl_errors)} validation error(s): "
                    + "; ".join(_vl_errors[:5])
                    + ". Please correct the filters and try again."
                ),
                "rows": [],
                "columns": [],
            }

    url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
    # Columnas por defecto si no se especifican
    default_cols = [
        "sf.Account.Id", "sf.Account.Name",
        "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
        "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c",
        "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
        "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
    ]
    payload = {
        "filters": filters or {"logic": "AND", "rules": []},
        "columns": columns or default_cols,
    }
    headers = {}
    if request:
        ck = request.headers.get("cookie")
        if ck:
            headers["cookie"] = ck
        auth = request.headers.get("authorization")
        if auth:
            headers["authorization"] = auth
    with httpx.Client(timeout=90.0) as cli:
        resp = cli.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, f"explorer_search failed: {resp.text[:300]}")
    data = resp.json()
    rows = data.get("rows") or []
    cols: List[Dict[str, str]] = []
    if rows:
        keys = list({k for r in rows for k in (r.get("data") or r).keys()})
        keys = sorted(keys)
        cols = [{"key": k, "label": k} for k in keys]
    # Aplanar data nested si viene en {account_id, account_name, data:{...}}
    flat_rows = []
    for r in rows:
        if "data" in r and isinstance(r["data"], dict):
            flat = {**r["data"]}
            for meta_k in ("account_id", "account_name", "country", "city"):
                if meta_k in r:
                    flat[meta_k] = r[meta_k]
        else:
            flat = dict(r)
        flat_rows.append(flat)
    return {
        "columns": cols,
        "rows": flat_rows[:300],
        "meta": {"total": len(rows)},
    }


def tool_nearest_filtered_sites(
    request: Request,
    location: str,
    filters: Optional[Dict[str, Any]] = None,
    top_n: int = 10,
    max_km: float = 1000,
    db: Optional[Any] = None,
) -> dict:
    """
    Find the nearest clinical sites to a city/address, optionally filtered by qual.* and sf.* fields.
    Returns sites sorted by straight-line (Haversine) distance in km.

    Fast path (no filters): queries local Site table directly — no Salesforce API call needed.
    Filtered path: calls /api/explorer/search internally (requires valid session cookie).
    """
    from app.models.site import Site as SiteModel

    # 1. Geocode location — try geonames_cities DB first (free, fast), Google API as fallback
    geo_lat, geo_lon, formatted = None, None, location

    # 1a. Try geonames_cities table (cities500.txt — ~200k cities with coords)
    try:
        if db:
            from app.models.geonames import GeonameCity
            # Parse "City, Country" or just "City"
            parts = [p.strip() for p in location.split(",")]
            city_name = parts[0]
            # Query by name, order by population DESC (largest city wins)
            q = db.query(GeonameCity).filter(
                GeonameCity.name.ilike(city_name)
            ).order_by(GeonameCity.population.desc())
            # If country hint provided, filter by it
            if len(parts) > 1:
                country_hint = parts[-1].strip()
                from app.utils.country_norms import resolve_countries
                resolved = resolve_countries(country_hint)
                if resolved:
                    iso2 = resolved[0].get("iso2", "")
                    if iso2:
                        q = q.filter(GeonameCity.country_code == iso2)
            geo_city = q.first()
            if geo_city and geo_city.latitude and geo_city.longitude:
                geo_lat, geo_lon = float(geo_city.latitude), float(geo_city.longitude)
                formatted = f"{geo_city.name}, {geo_city.country_code}"
                _dbg("NEAREST: geocoded '%s' via geonames_cities → %s (%.4f, %.4f, pop=%s)",
                     location, formatted, geo_lat, geo_lon, geo_city.population)
    except Exception as e:
        _dbg("WARN: geonames geocode failed: %s", e)

    # 1b. Fallback to Google Geocoding API (if geonames didn't find it)
    if geo_lat is None:
        google_key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        region = os.environ.get("GOOGLE_REGION_BIAS", "")
        if google_key:
            try:
                with httpx.Client(timeout=10.0) as gcli:
                    gr = gcli.get(
                        "https://maps.googleapis.com/maps/api/geocode/json",
                        params={"address": location, "key": google_key, "region": region},
                    )
                    gj = gr.json()
                    if gj.get("results"):
                        loc = gj["results"][0]["geometry"]["location"]
                        geo_lat, geo_lon = float(loc["lat"]), float(loc["lng"])
                        formatted = gj["results"][0].get("formatted_address", location)
                        _dbg("NEAREST: geocoded '%s' via Google API → %s", location, formatted)
            except Exception as e:
                _dbg("WARN: Google geocode failed: %s", e)
    if geo_lat is None:
        return {"error": f"Could not geocode '{location}'."}

    # 2. Haversine distance helper
    def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        )
        return round(R * 2 * math.asin(math.sqrt(max(0.0, a))), 1)

    has_filters = bool(filters and filters.get("rules"))
    n_total_sites = 0  # set by whichever path runs, for diagnostics
    n_no_coords = 0

    # Check if we have a valid SF session cookie → prefer Explorer path (all 195+ sites with coords)
    # The local Site table only has ~45 sites (those with qualification uploads), so fast-path
    # misses most sites. Only fall back to fast-path when no SF session is available.
    _has_sf_cookie = bool(request and request.headers.get("cookie"))

    # 3a. Fast path: no filters AND no SF session → query local Site table + explorer geocache
    if not has_filters and db is not None and not _has_sf_cookie:
        _dbg("NEAREST: fast-path via local DB + geocache (no filters)")
        # Lazy-import geocache + country normalizer from explorer (already loaded in memory, no overhead)
        _geo_cache_get_fn = None
        _country_norm_fn = None
        try:
            from app.utils.geo_cache import _geo_cache_get as _geo_cache_get_fn  # noqa: F401
            from app.routers.salesforce_explorer import _country_norm as _country_norm_fn  # noqa: F401
        except Exception as _imp_err:
            _dbg("NEAREST: could not import geocache helpers: %s", _imp_err)

        sites_q = db.query(
            SiteModel.salesforce_account_id,
            SiteModel.name,
            SiteModel.city,
            SiteModel.country,
            SiteModel.latitude,
            SiteModel.longitude,
        ).filter(
            SiteModel.salesforce_account_id.isnot(None),
        ).all()

        enriched = []
        n_db_coords = 0
        n_cache_coords = 0
        n_no_coords = 0
        for s in sites_q:
            lat, lng = s.latitude, s.longitude
            if lat is None or lng is None:
                # Fallback: look up explorer's in-memory geocache (city|country key)
                if _geo_cache_get_fn is not None:
                    # Try as-is first, then with normalized country (e.g. "Spain" → "ES")
                    cached = _geo_cache_get_fn(s.city, s.country)
                    if not cached and _country_norm_fn is not None and s.country:
                        cached = _geo_cache_get_fn(s.city, _country_norm_fn(s.country))
                    if cached:
                        lat, lng = cached
                        n_cache_coords += 1
                    else:
                        n_no_coords += 1
                else:
                    n_no_coords += 1
            else:
                n_db_coords += 1
            if lat is None or lng is None:
                continue
            dist = haversine_km(geo_lat, geo_lon, float(lat), float(lng))
            if dist <= max_km:
                enriched.append({
                    "account_id": s.salesforce_account_id,
                    "account_name": s.name or "",
                    "country": s.country or "",
                    "city": s.city or "",
                    "distance_km": dist,
                })
        n_total_sites = len(sites_q)
        _dbg(
            "NEAREST: fast-path: %d total sites, db_coords=%d cache_coords=%d no_coords=%d within_%skm=%d",
            n_total_sites, n_db_coords, n_cache_coords, n_no_coords, int(max_km), len(enriched),
        )

    # 3b. Filtered path: call /api/explorer/search (requires session cookie)
    else:
        _dbg("NEAREST: filtered-path via internal HTTP (filters=%s)", bool(has_filters))
        url = f"http://127.0.0.1:8000{EXPLORER_SEARCH_PATH}"
        headers = {}
        if request:
            ck = request.headers.get("cookie")
            if ck:
                headers["cookie"] = ck
            auth = request.headers.get("authorization")
            if auth:
                headers["authorization"] = auth
        _nearest_sf_cols = [
            "sf.Account.Id", "sf.Account.Name", "sf.Account.ShippingCountry", "sf.Account.ShippingCity",
            "sf.C_Number_of_Stage1_Individuals_followed__c", "sf.C_Number_of_Stage2_Individuals_followed__c",
            "sf.C_Number_of_new_T1D_diagnosed_O_18__c", "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
            "sf.C_Number_of_T1D_Patients_currently_O_18__c", "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        ]
        with httpx.Client(timeout=90.0) as cli:
            resp = cli.post(
                url,
                json={"filters": filters or {"logic": "AND", "rules": []}, "columns": _nearest_sf_cols},
                headers=headers,
            )
        _dbg("NEAREST: explorer/search status=%d", resp.status_code)
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, f"explorer_search failed: {resp.text[:300]}")
        resp_json = resp.json()
        points = resp_json.get("points") or []
        point_coords: Dict[str, tuple] = {
            p["account_id"]: (float(p["lat"]), float(p["lng"]))
            for p in points
            if p.get("account_id") and p.get("lat") and p.get("lng")
        }
        enriched = []
        for row in resp_json.get("rows") or []:
            aid = row.get("account_id", "")
            if aid not in point_coords:
                continue
            lat, lng = point_coords[aid]
            dist = haversine_km(geo_lat, geo_lon, lat, lng)
            if dist > max_km:
                continue
            flat: Dict[str, Any] = {
                "account_id": aid,
                "account_name": row.get("account_name", ""),
                "country": row.get("country", ""),
                "city": row.get("city", ""),
                "distance_km": dist,
            }
            data = row.get("data") or {}
            flat.update(data)
            enriched.append(flat)

    enriched.sort(key=lambda r: r["distance_km"])
    enriched = enriched[: min(top_n, 50)]

    # Base columns always shown
    columns = [
        {"key": "distance_km", "label": f"Distance from {formatted} (km)"},
        {"key": "account_name", "label": "Site"},
        {"key": "country", "label": "Country"},
        {"key": "city", "label": "City"},
    ]
    # Auto-detect SF metric fields present in rows and add them as columns
    _sf_key_labels = {
        "sf.C_Number_of_Stage1_Individuals_followed__c": "Stage 1",
        "sf.C_Number_of_Stage2_Individuals_followed__c": "Stage 2",
        "sf.C_Number_of_new_T1D_diagnosed_O_18__c": "ND ≥18",
        "sf.C_Number_of_new_T1D_diagnosed_U_18__c": "ND <18",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c": "T1D ≥18",
        "sf.C_Number_of_T1D_Patients_currently_U_18__c": "T1D <18",
    }
    _present_sf_keys = {k for row in enriched for k in row if k.startswith("sf.")}
    for sf_key, sf_label in _sf_key_labels.items():
        if sf_key in _present_sf_keys:
            columns.append({"key": sf_key, "label": sf_label})
    # Also include any sf.* fields explicitly mentioned in filter rules (reliable even when data is null)
    _already_col_keys = {c["key"] for c in columns}
    def _collect_filter_sf_fields_fn(f: dict) -> list:
        result: list = []
        if not f:
            return result
        for _r in f.get("rules") or []:
            if isinstance(_r, dict):
                _fld = _r.get("field", "")
                if _fld.startswith("sf."):
                    result.append(_fld)
                else:
                    result.extend(_collect_filter_sf_fields_fn(_r))
        return result
    for _ff in _collect_filter_sf_fields_fn(filters or {}):
        if _ff not in _already_col_keys:
            _lbl = _sf_key_labels.get(_ff) or _pretty_label(_ff)
            columns.append({"key": _ff, "label": _lbl})
            _already_col_keys.add(_ff)
    return {
        "columns": columns,
        "rows": enriched,
        "meta": {
            "total": len(enriched),
            "geocoded": formatted,
            "note": "Straight-line distances (km). Use Explorer nearby for driving distance.",
            "no_site_coords": n_no_coords,   # >0 means some sites had no geocoords (DB + cache both missed)
            "n_total_sites": n_total_sites,
        },
    }


def tool_rank_sites_by_group(
    db: Session,
    sf,
    metric: str,
    group_by: Literal["country", "city"] = "country",
    top_n: int = 3,
    order: Literal["asc", "desc"] = "desc",
):
    """Top-N per group (country/city) for both SF and site_qual metrics."""
    from app.routers import ai_chat as _ai

    meta = _resolve_metric(metric, db)
    dir_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    grp_col = "country" if group_by == "country" else "city"

    # -- site_qual path --
    if meta.get("source") == "site_qual":
        key = meta.get("key")
        grp = f"s.{grp_col}"
        sql = f"""
            WITH scored AS (
              SELECT
                {grp} AS grp,
                s.salesforce_account_id AS account_id,
                s.name AS site,
                s.country,
                s.city,
                COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) AS metric,
                ROW_NUMBER() OVER (PARTITION BY {grp} ORDER BY COALESCE(NULLIF(regexp_replace(sq.data->>:key, '[^0-9\\.\\-]', '', 'g'), '')::numeric, 0) {dir_sql} NULLS LAST) AS rn
              FROM public.sites s
              LEFT JOIN public.site_qual sq ON sq.site_id = s.id
              WHERE {grp} IS NOT NULL
            )
            SELECT grp, account_id, site, country, city, metric
            FROM scored
            WHERE rn <= :top
            ORDER BY grp, metric {dir_sql}
        """
        out = _ai.tool_sql_query(db, sql, {"key": key, "top": int(top_n)})
        cols = out.get("columns") or []
        rows = [{cols[i]: v for i, v in enumerate(r)} for r in out.get("rows") or []]
        table = {
            "columns": [
                {"key": "group", "label": group_by.title()},
                {"key": "account_id", "label": "Account Id"},
                {"key": "site", "label": "Account Name"},
                {"key": "country", "label": "Country"},
                {"key": "city", "label": "City"},
                {"key": f"qual.{key}", "label": _pretty_label(f"qual.{key}")}
            ],
            "rows": [
                {
                    "group": r.get("grp"),
                    "account_id": r.get("account_id"),
                    "site": r.get("site"),
                    "country": r.get("country"),
                    "city": r.get("city"),
                    f"qual.{key}": r.get("metric")
                } for r in rows
            ],
        }
        return _normalize_table_for_ui(table)

    # -- SF path --
    field = meta.get("field")
    if not sf:
        raise HTTPException(400, "No active Salesforce session for SF ranking")
    grp_field = f"Account.Shipping{group_by.title()}"
    soql = f"""
        SELECT
            {grp_field} grp,
            Account.Id,
            Account.Name,
            Account.ShippingCountry,
            Account.ShippingCity,
            MAX({field}) metric
        FROM Opportunity
        WHERE {field} != null AND {grp_field} != null
        GROUP BY {grp_field}, Account.Id, Account.Name, Account.ShippingCountry, Account.ShippingCity
        ORDER BY {grp_field}, metric {dir_sql} NULLS LAST
        LIMIT 500
    """
    raw = _ai.tool_salesforce_query(sf, soql)
    records = raw.get("records", []) if isinstance(raw, dict) else []

    # Group and take top N per group
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        acc = r.get("Account") or {}
        # Try multiple ways to get the group value
        g = r.get("grp") or r.get("expr0")
        # If not found, try getting it from the Account directly
        if not g and group_by == "country":
            g = acc.get("ShippingCountry")
        elif not g and group_by == "city":
            g = acc.get("ShippingCity")
        if not g:
            continue
        grouped.setdefault(g, [])
        if len(grouped[g]) < int(top_n):
            grouped[g].append({
                "group": g,
                "account_id": acc.get("Id"),
                "site": acc.get("Name"),
                "country": acc.get("ShippingCountry"),
                "city": acc.get("ShippingCity"),
                f"sf.{field}": r.get("expr1") or r.get("metric"),
            })

    rows = [item for items in grouped.values() for item in items]
    table = {
        "columns": [
            {"key": "group", "label": group_by.title()},
            {"key": "account_id", "label": "Account Id"},
            {"key": "site", "label": "Account Name"},
            {"key": "country", "label": "Country"},
            {"key": "city", "label": "City"},
            {"key": f"sf.{field}", "label": _pretty_label(f"sf.{field}")},
        ],
        "rows": rows,
    }
    return _normalize_table_for_ui(table)
