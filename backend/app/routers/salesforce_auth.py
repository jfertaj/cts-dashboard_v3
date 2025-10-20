# app/routers/salesforce_auth.py
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from typing import Optional
import os

from app.services.salesforce_oauth import (
    build_authorize_url, exchange_code_for_token,
    create_session, destroy_session, sign_value, unsign_value,
    get_identity_from_session_id,
    COOKIE_NAME, STATE_COOKIE, FRONTEND_BASE,
)

router = APIRouter(prefix="/api/salesforce", tags=["salesforce-auth"])

def _secure_cookies_enabled() -> bool:
    """Activa 'secure' para cookies si estamos en HTTPS o lo fuerza la var de entorno."""
    if os.getenv("ENABLE_SECURE_COOKIES", "").strip() in ("1", "true", "TRUE", "yes"):
        return True
    return FRONTEND_BASE.lower().startswith("https://")

# Helpers para cookies httpOnly
COOKIE_KW_BASE = dict(httponly=True, samesite="lax")
COOKIE_KW = {**COOKIE_KW_BASE, **({"secure": True} if _secure_cookies_enabled() else {})}

@router.get("/oauth/login")
def oauth_login(next: Optional[str] = "/"):
    """
    Redirige a Salesforce con Authorization Code Flow.
    'next' permite volver a la ruta del frontend tras el login.
    """
    state_raw = next or "/"
    state = sign_value(state_raw)
    url = build_authorize_url(state)
    resp = RedirectResponse(url)
    # Guarda 'state' en una cookie auxiliar (opcional pero útil)
    resp.set_cookie(STATE_COOKIE, state, **COOKIE_KW)
    return resp

@router.get("/oauth/callback")
async def oauth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None):
    """
    Callback desde Salesforce. Intercambia 'code' por tokens y crea sesión.
    Guarda cookie httpOnly 'sf_session' y redirige al frontend.
    """
    if not code:
        raise HTTPException(status_code=400, detail="Missing 'code'")
    if not state:
        raise HTTPException(status_code=400, detail="Missing 'state'")

    # valida state contra cookie o firma directa
    cookie_state = request.cookies.get(STATE_COOKIE)
    if cookie_state != state and not unsign_value(state):
        raise HTTPException(status_code=400, detail="Invalid 'state'")

    # Intercambio code -> tokens
    try:
        token_payload = await exchange_code_for_token(code)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {e}")

    # Crea sesión en memoria y firma el session_id
    session_id = create_session(token_payload)
    signed_session = sign_value(session_id)

    # Destino final (ruta del frontend)
    next_url = unsign_value(state) or "/"
    redirect_to = f"{FRONTEND_BASE}{next_url}"

    resp = RedirectResponse(redirect_to, status_code=302)
    resp.set_cookie(COOKIE_NAME, signed_session, **COOKIE_KW)
    # limpia state cookie
    resp.delete_cookie(STATE_COOKIE)
    return resp

@router.get("/logout")
def logout_get(request: Request):
    """
    Variante GET del logout (útil para enlaces). Responde JSON igualmente.
    """
    signed = request.cookies.get(COOKIE_NAME)
    if signed:
        sid = unsign_value(signed)
        if sid:
            destroy_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp

@router.get("/me")
def me(request: Request):
    """
    Estado de autenticación Salesforce para el frontend.
    """
    signed = request.cookies.get(COOKIE_NAME)
    if not signed:
        return {"authenticated": False}
    sid = unsign_value(signed)
    if not sid:
        return {"authenticated": False}
    info = get_identity_from_session_id(sid)
    if not info:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "instance_url": info.get("instance_url"),
        "issued_at": info.get("issued_at"),
        "has_refresh": bool(info.get("refresh_token")),
    }

@router.get("/oauth/debug")
def oauth_debug():
    from app.services.salesforce_oauth import build_authorize_url
    return {"authorize_url": build_authorize_url("debug")}