# CTS Dashboard v3 — Current State

Legend:
- **CONFIRMED** — code + tests exist and the connection between frontend and backend is observable
- **INFERRED** — code exists and appears wired up, but no dedicated automated test covers the integration end-to-end

---

## What Is Complete

### Salesforce Authentication — CONFIRMED
- OAuth2 Authorization Code Flow: `salesforce_auth.py` + `services/salesforce_oauth.py`
- Sessions stored in `sf_sessions` PostgreSQL table (persists across ECS restarts)
- `Header.tsx` polls `/api/salesforce/me` and shows login/logout button
- `useSalesforceAuth` hook drives app-level auth state; idle-timer triggers session-expired overlay
- Covered by E2E: `dashboard.spec.ts` mocks `/api/salesforce/me`

### Explorer View (Filter + Table + Map) — CONFIRMED
- `POST /api/explorer/search` returns rows + map points
- **Salesforce-style FilterBuilder** — CONFIRMED (2026-03-16): complete rewrite of `FilterBuilder.tsx`; filters are now numbered (①, ②, ③…); "Include rows matching" bar shows editable logic expression (`1 AND (2 OR 3)`); modal with real-time validation; expression parser in `frontend/src/lib/filterLogic.ts` (tokenizer + recursive descent, AND > OR precedence, AST manipulation for rule removal/addition)
- **Logic expression evaluator backend** — CONFIRMED (2026-03-16): `_is_logic_expr()` + `_eval_logic_expr_be()` added to `salesforce_explorer.py`; `pass_site`, `pass_qual`, `pass_member`, `pass_extra` use expression evaluation when `filters.logic` is an expression string; SOQL falls back to OR for safety (Python corrects); `FilterQuery.logic` widened to `str`; 27 new backend unit tests in `test_filter_logic.py` (179 total, 0 regressions)
- **FilterBuilder UX improvements** — CONFIRMED (2026-03-17): value inputs (text/number) commit on blur or Enter only — no emit on each keystroke; filter panel has collapse/expand chevron toggle (collapsed view shows chips + logic expression); filter chips show numbered badge (① ②…) before the label
- **Country filter bug fixes** — CONFIRMED (2026-03-17): `contains "Italy"` no longer matches Switzerland/Lithuania/UK (ISO normalization "Italy"→"IT" was causing "it" substring match against display names; fixed to use raw search string for partial-match operators); multi-country OR correction applied in expression mode (`pass_site` now also OR-combines country rules when using `_eval_logic_expr_be`)
- `FilterGroup.rules` type in `api.ts` accepts both legacy `FilterRule` (op) and new `Rule` (operator); `FilterGroup.logic: string`
- Moby-returned FilterGroup trees are converted to flat+expr format via `filterGroupToFlat()` when received by ExplorerView events
- TanStack table with sorting, pagination, ColumnPicker, non-empty column detection
- **Table column/cell truncation** — CONFIRMED (2026-03-17): column headers capped at `max-w-[180px]` with `truncate` + full label in `title` tooltip; cell content capped at `max-w-[260px]` with `truncate` + `title` tooltip when value >40 chars; CSV export unaffected (reads from row data, not DOM)
- Google Maps `MapView.tsx` renders pins coloured by clinical/referral/detect flags
- Filter chip display, chip removal with auto-re-search (uses `removeRuleFromExpr` to update expression)
- Row selection checkboxes + "Select all" banner
- `SiteDetailsModal` on row click
- `ChartModal` for bar/line/pie charts from Explorer data
- `GET /api/explorer/fields` drives field catalog in FilterBuilder; field groups from `fields_opportunity_curated.json`
- SWR cache keyed by `EXPLORER_BOOT_KEY` warms the map bootstrap on auth
- E2E coverage: `filters.spec.ts`, `table.spec.ts`, `map.spec.ts`, `qualification.spec.ts`, `parity.spec.ts`
- Backend unit tests: `test_salesforce_explorer.py` (pure function tests for `_eval_qual_rule`, `_qual_get`); `test_filter_logic.py` (expression evaluator, 27 tests)
- **Multi-country filter** — CONFIRMED (2026-03-16): `pass_site()` now treats multiple `site.country equals X` rules under AND as OR; `_flatten_filter_rules()` recursively extracts rules from nested FilterGroup sub-groups before classification. Tested: `scripts/test_multi_country.py` — 41/41 pass (20 unit + 14 API + 7 Moby).
- **Multi-country Moby handler** — CONFIRMED (2026-03-16): country planner handler in `ai_chat.py` collects ALL `_COUNTRY_MAP` matches (removed `break`), builds `IN (...)` SOQL for multi-country, returns OR `last_filters`.

