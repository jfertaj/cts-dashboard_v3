import os
from fastapi import Request, HTTPException
from simple_salesforce import Salesforce
from app.services.salesforce_oauth import (
    COOKIE_NAME, unsign_value, get_salesforce_from_session_id
)

# Fallback por variables de entorno (legacy)
SF_USERNAME = os.getenv("SF_USERNAME")
SF_PASSWORD = os.getenv("SF_PASSWORD")
SF_SECURITY_TOKEN = os.getenv("SF_SECURITY_TOKEN")
SF_DOMAIN = os.getenv("SF_DOMAIN", "login")

def get_salesforce_session(request: Request | None = None) -> Salesforce:
    # 1) Si hay cookie OAuth, úsala
    if request:
        signed = request.cookies.get(COOKIE_NAME)
        if signed:
            sid = unsign_value(signed)
            if sid:
                from app.services.salesforce_service import get_service_sf
                return get_service_sf()

    # 2) Fallback a credenciales de entorno
    if SF_USERNAME and SF_PASSWORD and SF_SECURITY_TOKEN:
        return Salesforce(
            username=SF_USERNAME,
            password=SF_PASSWORD,
            security_token=SF_SECURITY_TOKEN,
            domain=SF_DOMAIN,
            version="59.0",
        )

    # 3) Si nada disponible, error
    raise HTTPException(401, "No Salesforce session. Inicia sesión con OAuth.")