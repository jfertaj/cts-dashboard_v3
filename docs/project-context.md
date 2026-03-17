# CTS Dashboard v3 — Project Context

## Objective

CTS Dashboard is an internal web application for INNODIA (a European clinical research network for Type 1 Diabetes). It centralises data from two sources — a Salesforce org and a local PostgreSQL database — and exposes it through four views: a qualification upload tool, an interactive data explorer with map, a Members institution directory, and an AI chat assistant named Moby.

Concrete things the app does:
- Lets INNODIA staff explore ~136 active clinical sites (CTS) by filtering on Salesforce Opportunity fields, local qualification checklist answers (JSONB), geographic location, assignments, and extra batch data (PI names, activity counts).
- Displays those sites on a Google Maps-based map with colour-coded pins (INNODIA clinical, referral, DETECT).
- Lets staff upload Excel qualification checklists, geocodes the site from geonames_cities, and stores answers in a flattened JSONB column (`site_qual.data`).
- Shows a Members directory (INNODIA Member institutions with proposed/validated roles, sub-accounts, contacts).
- Provides Moby: an AI assistant backed by Claude Sonnet 4.6. Moby understands INNODIA domain language and calls internal tools (explorer_search, nearest_filtered_sites, salesforce_query, sql_query, etc.) to answer natural-language questions about the network. It returns tables and charts that link back to the Explorer.

---

## Architecture

```
Browser (React + TypeScript + Vite)
    ↓ /api/* (cookie-based, httpOnly sf_session)
FastAPI backend (Python 3.x)
    ├── PostgreSQL (local DB — SQLAlchemy / Alembic)
    │     Tables: sites, site_qual, questionnaires, sections, questions,
    │             responses, profiling_kv, geonames_cities, sf_sessions
    ├── Salesforce REST API (simple_salesforce)
    │     Objects: Opportunity, Account (SubAccount + RT_Member), Contact,
    │              Assignment__c, AccountContactRelation
    ├── Anthropic API (claude-sonnet-4-6)
    └── Google Maps / Geocoding API
```

The frontend and backend are deployed as separate Docker images on AWS ECS (cluster `cts-dashboard`). There is no CI/CD; deployments are triggered manually via `scripts/deploy_build_push_and_migrate.sh`. The production domain is `https://cts-innodia-dashboard.org/`, routing through an ALB.

---

## Main Modules

### Backend (`backend/app/`)

| File | Responsibility |
|------|---------------|
| `main.py` | FastAPI app assembly: routers, CORS, startup tasks, catch-all SPA route |
| `startup.py` | DB init (`create_all`), geonames loader, placeholder subaccount sync |
| `routers/salesforce_auth.py` | OAuth2 Authorization Code flow with Salesforce; stores/retrieves sessions from `sf_sessions` PostgreSQL table via `itsdangerous`-signed cookies |
| `routers/salesforce_explorer.py` | The largest file (~4700+ lines). Two APIRouters: `salesforce_router` (`/api/salesforce/...`) and `explorer_router` (`/api/explorer/...`). Handles field catalog, map bootstrap, the main `explorer_search` endpoint, `within-drive-km`, `nearby-multi`, and all Python-side filtering logic |
| `routers/members_explorer.py` | `GET /api/members/bootstrap`, `POST /api/members/search`, `GET /api/members/{id}/detail`. 5-min in-memory cache. Fetches RT_Member accounts + SubAccount counts + contact counts from Salesforce |
| `routers/qualification.py` | `POST /api/qualification/upload`: parses Excel checklists, geocodes, stores in Questionnaire/Section/Question/Response hierarchy and flattened `site_qual.data` JSONB. Also list, link/unlink, delete endpoints |
| `routers/ai_chat.py` | Moby AI: `POST /api/ai/chat` and `POST /api/ai/chat/stream` (SSE). Deterministic planner (`_try_planner`) handles 15+ query patterns before falling back to Claude. Calls internal HTTP endpoints via httpx |
| `routers/explorer_bridge.py` | Thin endpoints `/api/explorer-bridge/highlight` and `/api/explorer-bridge/add-columns` — used by Moby to trigger frontend state changes via CustomEvent |
| `routers/geo.py` | Admin endpoints for re-geocoding sites in local DB |
| `routers/salesforce_sync.py` | `POST /api/salesforce/sync/profiling` — imports Profiling Opportunities to local sites table (used for badge display) |
| `services/salesforce_oauth.py` | OAuth helpers: token exchange, `sf_sessions` table CRUD, cookie signing/verification via `itsdangerous.Signer` |
| `models/site.py` | `Site` + `ProfilingKV` SQLAlchemy models |
| `models/site_qual.py` | `SiteQual` — single JSONB column `data` per site (one-to-one with Site) |
| `models/questionnaire.py` | `Questionnaire`, `Section`, `Question`, `Response` — hierarchical storage of raw Excel data |
| `models/geonames.py` | `GeonameCity` — loaded from GeoNames cities500.txt for city/country geocoding during upload |
| `config/fields_opportunity_curated.json` | Curated list of Salesforce Opportunity + Account fields with types, labels, groups. Loaded at startup by salesforce_explorer.py; drives field catalog returned by `GET /api/explorer/fields` |

