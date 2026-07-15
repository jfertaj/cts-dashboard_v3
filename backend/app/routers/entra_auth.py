import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.services.entra_oauth import oauth, REDIRECT_PATH, FRONTEND_BASE, extract_identity, is_innodia
from app.services.app_session import (
    COOKIE_NAME, sign_value, unsign_value, create_session, get_session, destroy_session,
)

log = logging.getLogger("entra_auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _secure() -> bool:
    if os.getenv("ENABLE_SECURE_COOKIES", "").strip().lower() in ("1", "true", "yes"):
        return True
    return FRONTEND_BASE.lower().startswith("https://")


_COOKIE_KW = dict(httponly=True, samesite="lax", **({"secure": True} if _secure() else {}))


def _sid_from_request(request: Request) -> Optional[str]:
    """Read + unsign the app-session cookie; returns None if absent or tampered."""
    signed = request.cookies.get(COOKIE_NAME)
    return unsign_value(signed) if signed else None


def _safe_next(raw: Optional[str]) -> str:
    """Constrain the post-login redirect to a same-site path.

    Prevents open redirects: `next` is concatenated onto FRONTEND_BASE, so a
    value like ``@evil.com`` or ``//evil.com`` would otherwise send the user to
    an attacker host. Only a single-slash-rooted path is allowed.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


@router.get("/login")
async def login(request: Request, next: Optional[str] = "/"):
    request.session["next"] = _safe_next(next)
    redirect_uri = str(request.base_url).rstrip("/") + REDIRECT_PATH
    return await oauth.entra.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    try:
        token = await oauth.entra.authorize_access_token(request)  # validates id_token + nonce
        userinfo = token.get("userinfo") or {}
        ident = extract_identity(userinfo)
        if not ident["email"] or not is_innodia(ident["email"]):
            return RedirectResponse(f"{FRONTEND_BASE}/?auth_error=not_innodia")
        sid = create_session(ident["email"], ident["name"] or "", ident["oid"] or "")
    except Exception as exc:  # bad state/nonce, exchange failure, or session store error
        log.warning("Auth callback failed: %s", exc)
        return RedirectResponse(f"{FRONTEND_BASE}/?auth_error=login_failed")

    next_url = _safe_next(request.session.pop("next", "/"))
    resp = RedirectResponse(f"{FRONTEND_BASE}{next_url}", status_code=302)
    resp.set_cookie(COOKIE_NAME, sign_value(sid), **_COOKIE_KW)
    return resp


@router.get("/me")
def me(request: Request):
    if os.getenv("AUTH_DISABLED", "").strip() == "1":
        return {"authenticated": True, "email": "dev@innodia.org", "name": "Dev User"}
    sid = _sid_from_request(request)
    sess = get_session(sid) if sid else None
    if not sess:
        return {"authenticated": False}
    return {"authenticated": True, "email": sess["email"], "name": sess["name"]}


@router.get("/logout")
def logout(request: Request):
    sid = _sid_from_request(request)
    if sid:
        destroy_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp
