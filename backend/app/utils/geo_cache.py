"""Geocoding cache (disk-backed, 10-year TTL) and Haversine distance utilities."""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger("cts-backend")

# Cache en memoria (address -> (lat, lng, expires_at)) + persistencia en JSON
_GEO_CACHE: Dict[str, Tuple[Optional[float], Optional[float], float]] = {}
# Persist across restarts; expiración muy larga para evitar llamadas repetidas
_GEO_TTL_SECONDS = 60 * 60 * 24 * 365 * 10  # 10 años
_GEO_LOCK = threading.RLock()
_GEO_CACHE_FILE = (Path(__file__).parent.parent / "cache" / "geocode_cache.json").resolve()
_GEO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _geo_key(city: Optional[str], country: Optional[str]) -> str:
    parts = [(city or "").strip().lower(), (country or "").strip().lower()]
    return "|".join(parts)


# Carga inicial desde disco
try:
    if _GEO_CACHE_FILE.exists():
        with _GEO_CACHE_FILE.open("r", encoding="utf-8") as fh:
            raw = json.load(fh) or {}
        with _GEO_LOCK:
            now_ts = time.time()
            for k, pair in raw.items():
                # pair = [lat, lng]
                lat, lng = (pair or [None, None])[:2]
                _GEO_CACHE[k] = (lat, lng, now_ts + _GEO_TTL_SECONDS)
        log.info("Geocode cache loaded: %d entries from %s", len(raw), _GEO_CACHE_FILE)
except Exception as _e:
    log.warning("Geocode cache load failed (starting with empty cache): %s", _e)


def _save_geo_cache_file() -> None:
    try:
        with _GEO_LOCK:
            blob = {k: [lat, lng] for k, (lat, lng, _exp) in _GEO_CACHE.items()}
        tmp = _GEO_CACHE_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(blob, fh)
        tmp.replace(_GEO_CACHE_FILE)
    except Exception as _e:
        log.warning("Geocode cache save failed: %s", _e)


def _geo_cache_get(city: Optional[str], country: Optional[str]) -> Tuple[Optional[float], Optional[float]] | None:
    k = _geo_key(city, country)
    with _GEO_LOCK:
        tup = _GEO_CACHE.get(k)
    if not tup:
        return None
    lat, lng, exp = tup
    if time.time() > exp:
        with _GEO_LOCK:
            _GEO_CACHE.pop(k, None)
        _save_geo_cache_file()
        return None
    return (lat, lng)


def _geo_cache_put(city: Optional[str], country: Optional[str], lat: Optional[float], lng: Optional[float]) -> None:
    k = _geo_key(city, country)
    with _GEO_LOCK:
        _GEO_CACHE[k] = (lat, lng, time.time() + _GEO_TTL_SECONDS)
    _save_geo_cache_file()


def _extract_result_country_iso(result: dict) -> Optional[str]:
    try:
        for comp in result.get("address_components", []):
            if "country" in comp.get("types", []):
                code = comp.get("short_name")
                return code
    except Exception:
        return None
    return None


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Straight-line Haversine distance in km."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))
