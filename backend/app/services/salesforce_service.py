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
from simple_salesforce.exceptions import (
    SalesforceExpiredSession, SalesforceAuthenticationFailed,
)

log = logging.getLogger("salesforce_service")

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {}  # {"sf": _ServiceSF, "expires_at": float}


class _ServiceSF:
    """Thin wrapper over a service-account Salesforce client that re-mints the
    token once and retries on an auth failure. The client-credentials flow has no
    refresh token, so a token Salesforce invalidated before our local TTL is
    recovered by re-minting. Wrapping `query`/`query_all` here means EVERY direct
    caller (Moby, sync, reports, extras) self-heals without threading retry logic
    through each call site; all other attributes pass through unchanged."""

    __slots__ = ("_sf",)

    def __init__(self, sf: Salesforce):
        self._sf = sf

    def query(self, soql: str, **kw):
        return self._retry("query", soql, **kw)

    def query_all(self, soql: str, **kw):
        return self._retry("query_all", soql, **kw)

    def _retry(self, method: str, soql: str, **kw):
        try:
            return getattr(self._sf, method)(soql, **kw)
        except (SalesforceExpiredSession, SalesforceAuthenticationFailed):
            reset_service_sf_cache()
            fresh = _new_service_client()  # raw client, one bounded retry
            return getattr(fresh, method)(soql, **kw)

    def __getattr__(self, name):
        # query/query_all/_sf are resolved directly; everything else (describe,
        # restful, SObject accessors, bulk, …) passes through to the real client.
        return getattr(self._sf, name)


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


def service_query(soql: str, *, query_all: bool = False) -> dict:
    """Convenience for a one-off SOQL query via the service account. Retry on an
    invalidated token is handled by the client wrapper (`_ServiceSF`), so this is
    just a thin call through it."""
    sf = get_service_sf()
    return sf.query_all(soql) if query_all else sf.query(soql)


def _build_client(cfg: Dict[str, object]) -> Salesforce:
    """Mint a token from the given config and build a raw Salesforce client."""
    payload = _mint_token(cfg)
    return Salesforce(
        instance_url=payload["instance_url"],
        session_id=payload["access_token"],
    )


def _new_service_client() -> Salesforce:
    """Mint a fresh token and build a raw (unwrapped) Salesforce client."""
    return _build_client(_cfg())


def get_service_sf() -> _ServiceSF:
    now = time.time()
    with _LOCK:
        sf = _CACHE.get("sf")
        exp = _CACHE.get("expires_at", 0)
        if sf is not None and isinstance(exp, (int, float)) and now < exp:
            return sf  # type: ignore[return-value]
        cfg = _cfg()
        client = _ServiceSF(_build_client(cfg))
        _CACHE["sf"] = client
        _CACHE["expires_at"] = now + int(cfg["ttl"]) - 60  # 60s safety margin
        return client
