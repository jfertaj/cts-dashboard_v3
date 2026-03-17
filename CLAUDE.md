# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session startup

At the start of every session in this repository:

1. Read this file (`CLAUDE.md`) first.
2. Then read, in order: `docs/project-context.md`, `docs/current-state.md`, `docs/next-steps.md`.
3. Before proposing any changes, write 2–4 lines summarising the current state (what is done, what is in progress, what is next).
4. Do **not** re-analyse the full repository from scratch unless the user explicitly asks for it, or the documented state is clearly out of date relative to recent commits.
5. Use the documented state as the baseline for all work.
6. **Update docs proactively, without being asked**, when changes are significant: new features, bug fixes that affect behavior, architectural changes, or anything a future session would need to know to avoid re-discovering. Update `docs/current-state.md` and/or `docs/next-steps.md` **in the same response as the change**, not at the end of the session. Minor changes (typos, style tweaks, test adjustments) do not require doc updates.
7. **Mark next-steps items as done**: whenever a change implements an item listed in `docs/next-steps.md`, mark it as `~~done~~ — DONE (YYYY-MM-DD)` in that file **in the same response as the change**. Do not leave completed items unmarked.

## What this project is

CTS Dashboard v3 — an internal INNODIA web app for managing Clinical Trial Sites (CTS). It connects to a Salesforce org (`innodia-prod`) via OAuth, serves a React SPA, and includes Moby: an AI assistant backed by Claude (`claude-sonnet-4-6`) that queries Salesforce using natural language.

## Commands

### Local backend
```bash
# First time: generate backend/.env from AWS Secrets Manager
aws sso login --profile juan
python3 scripts/gen_local_env.py

# Start (4 workers required — Moby makes internal HTTP calls that deadlock on 1 worker)
bash scripts/run_local_backend.sh

# Kill existing process and restart after code changes (no hot-reload)
bash scripts/restart_local_backend.sh
```

### Local frontend
```bash
cd frontend
npm ci          # if node_modules missing or wrong platform (common on iCloud Drive)
npm run dev     # Vite dev server on :5173
npm run build   # production build
```

### Tests
```bash
# Backend unit tests (no network, 152 tests)
python -m pytest backend/tests/ -v
python -m pytest backend/tests/test_salesforce_explorer.py -v   # single file
python -m pytest backend/tests/ -k "test_pass_site" -v          # single test

# Integration tests (need SF session cookie)
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/test_combined_filter.py
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/test_multi_country.py
SF_SESSION_COOKIE="<cookie>" API_BASE="http://localhost:8000" python scripts/test_multi_country.py --moby
SF_SESSION_COOKIE="<cookie>" python scripts/test_user_stories.py

# Playwright E2E (needs frontend running)
cd frontend && npm run test:e2e                                   # deterministic suite
cd frontend && PLAYWRIGHT_SMOKE=1 SF_SESSION_COOKIE="..." npm run test:smoke
```

### Deployment (manual only — never auto-deploy)
```bash
# Must be on main branch
git checkout main && git merge dev
bash scripts/deploy.sh                  # backend + frontend
bash scripts/deploy.sh --backend-only
bash scripts/deploy.sh --migrate        # include Alembic migration
```

## Architecture

### Stack
- **Frontend**: React + TypeScript + Vite → `frontend/src/`
- **Backend**: FastAPI + Python → `backend/app/`
- **DB**: PostgreSQL (SQLAlchemy + Alembic). Tables: `sites`, `site_quals`, `sf_sessions`, `members` (geocoords)
- **Salesforce**: `simple_salesforce` via OAuth session stored in `sf_sessions` DB table (survives redeploys)
- **Deployment**: AWS ECS Fargate, ECR images, ALB at `https://cts-innodia-dashboard.org`

### Backend router map
| Router | Prefix | File |
|--------|--------|------|
| `salesforce_router` | `/api/salesforce/` | `salesforce_explorer.py` |
| `explorer_router` | `/api/explorer/` | `salesforce_explorer.py` |
| `members_explorer.router` | `/api/members/` | `members_explorer.py` |
| `ai_chat.router` | `/api/ai/` | `ai_chat.py` |
| `qualification_router` | `/api/qualification/` | `qualification.py` |

### The Explorer (`salesforce_explorer.py` — ~5000 lines)
The core of the app. The main endpoint `POST /api/explorer/search` at `explorer_search()`:
1. Receives a `FilterGroup` (`{logic: "AND"|"OR", rules: [...]}`) — supports nested sub-groups via `_flatten_filter_rules()`
2. Classifies rules into: `site_rules` (site.country, site.city), `sf_rules` (SOQL WHERE), `qual_rules` (JSONB), `account_rules`, `member_rules`, `extra_rules`
3. Queries Salesforce Opportunities (`RecordType.DeveloperName='SubAccount'`, `C_Type__c='Clinical'`)
4. Python-side filtering: `pass_site()`, `pass_qual()`, `pass_member()`, `passes_row_checks()`
5. Returns `{rows: [{account_id, account_name, country, city, data: {"sf.*": ..., "qual.*": ...}}], columns: [...]}`

