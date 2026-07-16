# app/salesforce.py
from typing import Optional

from fastapi import Request
from simple_salesforce import Salesforce


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