### Qualification Upload & Link — CONFIRMED
- Excel upload → parse → geocode → store in Questionnaire hierarchy + SiteQual JSONB
- Link/unlink site to Salesforce Account
- Preview by site_id endpoint
- Delete questionnaire + SiteQual recompute
- Alembic migration `a1b2c3d4e5f6` normalises legacy dot-subcode keys in existing JSONB data
- E2E coverage: `qualification.spec.ts`
- **`_qual_get` 5-fallback lookup** — CONFIRMED (2026-03-17):
  1. Exact key
  2. First `_` → `.` (legacy compat)
  3. All `_` in section prefix → `.` — fixes 4 recent uploads that stored sections as `3.5.4__` instead of `3_5_4__`; affected 53 fields in sections 3.5.x and 3.7.x that were silently returning empty
  4. Strip section prefix → base slug
  5. Strip last `_word` suffix from base slug (resolves `3_8__autoantibodies_aab` → `autoantibodies`, `3_3__..._min` → `...`)
- **Qual field catalog deduplication** — CONFIRMED (2026-03-17): `QUAL_FIELD_BLACKLIST` in `salesforce_explorer.py` excludes 6 legacy unprefixed keys that were appearing as near-duplicates alongside their canonical section-prefixed counterparts: `are_insulin_tests_available`, `are_gad65_tests_available`, `autoantibodies`, `estimate_arrival_time_of_emergency_personnel`, `how_long_are_documents_retained`, `longest_single_day_visit_that_could_be_accommodated`. Catalog reduced from 244 → 238 qual fields, all with confirmed data access.

### Profiling Fields in Explorer — CONFIRMED (2026-03-16)
- 60 Profiling Opportunity fields added to `fields_opportunity_curated.json` with 7 subsection groups:
  - `Profiling › PI and Sub-I Experience` (13 fields)
  - `Profiling › Lead Study Coordinator` (5 fields)
  - `Profiling › Lead Study Nurse` (5 fields)
  - `Profiling › Clinical Site Set Up` (9 fields)
  - `Profiling › Patient Population` (7 fields)
  - `Profiling › Screening Initiatives` (11 fields)
  - `Profiling › Process & Admin` (10 fields — includes 6 new fields not previously in JSON: `Form_Questionnaire_received__c`, `C_Form_Questionnaire_sent__c`, `C_Meeting_Date__c`, `C_Meeting_Comments__c`, `Profiling_form_finalised_date__c`, `Profiling_form_uploaded_to_DB__c`)
- `SF_GROUP_RANK` in `FilterBuilder.tsx` updated: Profiling groups ranked 10–16 (before generic "Salesforce" at 21)
- API names verified against real Salesforce records (Bialystok ProfOp, 269-record coverage analysis)
- Backend SOQL auto-fetches fields on demand — no backend code changes needed
- End-to-end tested: Polish sites return `C_Meeting_Date__c`, `Form_Questionnaire_received__c`, `Profiling_form_uploaded_to_DB__c` ✓

### Members View — CONFIRMED
- `GET /api/members/bootstrap` + `POST /api/members/search` + `GET /api/members/{id}/detail`
- `MembersView.tsx` with multi-name tag input, country/level/role filters
- `MemberMapView.tsx` (Leaflet + teardrop INNODIA crystal pins)
- `MemberDetailsModal.tsx` (member contacts, sub-accounts, sub-account contacts)
- Prefetched in `App.tsx` after auth to avoid cold-start delay
- E2E coverage: `members.spec.ts` (comprehensive, fully mocked API)
- Backend unit tests: `test_members_explorer.py` exists (file confirmed in test directory)