**Key subtleties:**
- `qual.*` fields: JSONB stores base keys but frontend uses section-prefixed keys (e.g., `qual.2_2__field_name`). The `_qual_get()` helper does 3-fallback lookup (exact → dot-to-underscore → strip-section-prefix). Used in 8 places.
- `pass_site()` multi-country fix: multiple `site.country equals X` rules with AND logic are treated as OR (a site can't be in two countries).
- `_flatten_filter_rules()`: recursively flattens nested `FilterGroup` sub-groups before classification so nested OR groups work.
- Country values: stored as ISO-2 (`"ES"`) in the site DB, but Salesforce `ShippingCountry` stores full names (`"Spain"`). `_country_norm()` normalizes either way.

### Moby AI (`ai_chat.py` — ~7000 lines)
`POST /api/ai/chat` and `POST /api/ai/chat/stream` (SSE).

Flow: `chat_api()` → `_try_planner()` (deterministic handlers) → if no match → Claude (`claude-sonnet-4-6`) with tools.

**Deterministic planner handlers** (bypass Claude for speed, in order):
- Math (sum/avg on last table) — top of `_try_planner`
- Table context injection (compact system msg with last 5 rows)
- Conversational no-tools (short follow-ups)
- ND top/by-country, T1D followed, Stage 1/2 by-country, CT-3 Phase
- Country sites — multi-country: collects ALL `_COUNTRY_MAP` matches, builds `IN (...)` SOQL
- Nearest sites, km-of-assignment
- Study coordinators, activities

**Multi-country handler** (outside `_try_planner`, in `chat_api`): km-of-assignment uses `/api/explorer/search/nearby-multi`.

**Claude tools available**: `explorer_search`, `salesforce_query`, `nearest_filtered_sites`, `study_coordinators_with_activities`, `members_search`, and others in `TOOLS_SPEC`.

**Extended thinking**: `_is_complex_query()` detects multi-condition queries → `use_thinking=True` with `CLAUDE_THINKING_BUDGET=8000` tokens, `interleaved-thinking-2025-05-14` beta.

**Streaming**: `_claude_chat()` uses `aclient.messages.stream()` when `_STREAM_Q.q` is set (threading.local). Frontend reads SSE via `askAIStream()`.

### Members view (`members_explorer.py`)
3 endpoints, 5-min in-memory cache. RecordTypes: `RT_Member` (institution) / `SubAccount` (clinical unit linked via `C_Member__c`). Frontend: `MembersView.tsx` + `MemberDetailsModal.tsx`.

### Frontend views
- `ExplorerView.tsx` — main data explorer. Filter state (`FilterGroup`) drives `POST /api/explorer/search`. `FilterBuilder.tsx` renders the nested AND/OR UI. Country column: `accessorFn` returns ISO2 (sorting), `cell` renders full name via `displayCountry()`.
- `ChatView.tsx` — Moby chat with SSE streaming, `▋` live cursor.
- `MembersView.tsx` — member institutions table.
- `MapView.tsx` + `MemberMapView.tsx` — Leaflet maps.
- `ChartModal.tsx` — Recharts stacked/grouped bar charts.

### SF session flow
OAuth login → `sf_sessions` PostgreSQL table (UUID stored, not in-memory). Cookie format: `<uuid>.<itsdangerous-signature>`. `unsign_value()` strips the signature to get the UUID for DB lookup. Sessions expire in ~15-20 minutes during active use.

### Field namespacing convention
| Prefix | Source | Example |
|--------|--------|---------|
| `sf.*` | Salesforce Opportunity field | `sf.C_Number_of_Stage1_Individuals_followed__c` |
| `qual.*` | JSONB qualification upload | `qual.2_2__personal_conversation_with_physician` |
| `site.*` | Local DB site table | `site.country`, `site.city` |
| `extra.*` | Batch-fetched SF data | `extra.AssignmentsNames` |
| `account_name`, `country`, `city` | Top-level row fields | always present |

### Alembic migrations
```bash
# Always check current head before creating a new migration
alembic heads
# Set down_revision to current tip in the new migration file
alembic revision --autogenerate -m "description"
# In SOQL use CAST(:x AS jsonb) not :x::jsonb (psycopg3 conflict)
```

### Docker build (Apple Silicon)
Always use `--platform linux/amd64`:
```bash
docker buildx build --platform linux/amd64 -f backend/Dockerfile.backend ./backend
```

## Config files
- `backend/app/config/sf_schema_full.json` — full SF org schema dump (Account, Opportunity, Contact, Assignment__c)
- `backend/app/config/fields_opportunity_curated.json` — curated fields for Moby's knowledge index
- `backend/app/config/qualification_aliases.json` — NLP aliases for qual fields
- `backend/.env` — gitignored, generated by `scripts/gen_local_env.py`
