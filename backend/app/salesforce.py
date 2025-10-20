# app/salesforce.py
from typing import Optional
from fastapi import Request
from simple_salesforce import Salesforce, SalesforceExpiredSession, SalesforceGeneralError

from app.services.salesforce_oauth import (
    COOKIE_NAME,
    unsign_value,
    get_identity_from_session_id,
    get_salesforce_from_session_id,
    refresh_access_token,   # tienes refresh en tu servicio
    SESSIONS,               # almacen de sesiones en memoria
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
    """
    Devuelve un cliente de Salesforce a partir de la cookie httpOnly 'sf_session'.
    - Si la sesión no existe o la cookie no es válida: None
    - Si el access_token expiró y hay refresh_token: intenta refresh automáticamente.
    """
    if request is None:
        return None

    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return None

    session_id = unsign_value(signed)
    if not session_id:
        return None

    info = get_identity_from_session_id(session_id)
    if not info:
        return None

    access_token = info.get("access_token")
    instance_url = info.get("instance_url")
    if not access_token or not instance_url:
        return None

    # 1) Intenta cliente con el token actual
    sf = _build_client(access_token, instance_url)

    # 2) Prueba una llamada trivial para detectar expiración si quieres ser proactivo.
    #    Puedes omitir esto y dejar que el primer 401 dispare el refresh.
    try:
        # Llamada ligera (limits) para validar token. Ignora el resultado.
        _ = sf.restful("limits")
        return sf
    except SalesforceExpiredSession:
        # 3) Token expirado → intenta refresh si lo tienes
        refresh_token = info.get("refresh_token")
        if not refresh_token:
            return None

        try:
            # Llama a tu helper de refresh
            new_tokens = any_refresh(refresh_token)
            # Guarda nuevos tokens en SESSIONS[session_id]
            info["access_token"] = new_tokens["access_token"]
            info["instance_url"] = new_tokens.get("instance_url", info["instance_url"])
            # ¡OJO! Salesforce a veces no devuelve de nuevo el refresh_token
            if new_tokens.get("refresh_token"):
                info["refresh_token"] = new_tokens["refresh_token"]

            SESSIONS[session_id] = info
            # Devuelve un cliente nuevo con el token fresco
            return _build_client(info["access_token"], info["instance_url"])
        except Exception:
            return None
    except SalesforceGeneralError:
        # Algún otro error general; devuélvelo igualmente y que el caller gestione
        return sf

def any_refresh(refresh_token: str) -> dict:
    """
    Pequeño wrapper por claridad; usa tu refresh_access_token async con httpx
    desde un contexto sync mediante .sync().
    """
    import anyio
    return anyio.run(refresh_access_token, refresh_token)