### Frontend (`frontend/src/`)

| File | Responsibility |
|------|---------------|
| `App.tsx` | Root component. Tab routing (`upload / explorer / members / chat`), SF auth state, session-expired overlay, prefetch of `/api/members/bootstrap` and `/api/salesforce/map/bootstrap` on auth |
| `pages/ExplorerView.tsx` | Main explorer page (~3000 lines). FilterBuilder, TanStack table with sorting/pagination, MapView, ColumnPicker, ChartModal, nearby/within-km drawers, row checkboxes with "Select all" banner, non-empty column detection |
| `pages/ChatView.tsx` | Moby chat UI. Sends messages to `/api/ai/chat/stream` (SSE), renders live streaming bubble, renders AIResultTable and charts from assistant responses, stores conversation in localStorage |
| `pages/MembersView.tsx` | Members directory. Fetches from `/api/members/bootstrap` (or uses prefetched rows), filters locally by name tags, country, level, proposed/validated roles. Shows MemberMapView and MemberDetailsModal |
| `pages/UploadLinkView.tsx` | Upload & Link tab — uploads Excel, links/unlinks sites to SF Accounts |
| `components/FilterBuilder.tsx` | Nested AND/OR filter tree UI. Supports string, number, date, boolean operators |
| `components/MapView.tsx` | Google Maps integration. SVG pin icons, colour-coded by clinical/referral/detect flags, InfoWindow popups, highlighted rows |
| `components/MemberMapView.tsx` | Leaflet-based map for Members view with teardrop INNODIA crystal pins |
| `components/Header.tsx` | Sticky header, tab navigation, login/logout button (calls `/api/salesforce/me`) |
| `components/AIResultTable.tsx` | Renders tables returned by Moby with export CSV, "Open in Explorer", "Add columns", highlight buttons |
| `components/SiteDetailsModal.tsx` | Modal showing site qualification data and extras |
| `components/MemberDetailsModal.tsx` | Modal showing Member contacts, sub-accounts, and sub-account contacts |
| `lib/api.ts` | Single `api()` fetch wrapper with timeout, retry, in-flight deduplication. All API call functions: `explorerSearch`, `explorerBootstrap`, `salesforceMe`, `getExplorerFields`, etc. |
| `lib/countryUtils.ts` | `ISO2_COUNTRY` map + `displayCountry()` helper |
| `lib/ai.ts` | `askAI()` and `askAIStream()` — POST to `/api/ai/chat` or reads SSE stream |
| `types.ts` | `Tab` union type and `ResponseRecord` interface |

