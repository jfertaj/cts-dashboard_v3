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


def _cfg() -> Dict[str, str]:
    my_domain = os.getenv("SF_MY_DOMAIN", "").rstrip("/")
    return {
        "token_url": f"{my_domain}/services/oauth2/token",
        "client_id": os.getenv("SF_CLIENT_ID", ""),
        "client_secret": os.getenv("SF_CLIENT_SECRET", ""),
        "ttl": int(os.getenv("SF_TOKEN_TTL_SECONDS", "3600")),
    }


def _mint_token() -> dict:
    cfg = _cfg()
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
        payload = _mint_token()
        sf = Salesforce(
            instance_url=payload["instance_url"],
            session_id=payload["access_token"],
        )
        _CACHE["sf"] = sf
        _CACHE["expires_at"] = now + _cfg()["ttl"] - 60  # 60s safety margin
        return sf
