# app/salesforce.py
from typing import Optional
from fastapi import Request
from simple_salesforce import Salesforce, SalesforceExpiredSession, SalesforceGeneralError

from app.services.salesforce_oauth import (
    COOKIE_NAME,
    unsign_value,
    get_identity_from_session_id,
    get_salesforce_from_session_id,
    refresh_access_token,
    _db_write_session,      # para persistir tokens refrescados
    SF_API_VERSION,
)

def _build_client(access_token: str, instance_url: str) -> Salesforce:
    # Usa versión que ya saneaste en services.salesforce_oauth
    return Salesforce(
        instance_url=instance_url,
        session_id=access_token,
        version=SF_API_VERSION,
    )

def get_sf_client(request: Optional[Request] = None) -> Optional[Salesforce]:
    """Return the shared service-account Salesforce client (per-user `sf_session`
    is retired; Moby's direct Salesforce tools now run as the service account too).

    Returns None if the service account is not configured, preserving the old
    "no Salesforce session" degradation for callers and for local/test envs that
    have no SF credentials — so Moby degrades gracefully instead of crashing.
    """
    from app.services.salesforce_service import get_service_sf
    try:
        return get_service_sf()
    except Exception:
        return None

def any_refresh(refresh_token: str) -> dict:
    """
    Pequeño wrapper por claridad; usa tu refresh_access_token async con httpx
    desde un contexto sync mediante .sync().
    """
    import anyio
    return anyio.run(refresh_access_token, refresh_token)