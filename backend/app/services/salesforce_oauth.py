# backend/app/services/salesforce_oauth.py
import os
import time
import uuid
from typing import Optional, Dict, Any
from urllib.parse import urlencode, urlparse

import httpx
from itsdangerous import Signer, BadSignature
from simple_salesforce import Salesforce

# === Config desde entorno ===
SF_CLIENT_ID = os.getenv("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.getenv("SF_CLIENT_SECRET", "")
SF_REDIRECT_URI = os.getenv("SF_REDIRECT_URI", "")
SF_DOMAIN = os.getenv("SF_DOMAIN", "login")  # 'login' | 'test' | my-domain (corto) | host completo
SF_SCOPES = os.getenv("SF_SCOPES", "refresh_token api id web")
SF_API_VERSION = os.getenv("SALESFORCE_API_VERSION", "59.0").lstrip("v")

FRONTEND_BASE = os.getenv("FRONTEND_BASE", "http://localhost")

# Cookies / sesión
COOKIE_SECRET = os.getenv("COOKIE_SECRET", "dev-secret-change-me")
COOKIE_NAME = "sf_session"
STATE_COOKIE = "sf_oauth_state"

_signer = Signer(COOKIE_SECRET)

# === Sesiones simples en memoria (mover a Redis/DB en prod) ===
SESSIONS: Dict[str, Dict[str, Any]] = {}


# ---------------------------
# Helpers de dominio y URLs
# ---------------------------
def _sf_host() -> str:
    """
    Devuelve el host correcto de Salesforce para OAuth según SF_DOMAIN:
    - 'login' -> 'login.salesforce.com'
    - 'test'  -> 'test.salesforce.com'
    - 'mi-dominio'            -> 'mi-dominio.my.salesforce.com'
    - 'mi-dominio.my'         -> 'mi-dominio.my.salesforce.com'
    - 'mi-dominio.my.salesforce.com' (o un host completo) -> tal cual
    - si viene con esquema, extrae netloc
    """
    dom = (SF_DOMAIN or "login").strip()
    if dom.startswith("http://") or dom.startswith("https://"):
        return urlparse(dom).netloc
    if "." in dom:
        # ya es un host (e.g. innodia.my.salesforce.com)
        return dom
    if dom in ("login", "test"):
        return f"{dom}.salesforce.com"
    # my domain corto
    if dom.endswith(".my"):
        return f"{dom}.salesforce.com"
    return f"{dom}.my.salesforce.com"


def _authorize_url() -> str:
    return f"https://{_sf_host()}/services/oauth2/authorize"


def _token_url() -> str:
    return f"https://{_sf_host()}/services/oauth2/token"


# ---------------------------
# Firma / verificación cookies
# ---------------------------
def sign_value(v: str) -> str:
    return _signer.sign(v.encode()).decode()


def unsign_value(v: str) -> Optional[str]:
    try:
        return _signer.unsign(v.encode()).decode()
    except BadSignature:
        return None


# ---------------------------
# OAuth: authorize / token
# ---------------------------
def build_authorize_url(state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": SF_CLIENT_ID,
        "redirect_uri": SF_REDIRECT_URI,
        "scope": SF_SCOPES,
        "state": state,
        # Forzar pantalla de login si procede (evita sesiones previas extrañas)
        "prompt": "login",
    }
    return f"{_authorize_url()}?{urlencode(params)}"


async def exchange_code_for_token(code: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _token_url(),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": SF_CLIENT_ID,
                "client_secret": SF_CLIENT_SECRET,
                "redirect_uri": SF_REDIRECT_URI,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _token_url(),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": SF_CLIENT_ID,
                "client_secret": SF_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------
# Gestión de sesiones
# ---------------------------
def create_session(token_payload: Dict[str, Any]) -> str:
    """
    Guarda lo básico de la sesión OAuth devuelta por Salesforce.
    """
    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "access_token": token_payload["access_token"],
        "instance_url": token_payload["instance_url"],
        "issued_at": int(time.time()),
        "refresh_token": token_payload.get("refresh_token"),
        "token_type": token_payload.get("token_type", "Bearer"),
        "id_url": token_payload.get("id"),  # endpoint de identidad
    }
    return session_id


def destroy_session(session_id: str):
    SESSIONS.pop(session_id, None)


def get_identity_from_session_id(session_id: str) -> Optional[Dict[str, Any]]:
    return SESSIONS.get(session_id)


def get_salesforce_from_session_id(session_id: str) -> Optional[Salesforce]:
    data = SESSIONS.get(session_id)
    if not data:
        return None
    # Nota: simple_salesforce ignora 'version' si se pasa vacío; usamos la env saneada.
    return Salesforce(
        instance_url=data["instance_url"],
        session_id=data["access_token"],
        version=SF_API_VERSION,  # p.ej. "59.0"
    )