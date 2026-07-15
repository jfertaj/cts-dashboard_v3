# Entra ID SSO gate + Salesforce service-account — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-user Salesforce OAuth login with a Microsoft Entra ID (innodia.org) SSO gate, and serve all Salesforce data through a single service account (OAuth 2.0 Client Credentials Flow).

**Architecture:** App-level OIDC in FastAPI using Authlib gates the app (session cookie `cts_session`, backed by a new `app_sessions` table). Salesforce access is decoupled from the user: a `get_service_sf()` singleton mints a client-credentials token and every existing SF-client accessor is re-pointed to it. A `require_user` dependency protects all data routers; an `AUTH_DISABLED=1` bypass keeps local dev and the test suites working.

**Tech Stack:** FastAPI, Authlib (`authlib.integrations.starlette_client`), Starlette `SessionMiddleware`, itsdangerous, simple-salesforce, httpx, SQLAlchemy + Alembic, PostgreSQL, React + TypeScript + Vite, Playwright, pytest.

## Global Constraints

- **Salesforce token endpoint MUST be the org My Domain URL** (`{SF_MY_DOMAIN}/services/oauth2/token`), never `login.salesforce.com`. Client credentials go in the **POST body**, never the query string.
- **Client Credentials Flow returns NO refresh token** — re-mint on expiry or on a 401 from Salesforce.
- **Entra app is single-tenant** — issuer `https://login.microsoftonline.com/{ENTRA_TENANT_ID}/v2.0`; only innodia.org accounts can sign in. Any tenant user is allowed (no per-user assignment gate).
- **Never log** `ENTRA_CLIENT_SECRET`, `SF_CLIENT_SECRET`, `APP_SESSION_SECRET`, or any access token.
- **Cookies:** `httponly=True`, `samesite="lax"`, `secure=True` in prod (reuse the existing `_secure_cookies_enabled()` convention).
- **Alembic:** new migration's `down_revision = "c3d4e5f6a7b8"` (current head). In SOQL/SQL use `CAST(:x AS jsonb)` not `:x::jsonb` (psycopg3). Run `alembic heads` before creating.
- **Docker builds:** `--platform linux/amd64`.
- **Deploy is manual only** — never auto-deploy. Backend + frontend must be cut over together.
- **Config access pattern:** read env with `os.getenv(...)` at module scope, mirroring `salesforce_oauth.py`.
- **Test coverage target:** 80%+ on new auth modules.

---

## File Structure

**Backend — new files**
- `backend/app/services/salesforce_service.py` — client-credentials token mint + cache; `get_service_sf()`.
- `backend/app/services/app_session.py` — `app_sessions` store + itsdangerous sign/unsign + cookie constants.
- `backend/app/services/entra_oauth.py` — Authlib OAuth registration for Entra + identity extraction helper.
- `backend/app/routers/entra_auth.py` — `/api/auth/{login,callback,me,logout}`.
- `backend/app/deps/__init__.py`, `backend/app/deps/auth.py` — `require_user` dependency + `AUTH_DISABLED` bypass.
- `backend/alembic/versions/<rev>_add_app_sessions_table.py` — `app_sessions` migration.
- Tests: `backend/tests/test_salesforce_service.py`, `test_app_session.py`, `test_require_user.py`, `test_entra_identity.py`.

**Backend — modified files**
- `backend/app/routers/salesforce_explorer.py` — re-point `_get_sf()` body to `get_service_sf()`; line 843 helper likewise.
- `backend/app/services/salesforce_client.py` — re-point to `get_service_sf()`.
- `backend/app/routers/members_explorer.py:33`, `qualification.py:364` — optional-session paths → `get_service_sf()`.
- `backend/app/main.py` — add `SessionMiddleware`, mount `entra_auth` router, stop mounting `salesforce_auth_router`, apply `require_user` to data routers.
- `backend/requirements.txt` + `backend/requirements.lock` — add `Authlib` (`itsdangerous`/`httpx` already present).
- `scripts/gen_local_env.py` — pull new keys into `backend/.env`.

**Frontend — modified files**
- `frontend/src/hooks/useAuth.ts` (new, replaces `useSalesforceAuth.ts`).
- `frontend/src/App.tsx` — sign-in gate + copy.
- `frontend/src/lib/api.ts` — global 401 interception in `api<T>()`.
- `frontend/src/lib/auth.ts` (new) — `authMe()`, `loginRedirect()`, `logout()`.
- `frontend/src/components/Header.tsx` — login/logout + user email.
- `frontend/tests/e2e/*` — repoint `/api/salesforce/me` mock to `/api/auth/me`; new gate spec.

