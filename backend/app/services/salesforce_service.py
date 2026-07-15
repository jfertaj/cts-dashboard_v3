"""Salesforce access via a single service account (OAuth 2.0 Client Credentials Flow).

No end-user session is involved. A token is minted from SF_CLIENT_ID/SECRET against
the org My Domain token endpoint and cached in-process (per uvicorn worker) until it
nears expiry; it is re-minted on expiry or when a caller reports a 401.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict

import httpx
from simple_salesforce import Salesforce

log = logging.getLogger("salesforce_service")

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {}  # {"sf": Salesforce, "expires_at": float}


def _cfg() -> Dict[str, object]:
    """Read + validate the client-credentials config, failing fast with a clear
    message rather than letting a misconfig surface as an opaque HTTP error."""
    my_domain = os.getenv("SF_MY_DOMAIN", "").rstrip("/")
    client_id = os.getenv("SF_CLIENT_ID", "").strip()
    client_secret = os.getenv("SF_CLIENT_SECRET", "").strip()

    missing = [
        name for name, val in (
            ("SF_MY_DOMAIN", my_domain),
            ("SF_CLIENT_ID", client_id),
            ("SF_CLIENT_SECRET", client_secret),
        ) if not val
    ]
    if missing:
        raise RuntimeError(
            "Salesforce service account not configured: missing " + ", ".join(missing)
        )

    try:
        ttl = int(os.getenv("SF_TOKEN_TTL_SECONDS", "3600"))
    except ValueError:
        ttl = 3600
    ttl = max(ttl, 60)  # never cache for less than the mint-safety margin

    return {
        "token_url": f"{my_domain}/services/oauth2/token",
        "client_id": client_id,
        "client_secret": client_secret,
        "ttl": ttl,
    }


def _mint_token(cfg: Dict[str, object]) -> dict:
    resp = httpx.post(
        cfg["token_url"],
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    log.info("Minted Salesforce service token (instance=%s)", payload.get("instance_url"))
    return payload


def reset_service_sf_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def get_service_sf() -> Salesforce:
    now = time.time()
    with _LOCK:
        sf = _CACHE.get("sf")
        exp = _CACHE.get("expires_at", 0)
        if sf is not None and isinstance(exp, (int, float)) and now < exp:
            return sf  # type: ignore[return-value]
        cfg = _cfg()
        payload = _mint_token(cfg)
        sf = Salesforce(
            instance_url=payload["instance_url"],
            session_id=payload["access_token"],
        )
        _CACHE["sf"] = sf
        _CACHE["expires_at"] = now + int(cfg["ttl"]) - 60  # 60s safety margin
        return sf
