# Design — Entra ID SSO gate + Salesforce service-account access

**Date:** 2026-07-15
**Status:** Approved design → ready for implementation plan
**Author:** Claude (brainstorming session with Juan)

## Problem

Today the only way into CTS Dashboard is a **per-user Salesforce OAuth session**. Login *is* the
Salesforce connection: every data endpoint (`_get_sf(request)` in `salesforce_explorer.py`, plus the
equivalents in `ai_chat.py`, `members_explorer.py`, `salesforce_extras*.py`) builds a `Salesforce()`
client from **that user's** access token stored in the `sf_sessions` table.

Juan wants two things:

1. A **Microsoft Entra ID (innodia.org) SSO login** as the gate to the app.
2. Entering the app **must no longer require a valid Salesforce user session**.

Because data endpoints still need Salesforce credentials, requirement (2) forces a second, coupled
change: **decouple Salesforce access from the user session** by moving it to a single service
(integration) account.

## Decisions (locked in brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| Identity provider | innodia.org SSO backend | **Microsoft Entra ID (Microsoft 365 tenant)** |
| Salesforce data source after SSO | **Single service account** via OAuth 2.0 **Client Credentials Flow** (External Client App, Run As integration user) |
| Where the gate lives | **App-level OIDC in FastAPI** (not ALB, not Cognito) |
| OIDC library | **Authlib** (`authlib.integrations.starlette_client`) — validates `id_token` via JWKS + `iss`/`aud`/`exp`/`nonce` |
| Per-user Salesforce OAuth | **Retire it** — SF reads *and* writes go through the service account; SF login UI removed; `salesforce_auth.py` / `salesforce_oauth.py` / `sf_sessions` retired |
| Who may sign in | **Any account in the innodia.org tenant** (single-tenant Entra app registration) |
| Local dev / E2E | Keep an **`AUTH_DISABLED=1`** bypass (fake dev user); OFF in prod |

## Non-goals

- No per-user authorization/roles inside the app (any innodia.org user gets the same access). RBAC is a
  future concern.
- No ALB / Cognito / infrastructure-as-code changes. The ALB config is untouched.
- No change to Moby's Salesforce query logic beyond swapping how the `Salesforce()` client is obtained.

---

## Architecture

### A. Entra ID app registration (manual, documented — not code)

Single-tenant confidential web app in the innodia.org Entra tenant:

- **Redirect URIs:** `https://cts-innodia-dashboard.org/api/auth/callback` and
  `http://localhost:8000/api/auth/callback` (dev).
- **Scopes:** `openid profile email`.
- **Client secret** → AWS Secrets Manager (same secret bundle the backend already reads).
- **Single-tenant** issuer, so only innodia.org accounts can complete sign-in. (No Enterprise-App
  assignment restriction — any tenant user is allowed, per decision.)