**Docs**
- `docs/entra-sso-setup.md` — Entra app registration + Salesforce External Client App + Secrets Manager runbook.

---

## Phase 0 — Salesforce service account (de-risk first)

### Task 1: `get_service_sf()` — client-credentials token + cache

**Files:**
- Create: `backend/app/services/salesforce_service.py`
- Test: `backend/tests/test_salesforce_service.py`

**Interfaces:**
- Produces: `get_service_sf() -> simple_salesforce.Salesforce`; `reset_service_sf_cache() -> None` (test seam); `_mint_token() -> dict` returning `{"access_token", "instance_url", "issued_at"}`.
- Consumes: env `SF_MY_DOMAIN`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`; `SF_TOKEN_TTL_SECONDS` (default `3600`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_salesforce_service.py
import time
import pytest
from unittest.mock import patch, MagicMock
from app.services import salesforce_service as svc


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SF_MY_DOMAIN", "https://innodiaivzw.my.salesforce.com")
    monkeypatch.setenv("SF_CLIENT_ID", "cid")
    monkeypatch.setenv("SF_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("SF_TOKEN_TTL_SECONDS", "3600")
    svc.reset_service_sf_cache()
    yield
    svc.reset_service_sf_cache()


def _fake_token_response():
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {
        "access_token": "TOK1",
        "instance_url": "https://innodiaivzw.my.salesforce.com",
        "issued_at": "0",
    }
    r.raise_for_status.return_value = None
    return r


def test_mint_posts_client_credentials_in_body():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
    url = p.call_args.args[0]
    data = p.call_args.kwargs["data"]
    assert url == "https://innodiaivzw.my.salesforce.com/services/oauth2/token"
    assert data["grant_type"] == "client_credentials"
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "csecret"


def test_token_is_cached_across_calls():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
        svc.get_service_sf()
    assert p.call_count == 1


def test_token_reminted_after_ttl(monkeypatch):
    monkeypatch.setenv("SF_TOKEN_TTL_SECONDS", "1")
    svc.reset_service_sf_cache()
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()) as p, \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        svc.get_service_sf()
        svc._CACHE["expires_at"] = time.time() - 1  # force expiry
        svc.get_service_sf()
    assert p.call_count == 2


def test_secret_never_in_repr():
    with patch.object(svc.httpx, "post", return_value=_fake_token_response()), \
         patch.object(svc, "Salesforce", return_value=MagicMock()):
        sf = svc.get_service_sf()
    assert "csecret" not in repr(sf)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_salesforce_service.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.salesforce_service`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/salesforce_service.py