---

## Main Flows

### 1. Salesforce Login
1. User clicks "Login Salesforce" → `sfLoginRedirect()` → `GET /api/salesforce/oauth/login`
2. Backend builds Salesforce Authorization URL, redirects browser
3. SF redirects back to `GET /api/salesforce/oauth/callback?code=...&state=...`
4. Backend exchanges code for tokens, creates row in `sf_sessions` PostgreSQL table, sets `sf_session` cookie (signed with `itsdangerous`)
5. Frontend redirected back to app; `GET /api/salesforce/me` confirms auth

### 2. Explorer Search (end-to-end)
1. User sets filters in FilterBuilder, clicks "Search"
2. `POST /api/explorer/search` with `{filters: FilterGroup, columns: [...]}` body
3. Backend classifies rules into `site_rules` / `sf_rules` / `qual_rules` / `account_rules` / `member_rules` / `extra_rules`
4. SF query: `SELECT ... FROM Opportunity WHERE Type IN (...)` — fetches CTS accounts
5. If `need_batch_extras`: calls `batch_fetch_account_extras()` for PI name, assignments, activities
6. If `qual_rules`: joins with `site_qual` from PostgreSQL via `salesforce_account_id`
7. Python-side `passes_row_checks()` applies all filter categories
8. Returns `{rows: [{account_id, account_name, country, city, data: {sf.*, qual.*, extra.*}}], points: [{lat, lng, ...}]}`
9. Frontend renders TanStack table + Google Map markers

### 3. Qualification Upload
1. User selects Excel file in Upload & Link tab
2. `POST /api/qualification/upload` — `parse_qualification_checklist()` extracts section/question/answer rows
3. Site inferred from content (name, city/country from "City and Country" question)
4. City/country geocoded via `geonames_cities` table first, then Google Maps fallback
5. Questionnaire/Section/Question/Response hierarchy stored in PostgreSQL
6. Flat JSONB `site_qual.data` built: keys are `{section_subcode}__{slug}` (e.g. `3_6__is_your_pharmacy_on_site_or_off_campus`)
7. Site linked to Salesforce Account via `POST /api/qualification/link`

### 4. Moby Chat
1. User types in ChatView; `askAIStream()` sends `POST /api/ai/chat/stream` with `{messages, last_table, last_filters}`
2. Backend `_try_planner()` checks 15+ deterministic regex patterns in order; if matched, returns immediately
3. If not matched: builds Claude messages, calls `anthropic.messages.stream()` with tool definitions
4. Claude may call tools (`explorer_search`, `salesforce_query`, `sql_query`, `nearest_filtered_sites`, etc.)
5. Backend dispatches tool calls internally (httpx for explorer_search, direct SF for salesforce_query, SQLAlchemy for sql_query)
6. Claude returns JSON `{answer, table, visualization, last_filters}`
7. SSE stream sends tokens; frontend renders live bubble then final structured response with table and optional chart

### 5. Members View
1. `App.tsx` pre-fetches `/api/members/bootstrap` after auth confirmation
2. `MembersView` receives prefetched rows (or fetches if none)
3. Filter changes call `POST /api/members/search` with FilterGroup
4. Backend applies Python-side filtering on 5-min cached data
5. Row click → `GET /api/members/{id}/detail` → modal with contacts, sub-accounts

---

## Data Sources

### Salesforce org
- **Opportunity** — one per clinical site relationship (Type = "Profiling", "CTS", etc.). Contains patient metrics (Stage1/2, T1D counts, ND, HLA typing), dates, assignments reference
- **Account (SubAccount)** — clinical sites. `RecordType.DeveloperName = 'SubAccount'`, `C_Type__c = 'Clinical'`. Has geography (ShippingCity, ShippingCountry, lat/lng), membership in RT_Member via `C_Member__c`
- **Account (RT_Member)** — INNODIA member institutions. Has proposed/validated role boolean fields
- **Contact** — staff linked to accounts. Role data from `AccountContactRelation` (role = "PI", "Study Coordinator", etc.)
- **Assignment__c** — custom object linking sites to activities (Opportunities with RT_Activity). Fields: `C_Assignment_Stage__c`, `Assignment_Type__c`, `C_MCA_Status__c`, `C_Payment_Done__c`

