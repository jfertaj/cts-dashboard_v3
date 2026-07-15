# Entra ID SSO + Salesforce service-account setup runbook

Manual, one-time setup for the auth model introduced in `feat/entra-sso-service-account`:
an Entra ID (Azure AD) OIDC login gate in front of the whole app, and a Salesforce
service account (OAuth 2.0 Client Credentials Flow) that serves all Salesforce data —
no more per-user Salesforce OAuth. Domain is **innodia.org** (never `.eu`).

This is manual admin work in the Entra and Salesforce consoles — there is no CLI/script
for either registration step.

---

## (a) Entra ID — app registration

1. Azure Portal → **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: `CTS Dashboard` (or similar). **Supported account types: single tenant**
   (this app gates INNODIA staff only — do not select multi-tenant).
3. **Redirect URIs** — platform **Web**, add both:
   - `https://cts-innodia-dashboard.org/api/auth/callback` (prod)
   - `http://localhost:8000/api/auth/callback` (local dev)

   The backend computes this path itself (`REDIRECT_PATH = "/api/auth/callback"` in
   `backend/app/services/entra_oauth.py`) — it must match exactly, scheme included.
4. **API permissions** → Microsoft Graph → Delegated permissions → add `openid`,
   `profile`, `email` (these are typically pre-granted by default for a new app; verify
   they're listed). The backend requests `client_kwargs={"scope": "openid profile email"}`.
5. **Certificates & secrets** → **New client secret** → copy the **value** immediately
   (it is not retrievable later). This is `ENTRA_CLIENT_SECRET`.
6. From the app's **Overview** page, copy:
   - **Application (client) ID** → `ENTRA_CLIENT_ID`
   - **Directory (tenant) ID** → `ENTRA_TENANT_ID`

---

## (b) Salesforce — External Client App (service account)

Per the org-wide rule (see global CLAUDE.md): Juan's INNODIA org (`innodiaivzw.my.salesforce.com`)
only offers **External Client Apps**, not classic Connected Apps.

1. **Create:** Setup → **App Manager** → **New External Client App**. Name it something
   like `CTS Dashboard Service Account`.
2. **Configure (separate page):** Setup → Quick Find → **"External Client Apps Manager"**
   → find the app → actions dropdown:
   - **Edit Settings**:
     - **Enable OAuth**.
     - Scopes: `api` (minimum needed for SOQL/data access).
     - **Flow Enablement → Enable Client Credentials Flow**.
     - The **Consumer Key** and **Consumer Secret** live on this same page — copy both:
       - Consumer Key → `SF_CLIENT_ID`
       - Consumer Secret → `SF_CLIENT_SECRET`
   - **Edit Policies**:
     - **OAuth Policies → Client Credentials Flow → Run As** = the integration user
       `juan.f.tajes@innodia.org`.
3. **Integration user permissions**: `juan.f.tajes@innodia.org` (the Run As user) needs
   **read + write** access on:
   - `Opportunity`
   - `Account`
   - `Contact`
   - `Assignment__c`

   Grant via profile or permission set — whatever this user already has for other
   INNODIA integrations; verify it covers all four objects with write, not just read
   (the app both queries and updates these objects).
4. **Token endpoint**: confirm it is the org's **My Domain** URL, not `login.salesforce.com`:
   ```
   {SF_MY_DOMAIN}/services/oauth2/token
   ```
   e.g. `https://innodiaivzw.my.salesforce.com/services/oauth2/token`. The backend builds
   this itself from `SF_MY_DOMAIN` (`backend/app/services/salesforce_service.py::_cfg()`)
   — just make sure the secret value has no trailing slash and no path suffix.

   `SF_MY_DOMAIN` = `https://innodiaivzw.my.salesforce.com`

---

## (c) AWS Secrets Manager — the 7 new keys

Add these keys to the backend Secrets Manager bundle(s) already read by
`scripts/gen_local_env.py` (`prod/cts-dashboard/backend` and
`prod/cts-dashboard/salesforce`, or whichever of the two logically fits each key —
`gen_local_env.py` merges both bundles into one `backend/.env`, so exact placement
between the two doesn't matter for local dev, but check `deploy.sh`/task-def env
wiring for which secret ARN backs the ECS task definition):

| Key | Value | Source |
|---|---|---|
| `ENTRA_TENANT_ID` | Directory (tenant) ID | step (a).6 |
| `ENTRA_CLIENT_ID` | Application (client) ID | step (a).6 |
| `ENTRA_CLIENT_SECRET` | client secret value | step (a).5 |
| `APP_SESSION_SECRET` | a fresh random secret (e.g. `openssl rand -hex 32`) — signs the app's own session cookie, unrelated to Entra/SF | new, generate it |
| `SF_CLIENT_ID` | Consumer Key | step (b).2 |
| `SF_CLIENT_SECRET` | Consumer Secret | step (b).2 |
| `SF_MY_DOMAIN` | `https://innodiaivzw.my.salesforce.com` | INNODIA org My Domain |

`APP_SESSION_SECRET` is required at backend startup — `main.py` does
`secret_key=os.environ["APP_SESSION_SECRET"]` with no default, so a missing key means
the backend fails to boot in prod. Locally, `scripts/gen_local_env.py` also writes
`AUTH_DISABLED=1` as a default (overridable by the secrets bundle) so local dev and
tests bypass the Entra gate entirely without needing real Entra credentials.

After adding the keys, re-run (or have Juan re-run) `python3 scripts/gen_local_env.py`
locally to pick them up into `backend/.env`.

---

## (d) Migrate

```bash
bash scripts/deploy.sh --migrate
```

Runs `alembic upgrade head`, which creates the `app_sessions` table (the new app-level
session store backing the Entra login gate — separate from the old `sf_sessions` table,
which is now dormant).

---

## (e) Cutover

1. Deploy **backend + frontend together** — the frontend's sign-in gate
   (`data-testid="signin-innodia"`) and `useAuth`/`authMe()` depend on the new
   `/api/auth/*` routes existing on the backend; deploying only one side leaves the
   other broken.
   ```bash
   git checkout main && git merge dev
   bash scripts/deploy.sh --migrate
   ```
2. Verify the login flow end-to-end:
   - Visit `https://cts-innodia-dashboard.org/` while signed out → should show the
     innodia.org sign-in gate.
   - Click sign-in (`/api/auth/login`) → redirected to Entra login → after auth,
     redirected back to `/api/auth/callback` → lands on the dashboard.
   - `GET /api/auth/me` should return `{"authenticated": true, ...}`.
3. Verify a data call actually returns rows (proves the Salesforce service account is
   working end-to-end, not just the Entra gate): open the Explorer view and run any
   search, or ask Moby a simple question — both should return real Salesforce data
   without prompting for a separate Salesforce login (the old per-user SF OAuth screen
   should no longer appear anywhere).

If step 2 or 3 fails, check (in order): `APP_SESSION_SECRET` present in the running
task's environment (backend fails to boot without it, so this usually shows as a
crash-looping ECS task, not a login error) → `ENTRA_*` values correct and redirect URI
matches exactly (scheme + host + path) → `SF_MY_DOMAIN`/`SF_CLIENT_ID`/`SF_CLIENT_SECRET`
correct and the Client Credentials Flow policy's Run As user has the object permissions
from step (b).3.