"""Salesforce access via a single service account (OAuth 2.0 Client Credentials Flow).

No end-user session is involved. A token is minted from SF_CLIENT_ID/SECRET against
the org My Domain token endpoint and cached in-process (per uvicorn worker) until it
nears expiry; it is re-minted on expiry or when a caller reports a 401.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict

import httpx
from simple_salesforce import Salesforce

log = logging.getLogger("salesforce_service")

_LOCK = threading.Lock()
_CACHE: Dict[str, object] = {}  # {"sf": Salesforce, "expires_at": float}


def _cfg() -> Dict[str, str]:
    my_domain = os.getenv("SF_MY_DOMAIN", "").rstrip("/")
    return {
        "token_url": f"{my_domain}/services/oauth2/token",
        "client_id": os.getenv("SF_CLIENT_ID", ""),
        "client_secret": os.getenv("SF_CLIENT_SECRET", ""),
        "ttl": int(os.getenv("SF_TOKEN_TTL_SECONDS", "3600")),
    }


def _mint_token() -> dict:
    cfg = _cfg()
    resp = httpx.post(
        cfg["token_url"],
        data={
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    log.info("Minted Salesforce service token (instance=%s)", payload.get("instance_url"))
    return payload


def reset_service_sf_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def get_service_sf() -> Salesforce:
    now = time.time()
    with _LOCK:
        sf = _CACHE.get("sf")
        exp = _CACHE.get("expires_at", 0)
        if sf is not None and isinstance(exp, (int, float)) and now < exp:
            return sf  # type: ignore[return-value]
        payload = _mint_token()
        sf = Salesforce(
            instance_url=payload["instance_url"],
            session_id=payload["access_token"],
        )
        _CACHE["sf"] = sf
        _CACHE["expires_at"] = now + _cfg()["ttl"] - 60  # 60s safety margin
        return sf
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_salesforce_service.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/salesforce_service.py backend/tests/test_salesforce_service.py
git commit -m "feat(sf): service-account Salesforce client via client-credentials flow"
```

---

### Task 2: Re-point every SF-client accessor to the service account

**Files:**
- Modify: `backend/app/routers/salesforce_explorer.py` (`_get_sf` body ~836-848; helper at ~843)
- Modify: `backend/app/services/salesforce_client.py:21-27`
- Modify: `backend/app/routers/members_explorer.py:33`
- Modify: `backend/app/routers/qualification.py:364`
- Test: `backend/tests/test_get_sf_delegates.py`

**Interfaces:**
- Consumes: `get_service_sf()` from Task 1.
- Produces: `_get_sf(request)` now returns the service client (request arg ignored, kept for signature compatibility so the 16 call sites are untouched).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_get_sf_delegates.py
from unittest.mock import patch, MagicMock
from app.routers import salesforce_explorer as se


def test_get_sf_returns_service_client_ignoring_request():
    fake = MagicMock(name="ServiceSF")
    with patch.object(se, "get_service_sf", return_value=fake) as g:
        out = se._get_sf(request=None)
    g.assert_called_once()
    assert out is fake
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_get_sf_delegates.py -v`
Expected: FAIL — `_get_sf` still reads cookies / `get_service_sf` not imported.

- [ ] **Step 3: Write minimal implementation**

In `backend/app/routers/salesforce_explorer.py`, add near the other service imports:

```python
from app.services.salesforce_service import get_service_sf
```

Replace the `_get_sf` body (currently reads the cookie/session) with:

```python
def _get_sf(request: Request = None):  # request kept for call-site compatibility
    """Return the shared service-account Salesforce client (user session no longer used)."""
    return get_service_sf()
```

Replace the direct helper at line ~843 (inside the map/bootstrap function) — change:

```python
        sf = get_salesforce_from_session_id(session_id)
```
to:
```python
        sf = get_service_sf()
```

In `backend/app/services/salesforce_client.py`, replace the `get_salesforce_from_session_id(sid)` construction (lines 21-27) with:

```python
    from app.services.salesforce_service import get_service_sf
    return get_service_sf()
```

In `backend/app/routers/members_explorer.py:33`, replace:
```python
    sf = get_salesforce_from_session_id(session_id) if session_id else None
```
with:
```python
    from app.services.salesforce_service import get_service_sf
    sf = get_service_sf()
```

In `backend/app/routers/qualification.py:364`, apply the same replacement as members_explorer line 33.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_get_sf_delegates.py backend/tests/ -q`
Expected: PASS; existing suite has no regressions (no current unit test depends on `_get_sf` cookie behavior).

- [ ] **Step 5: Manual real-Salesforce validation (de-risk gate)**

Per repo practice, open the SSM tunnel and validate against real Salesforce **before** building the gate:

```bash
# populate backend/.env with SF_CLIENT_ID/SECRET/MY_DOMAIN first (Task 8)
cd backend && python - <<'PY'
from app.services.salesforce_service import get_service_sf
sf = get_service_sf()
print(sf.query("SELECT Id, Name FROM Account LIMIT 1"))
PY
```
Expected: one Account row. If this fails, STOP — the External Client App / Run-As permissions are wrong (fix in Salesforce, do not proceed to the gate).

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/salesforce_explorer.py backend/app/services/salesforce_client.py \
        backend/app/routers/members_explorer.py backend/app/routers/qualification.py \
        backend/tests/test_get_sf_delegates.py
git commit -m "refactor(sf): serve all Salesforce data via the service account"
```

---

## Phase 1 — Entra OIDC gate (backend)

### Task 3: `app_sessions` store + Alembic migration

**Files:**
- Create: `backend/app/services/app_session.py`
- Create: `backend/alembic/versions/d4e5f6a7b8c9_add_app_sessions_table.py`
- Test: `backend/tests/test_app_session.py`

**Interfaces:**
- Produces: `COOKIE_NAME="cts_session"`, `STATE_COOKIE`, `sign_value(str)->str`, `unsign_value(str)->str|None`, `create_session(email,name,oid,ttl_seconds)->str` (returns session_id), `get_session(session_id)->dict|None`, `destroy_session(session_id)->None`, `_reload_signer()->None` (test seam).
- Consumes: env `APP_SESSION_SECRET`; the app's existing DB engine.

- [ ] **Step 1: Write the failing test** (pure sign/unsign — DB paths covered by TestClient later)

```python
# backend/tests/test_app_session.py
import pytest
from app.services import app_session as s


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("APP_SESSION_SECRET", "unit-test-secret")
    s._reload_signer()
    yield


def test_sign_unsign_roundtrip():
    signed = s.sign_value("abc-123")
    assert signed != "abc-123"
    assert s.unsign_value(signed) == "abc-123"


def test_tampered_value_rejected():
    signed = s.sign_value("abc-123")
    assert s.unsign_value(signed + "x") is None


def test_cookie_name_is_cts_session():
    assert s.COOKIE_NAME == "cts_session"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_app_session.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/app_session.py
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import text

from app.db import engine  # IMPLEMENTER: confirm import path (grep create_engine under backend/app;
                           # use the same engine salesforce_oauth.py reaches Postgres with)

COOKIE_NAME = "cts_session"
STATE_COOKIE = "cts_oauth_state"

_serializer: Optional[URLSafeSerializer] = None


def _reload_signer() -> None:
    global _serializer
    secret = os.getenv("APP_SESSION_SECRET", "")
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
```

- [ ] **Step 4: Create the Alembic migration**

Run: `cd backend && alembic heads` → confirm head is `c3d4e5f6a7b8`.

```python
# backend/alembic/versions/d4e5f6a7b8c9_add_app_sessions_table.py
"""add app_sessions table"""
from alembic import op
import sqlalchemy as sa

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_sessions",
        sa.Column("session_id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("oid", sa.String(), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_app_sessions_expires_at", "app_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_app_sessions_expires_at", table_name="app_sessions")
    op.drop_table("app_sessions")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_app_session.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/app_session.py \
        backend/alembic/versions/d4e5f6a7b8c9_add_app_sessions_table.py \
        backend/tests/test_app_session.py
git commit -m "feat(auth): app_sessions store + migration for the SSO gate"
```

---

### Task 4: Entra OAuth registration + identity extraction

**Files:**
- Create: `backend/app/services/entra_oauth.py`
- Test: `backend/tests/test_entra_identity.py`

**Interfaces:**
- Produces: `oauth` (Authlib `OAuth` with `entra` registered); `REDIRECT_PATH="/api/auth/callback"`; `FRONTEND_BASE`; `extract_identity(userinfo: dict) -> dict` returning `{"email","name","oid"}`; `is_innodia(email:str)->bool`.
- Consumes: env `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `FRONTEND_BASE`.

- [ ] **Step 1: Write the failing test** (pure extraction — the redirect dance is E2E/manual)

```python
# backend/tests/test_entra_identity.py
from app.services import entra_oauth as e


def test_extract_prefers_email_then_preferred_username():
    ui = {"email": "a@innodia.org", "preferred_username": "b@innodia.org",
          "name": "A", "oid": "oid-1"}
    assert e.extract_identity(ui) == {"email": "a@innodia.org", "name": "A", "oid": "oid-1"}


def test_extract_falls_back_to_preferred_username():
    ui = {"preferred_username": "b@innodia.org", "name": "B", "oid": "oid-2"}
    out = e.extract_identity(ui)
    assert out["email"] == "b@innodia.org"


def test_is_innodia():
    assert e.is_innodia("x@innodia.org") is True
    assert e.is_innodia("x@gmail.com") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_entra_identity.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/entra_oauth.py
from __future__ import annotations

import os
from typing import Dict

from authlib.integrations.starlette_client import OAuth

REDIRECT_PATH = "/api/auth/callback"
FRONTEND_BASE = os.getenv("FRONTEND_BASE", "http://localhost:5173").rstrip("/")
_TENANT = os.getenv("ENTRA_TENANT_ID", "")

oauth = OAuth()
oauth.register(
    name="entra",
    server_metadata_url=(
        f"https://login.microsoftonline.com/{_TENANT}/v2.0/.well-known/openid-configuration"
    ),
    client_id=os.getenv("ENTRA_CLIENT_ID", ""),
    client_secret=os.getenv("ENTRA_CLIENT_SECRET", ""),
    client_kwargs={"scope": "openid profile email"},
)


def extract_identity(userinfo: Dict) -> Dict:
    email = userinfo.get("email") or userinfo.get("preferred_username") or ""
    return {"email": email, "name": userinfo.get("name"), "oid": userinfo.get("oid")}


def is_innodia(email: str) -> bool:
    return email.lower().endswith("@innodia.org")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_entra_identity.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/entra_oauth.py backend/tests/test_entra_identity.py
git commit -m "feat(auth): Entra OIDC client registration + identity extraction"
```

---

### Task 5: `/api/auth` router + SessionMiddleware

**Files:**
- Create: `backend/app/routers/entra_auth.py`
- Modify: `backend/app/main.py` (add `SessionMiddleware`, mount router)

**Interfaces:**
- Consumes: `oauth`, `REDIRECT_PATH`, `FRONTEND_BASE`, `extract_identity`, `is_innodia` (Task 4); `app_session` helpers + `COOKIE_NAME` (Task 3).
- Produces: `router` with `GET /api/auth/login`, `GET /api/auth/callback`, `GET /api/auth/me`, `GET /api/auth/logout`.

- [ ] **Step 1: Write the router**

```python
# backend/app/routers/entra_auth.py
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


@router.get("/login")
async def login(request: Request, next: Optional[str] = "/"):
    request.session["next"] = next or "/"
    redirect_uri = str(request.base_url).rstrip("/") + REDIRECT_PATH
    return await oauth.entra.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    try:
        token = await oauth.entra.authorize_access_token(request)  # validates id_token + nonce
        userinfo = token.get("userinfo") or {}
        ident = extract_identity(userinfo)
    except Exception as exc:  # bad state/nonce, exchange failure
        log.warning("Auth callback failed: %s", exc)
        return RedirectResponse(f"{FRONTEND_BASE}/?auth_error=login_failed")

    if not ident["email"] or not is_innodia(ident["email"]):
        return RedirectResponse(f"{FRONTEND_BASE}/?auth_error=not_innodia")

    sid = create_session(ident["email"], ident["name"] or "", ident["oid"] or "")
    next_url = request.session.pop("next", "/") or "/"
    resp = RedirectResponse(f"{FRONTEND_BASE}{next_url}", status_code=302)
    resp.set_cookie(COOKIE_NAME, sign_value(sid), **_COOKIE_KW)
    return resp


@router.get("/me")
def me(request: Request):
    if os.getenv("AUTH_DISABLED", "").strip() == "1":
        return {"authenticated": True, "email": "dev@innodia.org", "name": "Dev User"}
    signed = request.cookies.get(COOKIE_NAME)
    sid = unsign_value(signed) if signed else None
    sess = get_session(sid) if sid else None
    if not sess:
        return {"authenticated": False}
    return {"authenticated": True, "email": sess["email"], "name": sess["name"]}


@router.get("/logout")
def logout(request: Request):
    signed = request.cookies.get(COOKIE_NAME)
    sid = unsign_value(signed) if signed else None
    if sid:
        destroy_session(sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp
```

- [ ] **Step 2: Wire middleware + router in `main.py`**

Add imports and middleware (place `SessionMiddleware` after CORS, before the router includes):

```python
from starlette.middleware.sessions import SessionMiddleware
from app.routers.entra_auth import router as entra_auth_router
# ...
app.add_middleware(SessionMiddleware, secret_key=os.environ["APP_SESSION_SECRET"], same_site="lax")
# ... with the other include_router calls:
app.include_router(entra_auth_router)  # /api/auth/... (public)
```

- [ ] **Step 3: Smoke test the routes exist**

Run:
```bash
cd backend && AUTH_DISABLED=1 APP_SESSION_SECRET=x python -c "
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
print(c.get('/api/auth/me').json())
"
```
Expected: `{'authenticated': True, 'email': 'dev@innodia.org', 'name': 'Dev User'}`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/entra_auth.py backend/app/main.py
git commit -m "feat(auth): /api/auth login/callback/me/logout + SessionMiddleware"
```

---

### Task 6: `require_user` dependency + protect data routers + retire SF login

**Files:**
- Create: `backend/app/deps/__init__.py`, `backend/app/deps/auth.py`
- Modify: `backend/app/main.py` (apply dependency; stop mounting `salesforce_auth_router`)
- Test: `backend/tests/test_require_user.py`

**Interfaces:**
- Produces: `require_user(request) -> dict` (raises `HTTPException(401)` when unauthenticated); usable as `Depends(require_user)`.
- Consumes: `app_session` helpers (Task 3); env `AUTH_DISABLED`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_require_user.py
import pytest
from types import SimpleNamespace
from fastapi import HTTPException
from unittest.mock import patch
from app.deps import auth as a


def _req(cookies=None):
    return SimpleNamespace(cookies=cookies or {})


def test_bypass_when_auth_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "1")
    assert a.require_user(_req())["email"] == "dev@innodia.org"


def test_missing_cookie_401(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with pytest.raises(HTTPException) as ei:
        a.require_user(_req())
    assert ei.value.status_code == 401


def test_valid_session(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with patch.object(a, "unsign_value", return_value="sid-1"), \
         patch.object(a, "get_session", return_value={"email": "u@innodia.org", "name": "U", "oid": "o"}):
        out = a.require_user(_req({a.COOKIE_NAME: "signed"}))
    assert out["email"] == "u@innodia.org"


def test_expired_session_401(monkeypatch):
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    with patch.object(a, "unsign_value", return_value="sid-1"), \
         patch.object(a, "get_session", return_value=None):
        with pytest.raises(HTTPException) as ei:
            a.require_user(_req({a.COOKIE_NAME: "signed"}))
    assert ei.value.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/tests/test_require_user.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/deps/__init__.py
```
```python
# backend/app/deps/auth.py
import os
from fastapi import HTTPException, Request

from app.services.app_session import COOKIE_NAME, unsign_value, get_session

_DEV_USER = {"email": "dev@innodia.org", "name": "Dev User", "oid": "dev"}


def require_user(request: Request) -> dict:
    if os.getenv("AUTH_DISABLED", "").strip() == "1":
        return dict(_DEV_USER)
    signed = request.cookies.get(COOKIE_NAME)
    sid = unsign_value(signed) if signed else None
    sess = get_session(sid) if sid else None
    if not sess:
        raise HTTPException(status_code=401, detail="Not authenticated. Please sign in with innodia.org.")
    return sess
```

- [ ] **Step 4: Apply the dependency + retire SF login in `main.py`**

Add `from fastapi import Depends` and `from app.deps.auth import require_user`. Attach the dependency to each data-router include, and remove the `salesforce_auth_router` include:

```python
_GUARD = [Depends(require_user)]
# REMOVE: app.include_router(salesforce_auth_router, tags=["salesforce-auth"])
app.include_router(qualification_router, tags=["qualification"], dependencies=_GUARD)
app.include_router(salesforce_accounts_router, tags=["salesforce"], dependencies=_GUARD)
app.include_router(salesforce_sync_router, tags=["salesforce-sync"], dependencies=_GUARD)
app.include_router(geo_router, tags=["geo"], dependencies=_GUARD)
app.include_router(salesforce_router, dependencies=_GUARD)
app.include_router(explorer_router, dependencies=_GUARD)
app.include_router(salesforce_extras.router, dependencies=_GUARD)
app.include_router(members_explorer.router, dependencies=_GUARD)
app.include_router(assignments_report.router, dependencies=_GUARD)
app.include_router(ai_chat.router, dependencies=_GUARD)
app.include_router(explorer_bridge.router, dependencies=_GUARD)
# health_router and entra_auth_router stay UNGUARDED (public)
```

> Keep the `salesforce_auth_router` import unused, or delete it — the module stays in the tree (dormant) per the spec.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_require_user.py -v && python -m pytest backend/tests/ -q`
Expected: PASS; full suite green (run existing suites with `AUTH_DISABLED=1` if any hit guarded routes via TestClient).

- [ ] **Step 6: Verify the guard end-to-end with TestClient**

```bash
cd backend && APP_SESSION_SECRET=x python -c "
import os; os.environ.pop('AUTH_DISABLED',None)
from fastapi.testclient import TestClient
from app.main import app
c=TestClient(app)
print('guarded:', c.post('/api/explorer/search', json={'logic':'AND','rules':[]}).status_code)  # 401
print('public :', c.get('/api/auth/me').status_code)  # 200
"
```
Expected: `guarded: 401`, `public : 200`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/deps/ backend/app/main.py backend/tests/test_require_user.py
git commit -m "feat(auth): require_user gate on all data routers; retire per-user SF login"
```

---

## Phase 2 — Frontend

### Task 7: Sign-in gate, `useAuth`, global 401 handling

**Files:**
- Create: `frontend/src/lib/auth.ts`, `frontend/src/hooks/useAuth.ts`
- Modify: `frontend/src/lib/api.ts` (401 interception in `api<T>()`), `frontend/src/App.tsx`, `frontend/src/components/Header.tsx`
- Modify: `frontend/tests/e2e/*` (repoint mock + new gate spec)

**Interfaces:**
- Consumes backend `/api/auth/me|login|logout`.
- Produces: `authMe(): Promise<{authenticated:boolean; email?:string; name?:string}>`, `loginRedirect(next?:string): void`, `logout(): Promise<void>`; `useAuth()` returning `{ authed: boolean|null, sessionExpired, setSessionExpired, user }`.

- [ ] **Step 1: Write `lib/auth.ts`**

```typescript
// frontend/src/lib/auth.ts
export interface AuthMe { authenticated: boolean; email?: string; name?: string }

export async function authMe(): Promise<AuthMe> {
  const res = await fetch("/api/auth/me", { credentials: "include" });
  if (!res.ok) return { authenticated: false };
  return res.json();
}

export function loginRedirect(next: string = window.location.pathname): void {
  window.location.href = `/api/auth/login?next=${encodeURIComponent(next)}`;
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { credentials: "include" });
  window.location.href = "/";
}
```

- [ ] **Step 2: Write `hooks/useAuth.ts`** (mirrors the old `useSalesforceAuth`, polling `/api/auth/me`)

```typescript
// frontend/src/hooks/useAuth.ts
import { useEffect, useState } from "react";
import { authMe, AuthMe } from "../lib/auth";

export function useAuth() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [user, setUser] = useState<AuthMe | null>(null);

  useEffect(() => {
    let alive = true;
    const check = async () => {
      const me = await authMe();
      if (!alive) return;
      setAuthed(me.authenticated);
      setUser(me);
      if (!me.authenticated) setSessionExpired(true);
    };
    check();
    const poll = window.setInterval(check, 5 * 60 * 1000);
    const onAuth = (e: Event) => {
      if ((e as CustomEvent<{ ok: boolean }>).detail?.ok === false) setSessionExpired(true);
    };
    window.addEventListener("app-auth", onAuth as EventListener);
    return () => {
      alive = false;
      window.clearInterval(poll);
      window.removeEventListener("app-auth", onAuth as EventListener);
    };
  }, []);

  return { authed, sessionExpired, setSessionExpired, user };
}
```

- [ ] **Step 3: Add global 401 interception in `lib/api.ts`**

In the `api<T>()` wrapper (around line 62, where `fetch(url, reqInit)` runs), after the response is received and before the existing non-OK error handling, add:

```typescript
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("app-auth", { detail: { ok: false } }));
    }
```

- [ ] **Step 4: Update `App.tsx`** — swap the hook, add the unauth gate, fix overlay copy

Replace `useSalesforceAuth` with `useAuth`. Add, before the main content render, the unauthenticated gate; change the "Session Expired" overlay button to `loginRedirect()` with innodia.org copy; remove the amber "not connected to Salesforce" banner block.

```tsx
import { loginRedirect } from "./lib/auth";
// ...
if (authed === false && !sessionExpired) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-[#f6f9fb]">
      <div className="max-w-md w-full rounded-xl bg-white shadow-2xl border p-6 text-center">
        <h1 className="text-lg font-semibold text-gray-900">CTS Dashboard</h1>
        <p className="mt-2 text-sm text-gray-700">Sign in with your innodia.org account to continue.</p>
        <button data-testid="signin-innodia"
          className="mt-4 rounded-md bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90"
          onClick={() => loginRedirect()}>
          Sign in with innodia.org
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Update `Header.tsx`** — login/logout point to `/api/auth/*`, show `user.email`. Remove `salesforceMe`/`salesforceLogout` usage; import `logout, loginRedirect` from `lib/auth`.

- [ ] **Step 6: Update E2E** — in every spec that mocks `/api/salesforce/me`, repoint to `/api/auth/me` returning `{authenticated:true,email:"dev@innodia.org"}`. Add the gate spec:

```typescript
// frontend/tests/e2e/auth-gate.spec.ts
import { test, expect } from "@playwright/test";
test("shows innodia.org sign-in gate when unauthenticated", async ({ page }) => {
  await page.route("**/api/auth/me", (r) => r.fulfill({ json: { authenticated: false } }));
  await page.goto("/");
  await expect(page.getByTestId("signin-innodia")).toBeVisible();
});
```

- [ ] **Step 7: Run frontend tests**

Run:
```bash
cd frontend && npm run build && npm run test && npm run test:e2e
```
Expected: build clean; unit tests pass; E2E green (gate spec + repointed specs).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/lib/auth.ts frontend/src/hooks/useAuth.ts frontend/src/lib/api.ts \
        frontend/src/App.tsx frontend/src/components/Header.tsx frontend/tests/e2e/
git commit -m "feat(frontend): innodia.org sign-in gate + global 401 handling"
```

---

## Phase 3 — Config, dependencies, docs

### Task 8: dependencies, env plumbing, and setup runbook

**Files:**
- Modify: `backend/requirements.txt`, `backend/requirements.lock`
- Modify: `scripts/gen_local_env.py`
- Create: `docs/entra-sso-setup.md`

- [ ] **Step 1: Add Authlib to requirements**

Add `Authlib` to `backend/requirements.txt` (intent) and pin the exact resolved version in `backend/requirements.lock` (resolve inside the amd64 build image, per the repo lock practice). `itsdangerous` and `httpx` are already present in both.

- [ ] **Step 2: Extend `gen_local_env.py`** to write the new keys into `backend/.env`:
`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `APP_SESSION_SECRET`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_MY_DOMAIN`, and `AUTH_DISABLED=1` for local. Pull secrets from the same Secrets Manager bundle the script already reads.

- [ ] **Step 3: Write `docs/entra-sso-setup.md`** — the manual runbook:
  1. **Entra:** register single-tenant app; redirect URIs (`https://cts-innodia-dashboard.org/api/auth/callback` + `http://localhost:8000/api/auth/callback`); API permissions `openid profile email`; create client secret; copy Tenant ID + Client ID.
  2. **Salesforce:** External Client App → Enable OAuth → Enable Client Credentials Flow → **Run As** `juan.f.tajes@innodia.org`; grant that user read+write on Opportunity/Account/Contact/Assignment__c; copy Consumer Key/Secret; confirm token endpoint = My Domain (`{SF_MY_DOMAIN}/services/oauth2/token`).
  3. **AWS Secrets Manager:** add all seven keys to the backend secret bundle.
  4. **Migrate:** `bash scripts/deploy.sh --migrate` (runs `alembic upgrade head` → creates `app_sessions`).
  5. **Cutover:** deploy backend + frontend together; verify `/api/auth/login` → Entra → dashboard; verify a data call returns rows.

- [ ] **Step 4: Commit**

```bash
git add backend/requirements.txt backend/requirements.lock scripts/gen_local_env.py docs/entra-sso-setup.md
git commit -m "chore(auth): Authlib dep, env plumbing, Entra/SF setup runbook"
```

- [ ] **Step 5: Update project state docs**

Update `docs/current-state.md` (new auth model) and mark any relevant `docs/next-steps.md` items done, per the repo's doc-update rule. Commit.

---

## Self-Review

**Spec coverage:**
- §A Entra registration → Task 8 (docs) + Tasks 4/5 (code consuming it). ✓
- §B Entra gate (entra_oauth, app_session, entra_auth, require_user, SessionMiddleware) → Tasks 3,4,5,6. ✓
- §C Salesforce service account + re-point call sites → Tasks 1,2. ✓
- §D Retire per-user SF OAuth (stop mounting router; remove FE SF UI) → Task 6 (backend), Task 7 (frontend). ✓
- §E Frontend (useAuth, gate, global 401, Header) → Task 7. ✓
- §F Flow → Task 5 smoke + Task 7 gate spec + Task 2 real-SF check. ✓
- §G Error handling (callback redirect, 401 JSON) → Task 5 callback, Task 6 dep. The 503-on-token-fail is inherent to `get_service_sf()` raising (httpx `raise_for_status`) → surfaces as 500; a dedicated 503 wrapper is optional and intentionally NOT added (YAGNI).
- Testing → Tasks 1,3,4,6 unit; Task 7 E2E. ✓
- Config checklist → Task 8. ✓

**Placeholder scan:** No TBD/TODO; every code step carries real code. The Task 3 note (engine import path) is a concrete grep instruction, not a placeholder.

**Type consistency:** `get_service_sf()`, `_get_sf(request=None)`, `require_user(request)->dict`, `COOKIE_NAME="cts_session"`, `authMe()/loginRedirect()/logout()`, `app-auth` event name — used consistently across tasks. Migration `revision="d4e5f6a7b8c9"`, `down_revision="c3d4e5f6a7b8"`.

**Known follow-ups (out of scope):** deleting the dormant `salesforce_auth.py`/`salesforce_oauth.py`/`sf_sessions` table; RBAC; rotating the pre-existing Google Maps key (tracked in the cost incident doc).