### Moby AI Chat — CONFIRMED
- `POST /api/ai/chat/stream` SSE endpoint backed by Claude Sonnet 4.6 (`anthropic` SDK)
- Deterministic planner intercepts 15+ query patterns (country sites, ND top/by-country, Stage 1/2, HLA %, pharmacy/overnight, Study Coordinators/PI, activities, activity-country matrix, sites-per-country chart, nearest sites, km-of-assignment)
- `_is_complex_query()` triggers extended thinking (`interleaved-thinking-2025-05-14` beta)
- `_truncate_history()` limits context to last 12 user turns
- `ChatView.tsx` renders live streaming bubble, structured table (AIResultTable), optional chart
- `AIResultTable.tsx` has export CSV, "Open in Explorer", "Add columns", highlight buttons
- Math handler and conversational no-tools shortcut for follow-up efficiency
- Backend unit tests: `test_ai_chat.py` (pure function tests for `_is_complex_query`, `_truncate_history`)
- E2E coverage: `chat.spec.ts` (deterministic with SSE mock), `chat-live.smoke.spec.ts` (requires real SF session, skipped in CI)

### Nearby / Within-KM Features — CONFIRMED
- `POST /api/explorer/search/within-drive-km`: finds sites within N km of a base account (uses local Site DB coords + Google Maps Distance Matrix)
- `POST /api/explorer/search/nearby-multi`: same but takes multiple base_account_ids; minimum distance over all bases; used by Moby km-of-assignment handler
- `NearbyDrawer` in `ExplorerView.tsx` for interactive nearby search
- Moby "km-of-assignment" handler at top of `chat_api()` calls `nearby-multi` endpoint

### Geocoding — CONFIRMED
- On qualification upload: city/country looked up in `geonames_cities` first, then Google Maps Geocoding API fallback
- `geo.py` admin endpoints to re-geocode sites in local DB
- `startup_geo.py` (optional import) for initial geocoding on startup

---

## What Is Partially Implemented

### `explorer_bridge.py` Endpoints — INFERRED (server side exists; frontend wiring partial)
- `POST /api/explorer-bridge/highlight` and `POST /api/explorer-bridge/add-columns` exist as backend stubs
- They always return `{"ok": True}` — the actual state change happens through a CustomEvent in the frontend
- The "highlight" flow (Moby → highlight rows in Explorer) depends on `listenExplorerChange` in ExplorerView and `window.dispatchEvent` calls in AIResultTable
- No E2E test verifies the full cross-tab highlight flow end-to-end

### `salesforce_sync.py` — INFERRED (code exists, not actively used)
- `POST /api/salesforce/sync/profiling` fetches Profiling Opportunities and upserts local Sites
- `startup.py` `sync_salesforce_subaccounts()` is a placeholder that logs only
- The actual sync appears not to be called in the current startup path (`startup_geo` is optional; `sync_salesforce_subaccounts` runs only if imported and is a no-op)

### `profiling_kv` Table — INFERRED
- `ProfilingKV` model exists in `site.py`
- Referenced in `ALLOWED_TABLES` whitelist in `ai_chat.py` (`public.profiling_kv`) so Moby can SQL-query it
- No active write path visible in current code beyond `salesforce_sync.py`; whether it contains data in production is unknown from code alone

### Within-Drive-KM "Full Filter" Path — INFERRED
- The `within-drive-km` endpoint supports full FilterGroup (qual rules, SF rules, extras) but uses the local `Site` table for base coordinates — meaning only sites with uploaded qualifications appear as potential bases
- The `nearby-multi` endpoint uses the full CTS Opportunity account list for candidates (correct), but the within-drive-km base lookup is still constrained to local DB sites

### OpenAI Client in ai_chat.py — INFERRED (instantiated, likely unused)
- `client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))` at line 73 of `ai_chat.py`
- All actual AI calls now go through `_claude_chat()` (Anthropic SDK)
- The OpenAI client is instantiated unconditionally; if `OPENAI_API_KEY` is absent the SDK may still initialise silently. No code path calls `client.*` for Moby responses, but the import and instantiation remain

---

## Frontend ↔ Backend API Connections (Observed)

