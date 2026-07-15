from fastapi import Request
from simple_salesforce import Salesforce


def get_salesforce_session(request: Request | None = None) -> Salesforce:
    """Return the shared service-account Salesforce client (user session no longer used)."""
    from app.services.salesforce_service import get_service_sf
    return get_service_sf()