### Local PostgreSQL
- **sites** — local registry of sites with address, geocoords, `salesforce_account_id` FK link
- **site_qual** — JSONB `data` column with flattened qualification checklist answers per site
- **questionnaires / sections / questions / responses** — raw hierarchical storage of uploaded Excel data
- **geonames_cities** — city geocoordinate lookup (loaded from GeoNames cities500.txt)
- **sf_sessions** — Salesforce OAuth tokens, keyed by session UUID; replaces previous in-memory dict for cross-ECS-instance persistence
- **profiling_kv** — key-value store for profiling badge data (present in model, used for badge display in map bootstrap)

---

## Commands

### Local Development
```bash
# 1. Fetch secrets from AWS Secrets Manager → backend/.env
aws sso login --profile juan
python3 scripts/gen_local_env.py

# 2. Start backend (4 workers required; no --reload)
bash scripts/run_local_backend.sh

# 3. Start frontend (separate terminal)
cd frontend && npm run dev        # dev server on :8080, proxies /api → :8000
```

### Testing
```bash
# Backend unit tests (no network, no DB)
python -m pytest backend/tests/ -v

# Frontend E2E (deterministic, needs dev or preview server running)
cd frontend && npm run test:e2e

# Integration tests (need SF_SESSION_COOKIE env var)
SF_SESSION_COOKIE="<value>" python scripts/test_multi_country.py
SF_SESSION_COOKIE="<value>" python scripts/test_country_filter.py
SF_SESSION_COOKIE="<value>" python scripts/test_user_stories.py

# Smoke tests against ECS
cd frontend && PLAYWRIGHT_SMOKE=1 SF_SESSION_COOKIE="..." BASE_URL="https://alb-cts-dashboard-169921453.eu-west-1.elb.amazonaws.com" npx playwright test --grep @smoke
```

### Deploy
```bash
# Manual deploy — builds Docker images (linux/amd64), pushes to ECR, updates ECS
bash scripts/deploy_build_push_and_migrate.sh
```

---

## Key Dependencies

### Python (backend/requirements.txt)
| Package | Role |
|---------|------|
| `fastapi` / `uvicorn[standard]` | Web framework and ASGI server |
| `sqlalchemy>=2.0` / `alembic` | ORM + migrations |
| `psycopg[binary]` | PostgreSQL driver (psycopg3) |
| `simple-salesforce` | Salesforce REST API client |
| `anthropic>=0.40.0` | Claude API (Moby AI) — primary LLM |
| `openai>=1.35.0` | OpenAI SDK (still imported in ai_chat.py; client instantiated but Moby now uses Claude) |
| `httpx` | Async HTTP client (internal calls from Moby to /api/explorer/search) |
| `itsdangerous` | Cookie signing for session tokens |
| `pycountry` | Country name utilities |
| `openpyxl` / `pandas` | Excel parsing for qualification upload |

### npm (frontend/package.json)
| Package | Role |
|---------|------|
| `@tanstack/react-table` | Headless table with sorting, filtering, pagination |
| `@react-google-maps/api` | Google Maps component for ExplorerView MapView |
| `react-leaflet` / `leaflet` | Leaflet map for MembersView MemberMapView |
| `recharts` | Charts in ChartModal and Moby visualization responses |
| `swr` | Stale-while-revalidate data fetching (explorer bootstrap cache) |
| `tailwindcss` | Utility CSS framework |
| `@playwright/test` | E2E test runner |
| `antd` | Ant Design (used for some UI elements) |