New config keys (Secrets Manager + `backend/.env` via `gen_local_env.py`):
`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `APP_SESSION_SECRET` (itsdangerous signing key),
`AUTH_DISABLED` (dev only).

### B. Backend — Entra OIDC gate (mirrors the existing `salesforce_auth` pattern)

New units, each with one clear purpose:

- **`backend/app/services/entra_oauth.py`** — registers an Authlib OAuth client against Entra's discovery
  document (`https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration`). Owns:
  build-authorize-url (with state + nonce), handle-callback (code→tokens, **Authlib validates the
  `id_token`**), extract identity (`email`/`preferred_username`, `name`, `oid`).
- **`backend/app/services/app_session.py`** — app session store, a near-copy of `sf_sessions` plumbing:
  a new `app_sessions` table (`session_id UUID PK, email, name, oid, issued_at, expires_at`), plus
  `create_session`, `get_session`, `destroy_session`, and `sign_value`/`unsign_value` (itsdangerous).
  Cookie `cts_session` (httpOnly, SameSite=Lax, Secure in prod). Survives ECS redeploys like `sf_sessions`.
- **`backend/app/routers/entra_auth.py`** — routes under `/api/auth`:
  - `GET /api/auth/login?next=/` → redirect to Entra.
  - `GET /api/auth/callback` → validate state/nonce, exchange code, create app session, set cookie,
    redirect to `{FRONTEND_BASE}{next}`. On failure redirect to `{FRONTEND_BASE}/?auth_error=<reason>`.
  - `GET /api/auth/me` → `{ authenticated: bool, email?, name? }`.
  - `GET /api/auth/logout` → destroy session, clear cookie, redirect to `/` (optionally Entra logout URL).
- **`backend/app/deps/auth.py`** — FastAPI dependency **`require_user`**:
  - `AUTH_DISABLED=1` → returns a fixed dev user (no cookie needed).
  - Else: read `cts_session` cookie → unsign → look up `app_sessions` → 401 JSON if missing/expired,
    else return the user.
  - Applied to every data router: `explorer_router`, `salesforce_router`, `members_explorer.router`,
    `ai_chat.router`, `qualification_router`, `assignments_report.router`, `salesforce_extras.router`,
    `explorer_bridge.router`. Health and `/api/auth/*` stay public.

Authlib needs Starlette `SessionMiddleware` for the OAuth state/nonce round-trip — added in `main.py`,
keyed by `APP_SESSION_SECRET`.

### C. Backend — Salesforce as a service account (Client Credentials Flow)

- **`backend/app/services/salesforce_service.py`** — obtains a token via
  `POST {SF_MY_DOMAIN}/services/oauth2/token` with `grant_type=client_credentials`,
  `client_id=SF_CLIENT_ID`, `client_secret=SF_CLIENT_SECRET` (credentials in the POST body). This flow
  returns `access_token` + `instance_url` and **no refresh token**, so the module caches the token
  in-process with an expiry and **re-requests on expiry or on a 401** from Salesforce. Thread-safe
  singleton (`threading.Lock`) since the backend runs 4 uvicorn workers — each worker holds its own cache,
  which is fine (stateless token mint). Exposes **`get_service_sf() -> Salesforce`**.
- **Replace** all `_get_sf(request)` call sites (6 in `salesforce_explorer.py`, plus the SF-client
  construction in `ai_chat.py`, `members_explorer.py`, `salesforce_extras.py`, `salesforce_extras_batch.py`)
  with `get_service_sf()`. The per-request SF cookie plumbing is deleted.
- New config keys: `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_MY_DOMAIN`
  (e.g. `https://innodiaivzw.my.salesforce.com`).
- **Salesforce side (manual, documented):** create/enable an **External Client App** with Client
  Credentials Flow, **Run As** the integration user `juan.f.tajes@innodia.org` (per global notes). The
  integration user must have **read + write** on the objects the app touches (Opportunity, Account,
  Contact, Assignment__c) because link/unlink and qualification writes now run as this user.

### D. Retire per-user Salesforce OAuth

- Remove the SF login/connect UI from the frontend (`UploadLinkView` SF-connect affordance, Header SF
  login button, the "not connected to Salesforce" banner and the SF "Session Expired" overlay copy).
- Stop mounting `salesforce_auth_router`. Keep `salesforce_auth.py` / `salesforce_oauth.py` and the
  `sf_sessions` table in the tree but unused (dormant), to keep the diff reviewable; a follow-up can
  delete them. **Verification gate before this step:** confirm no write path needs the *end-user's*
  Salesforce identity (link/unlink, qualification link). Expectation: all writes are attributable to the
  Run-As integration user and that is acceptable.

### E. Frontend

- **`frontend/src/hooks/useAuth.ts`** (replaces `useSalesforceAuth.ts`) — polls `/api/auth/me`; exposes
  `{ authed, sessionExpired, setSessionExpired, user }`.
- **`App.tsx`** — when `authed === false`, render a full **sign-in gate** (no data): "Sign in with
  innodia.org" → `window.location = /api/auth/login?next=<current path>`. Reuse the existing overlay
  component; change copy from Salesforce to innodia.org.
- **`frontend/src/lib/api.ts`** — the central `api<T>()` wrapper (line ~39) intercepts **HTTP 401**:
  dispatch the existing `sf-auth` (renamed `app-auth`) event → sets `sessionExpired` → overlay →
  re-login. This gives one global 401 handler instead of per-call handling.
- **`Header.tsx`** — login/logout point to `/api/auth/login` and `/api/auth/logout`; show the signed-in
  user's email.
- Remove `salesforceMe` / `salesforceLogout` / SF login helpers from `lib/salesforce.ts` + `api.ts`
  (or leave thin dormant stubs consistent with decision D).

### F. Auth + data flow

```
Browser → cts-innodia-dashboard.org
  SPA loads → GET /api/auth/me → { authenticated:false }
  → SPA shows "Sign in with innodia.org" gate (no data)
  → click → GET /api/auth/login → 302 Entra → user signs in (innodia.org)
  → 302 GET /api/auth/callback → Authlib validates id_token
      → create app_sessions row → Set-Cookie cts_session → 302 to frontend
  → GET /api/auth/me → { authenticated:true, email }
  → SPA renders; every data call carries cts_session
      → require_user passes → endpoint calls get_service_sf()
      → service token (client_credentials) → Salesforce query
  Session expiry → any data call → 401 → SPA overlay → re-login
```

### G. Error handling

- Callback failures (bad state/nonce, token-exchange error, non-innodia.org account) →
  `302 {FRONTEND}/?auth_error=<reason>`; SPA shows a dismissible banner.
- `get_service_sf()` token-mint failure → endpoints surface **503** with a clear message; detailed
  context logged server-side (never leak the secret).
- `require_user` missing/expired → **401 JSON** for API calls (SPA catches globally).
- Never log `ENTRA_CLIENT_SECRET` / `SF_CLIENT_SECRET` / tokens.

---

## Testing

**Backend unit (pytest, no network):**
- `app_session`: sign/unsign round-trip; tamper → rejected; create/get/destroy; expiry handling.
- `require_user`: valid / missing-cookie / expired / `AUTH_DISABLED` bypass (4 states).
- `entra_oauth`: state+nonce generated and enforced; identity extraction from a fixture claims dict.
- `salesforce_service`: token cache hit; re-mint on expiry; re-mint on simulated 401; POST body shape
  (`grant_type=client_credentials`, id+secret) — `httpx` mocked; secret never in logs.

**E2E (Playwright):**
- Mock `/api/auth/me` → authed, exactly as the current suite mocks `/api/salesforce/me`; existing specs
  keep passing (repoint the mock).
- New spec: unauthenticated → sign-in gate visible, no data table.
- Deterministic suites run with `AUTH_DISABLED=1`.

**Coverage target:** 80%+ on the new auth modules (per repo testing rule).

---

## Config / infra checklist (manual, documented in `docs/`)

- [ ] Entra: register single-tenant app, redirect URIs, `openid profile email`, client secret.
- [ ] Salesforce: External Client App → Client Credentials Flow, Run As integration user with read+write.
- [ ] AWS Secrets Manager: add `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`,
      `APP_SESSION_SECRET`, `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_MY_DOMAIN`.
- [ ] `scripts/gen_local_env.py`: pull the new keys into `backend/.env`.
- [ ] Alembic: `app_sessions` migration (set `down_revision` to current head).
- [ ] No ALB change.

## Rollout / risk

- **Cutover is hard** (login mechanism changes for everyone). Deploy backend + frontend together.
- **Biggest risk:** the Salesforce External Client App / integration-user permissions. Validate
  `get_service_sf()` against real Salesforce (SSM tunnel, per repo practice) *before* wiring the gate,
  so the two changes are de-risked independently.
- Rollback = redeploy previous task definitions (per-user SF OAuth still present but dormant in the tree).
