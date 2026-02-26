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

# === Sesiones en PostgreSQL (compartidas entre instancias y reinicios) ===
_DATABASE_URL = os.getenv("DATABASE_URL", "")
# psycopg3 requires postgresql:// scheme, not SQLAlchemy's postgresql+psycopg://
_PSYCOPG_URL = (
    _DATABASE_URL
    .replace("postgresql+psycopg://", "postgresql://", 1)
    .replace("postgres+psycopg://", "postgresql://", 1)
)

def _ensure_sessions_table():
    """CREATE TABLE IF NOT EXISTS sf_sessions — called once at startup."""
    import psycopg
    try:
        with psycopg.connect(_PSYCOPG_URL) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sf_sessions (
                    session_id   TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    instance_url TEXT NOT NULL,
                    issued_at    INTEGER NOT NULL,
                    refresh_token TEXT,
                    token_type   TEXT DEFAULT 'Bearer',
                    id_url       TEXT
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"WARN: could not create sf_sessions table: {e}")

_sessions_table_ready = False
def _init_sessions():
    global _sessions_table_ready
    if not _sessions_table_ready:
        _ensure_sessions_table()
        _sessions_table_ready = True

def _db_write_session(session_id: str, data: Dict[str, Any]):
    import psycopg
    _init_sessions()
    with psycopg.connect(_PSYCOPG_URL) as conn:
        conn.execute(
            """INSERT INTO sf_sessions (session_id, access_token, instance_url, issued_at, refresh_token, token_type, id_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (session_id) DO UPDATE SET
                 access_token  = EXCLUDED.access_token,
                 instance_url  = EXCLUDED.instance_url,
                 issued_at     = EXCLUDED.issued_at,
                 refresh_token = EXCLUDED.refresh_token,
                 token_type    = EXCLUDED.token_type,
                 id_url        = EXCLUDED.id_url
            """,
            (session_id, data["access_token"], data["instance_url"], data["issued_at"],
             data.get("refresh_token"), data.get("token_type", "Bearer"), data.get("id_url")),
        )
        conn.commit()

def _db_read_session(session_id: str) -> Optional[Dict[str, Any]]:
    import psycopg
    _init_sessions()
    try:
        with psycopg.connect(_PSYCOPG_URL) as conn:
            row = conn.execute(
                "SELECT access_token, instance_url, issued_at, refresh_token, token_type, id_url "
                "FROM sf_sessions WHERE session_id = %s",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "access_token":  row[0], "instance_url": row[1], "issued_at": row[2],
            "refresh_token": row[3], "token_type":   row[4], "id_url":    row[5],
        }
    except Exception as e:
        print(f"WARN: sf_sessions read failed: {e}")
        return None

def _db_delete_session(session_id: str):
    import psycopg
    _init_sessions()
    try:
        with psycopg.connect(_PSYCOPG_URL) as conn:
            conn.execute("DELETE FROM sf_sessions WHERE session_id = %s", (session_id,))
            conn.commit()
    except Exception as e:
        print(f"WARN: sf_sessions delete failed: {e}")


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
    """Persist the OAuth session to PostgreSQL and return the session_id."""
    session_id = str(uuid.uuid4())
    data = {
        "access_token":  token_payload["access_token"],
        "instance_url":  token_payload["instance_url"],
        "issued_at":     int(time.time()),
        "refresh_token": token_payload.get("refresh_token"),
        "token_type":    token_payload.get("token_type", "Bearer"),
        "id_url":        token_payload.get("id"),
    }
    _db_write_session(session_id, data)
    return session_id


def destroy_session(session_id: str):
    _db_delete_session(session_id)


def get_identity_from_session_id(session_id: str) -> Optional[Dict[str, Any]]:
    return _db_read_session(session_id)


def get_salesforce_from_session_id(session_id: str) -> Optional[Salesforce]:
    data = _db_read_session(session_id)
    if not data:
        return None
    return Salesforce(
        instance_url=data["instance_url"],
        session_id=data["access_token"],
        version=SF_API_VERSION,
    )