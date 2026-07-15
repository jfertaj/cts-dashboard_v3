import os
from fastapi import HTTPException, Request

from app.services.app_session import COOKIE_NAME, unsign_value, get_session

_DEV_USER = {"email": "dev@innodia.org", "name": "Dev User", "oid": "dev"}


def require_user(request: Request) -> dict:
    """FastAPI dependency gating data routers.

    With ``AUTH_DISABLED=1`` returns a fixed dev user (local development).
    Otherwise resolves the signed session cookie to a live session, raising
    ``HTTPException(401)`` when absent, invalid, or expired.
    """
    if os.getenv("AUTH_DISABLED", "").strip() == "1":
        return dict(_DEV_USER)
    signed = request.cookies.get(COOKIE_NAME)
    sid = unsign_value(signed) if signed else None
    sess = get_session(sid) if sid else None
    if not sess:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated. Please sign in with innodia.org.",
        )
    return sess