| Frontend call | Backend endpoint | File |
|---------------|-----------------|------|
| `GET /api/salesforce/me` | `salesforce_auth.py` | Header.tsx, useSalesforceAuth.ts |
| `GET /api/salesforce/oauth/login` | `salesforce_auth.py` | salesforce.ts |
| `GET /api/salesforce/logout` | `salesforce_auth.py` | salesforce.ts |
| `GET /api/salesforce/map/bootstrap` | `salesforce_explorer.py` | api.ts `explorerBootstrap()` |
| `GET /api/explorer/fields` | `salesforce_explorer.py` | api.ts `getExplorerFields()` |
| `POST /api/explorer/search` | `salesforce_explorer.py` | api.ts `explorerSearch()` |
| `POST /api/explorer/search/within-drive-km` | `salesforce_explorer.py` | api.ts |
| `POST /api/explorer/search/nearby-multi` | `salesforce_explorer.py` | api.ts |
| `POST /api/qualification/upload` | `qualification.py` | api.ts |
| `GET /api/qualification/list` | `qualification.py` | api.ts |
| `POST /api/qualification/link` | `qualification.py` | api.ts |
| `POST /api/qualification/unlink` | `qualification.py` | api.ts |
| `DELETE /api/qualification/delete/{id}` | `qualification.py` | api.ts |
| `GET /api/members/bootstrap` | `members_explorer.py` | App.tsx, MembersView.tsx |
| `POST /api/members/search` | `members_explorer.py` | MembersView.tsx |
| `GET /api/members/{id}/detail` | `members_explorer.py` | MemberDetailsModal.tsx |
| `POST /api/ai/chat/stream` | `ai_chat.py` | ai.ts `askAIStream()` |
| `POST /api/ai/chat` | `ai_chat.py` | ai.ts `askAI()` |
| `POST /api/explorer-bridge/highlight` | `explorer_bridge.py` | AIResultTable.tsx |
| `POST /api/explorer-bridge/add-columns` | `explorer_bridge.py` | AIResultTable.tsx |

---

## Test Coverage Summary

| Area | Backend unit | Frontend E2E |
|------|-------------|-------------|
| `_eval_qual_rule` + `_qual_get` | Yes (test_salesforce_explorer.py) | — |
| `_is_complex_query` + `_truncate_history` | Yes (test_ai_chat.py) | — |
| Members explorer logic | Yes (test_members_explorer.py) | Yes (members.spec.ts, fully mocked) |
| Dashboard navigation | — | Yes (dashboard.spec.ts) |
| Filter builder + search | — | Yes (filters.spec.ts) |
| Table display + pagination | — | Yes (table.spec.ts) |
| Map rendering | — | Yes (map.spec.ts) |
| Qualification upload | — | Yes (qualification.spec.ts) |
| Chat (deterministic mock) | — | Yes (chat.spec.ts) |
| Chat (live Salesforce) | — | Yes, smoke only (chat-live.smoke.spec.ts) |
| Parity (map/table count match) | — | Yes (parity.spec.ts) |
| Resilience (network errors) | — | Yes (resilience.spec.ts) |
| Performance (load time) | — | Yes (performance.spec.ts) |
| `salesforce_sync.py` profiling sync | No | No |
| `geo.py` regeocode endpoints | No | No |
| `explorer_bridge.py` cross-tab highlight | No | No |
| Within-drive-km full-filter path | No | No |

---

## Areas That Need Additional Inspection

### `salesforce_explorer.py` — Large File (~4700 lines)
The file contains two separate route groups (`salesforce_router` and `explorer_router`), the entire filter engine, three proximity endpoints, and all SF query helpers. Key sections not read in full:
- Lines 200–2400: `_build_account_map`, `_sf_query_all`, `_ensure_describes`, `_flatten_filter_rules`, `_infer_qual_fields`, map bootstrap (`/api/salesforce/map/bootstrap`), and describe-cache logic
- Lines 3120–4850: row-builder logic after filtering; `within-drive-km` full implementation

### `ai_chat.py` — Very Large File
Lines 200–4100 contain: `_build_knowledge_index()`, `SYSTEM_PROMPT` (entire prompt text including DOMAIN GLOSSARY), all tool implementations (`tool_salesforce_query`, `tool_explorer_search`, `tool_nearest_filtered_sites`, `tool_study_coordinators_with_activities`, activity tools, rank_sites, etc.), `_claude_chat()`, `_try_planner()` entry and math handler, and `chat_api()` itself. Lines 5300–6694 contain the km-of-assignment handler (outside `_try_planner`, at `chat_api` top level). These were only partially read.

### Country Name Duplication
`_ISO2` and `_ISO2_TO_DISPLAY` dicts exist in three separate files: `salesforce_explorer.py`, `members_explorer.py`, and `ai_chat.py` (`_COUNTRY_MAP`). `countryUtils.ts` provides a fourth copy on the frontend. These are not shared modules.

### `sf_sessions` Table vs. Alembic
The `sf_sessions` table is created directly by `salesforce_oauth.py` (`_ensure_sessions_table()` using raw psycopg3), not through Alembic. It is therefore not in the migration history and would not appear in `alembic current`.
