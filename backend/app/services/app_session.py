from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import text

from app.database import engine

log = logging.getLogger("app_session")

COOKIE_NAME = "cts_session"
STATE_COOKIE = "cts_oauth_state"

_serializer: Optional[URLSafeSerializer] = None


def _reload_signer() -> None:
    """(Re)build the cookie signer from APP_SESSION_SECRET.

    Production must supply APP_SESSION_SECRET (main.py fails fast without it). If
    it is missing here we fall back to a random per-process secret rather than an
    empty key — an empty key is publicly known and would make session cookies
    trivially forgeable. The random fallback keeps dev/test safe (cookies just do
    not survive a restart, which is fine when Entra auth is bypassed locally).
    """
    global _serializer
    secret = os.getenv("APP_SESSION_SECRET", "")
    if not secret:
        secret = secrets.token_hex(32)
        log.warning("APP_SESSION_SECRET not set; using a random per-process signing key")
    _serializer = URLSafeSerializer(secret, salt="cts-app-session")


_reload_signer()


def sign_value(raw: str) -> str:
    assert _serializer is not None
    return _serializer.dumps(raw)


def unsign_value(signed: str) -> Optional[str]:
    assert _serializer is not None
    try:
        return _serializer.loads(signed)
    except BadSignature:
        return None


def create_session(email: str, name: str, oid: str, ttl_seconds: int = 12 * 3600) -> str:
    sid = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO app_sessions (session_id, email, name, oid, issued_at, expires_at) "
                "VALUES (:sid, :email, :name, :oid, :issued, :expires)"
            ),
            {"sid": sid, "email": email, "name": name, "oid": oid,
             "issued": now, "expires": now + timedelta(seconds=ttl_seconds)},
        )
    return sid


def get_session(session_id: str) -> Optional[dict]:
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT email, name, oid, expires_at FROM app_sessions WHERE session_id = :sid"),
            {"sid": session_id},
        ).fetchone()
    if not row:
        return None
    if row.expires_at and row.expires_at < datetime.now(timezone.utc):
        return None
    return {"email": row.email, "name": row.name, "oid": row.oid}


def destroy_session(session_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_sessions WHERE session_id = :sid"), {"sid": session_id})
