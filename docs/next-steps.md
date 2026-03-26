# CTS Dashboard v3 — Next Steps

These items are directly supported by observations in the code. Each entry states what was observed, why it matters, and which files are affected. No speculative or generic recommendations.

---

## 0. ACTIVE INCIDENT — Google Maps Distance Matrix cost spike (P0)

**All other development is paused until the P0 items below are resolved.**

Full details, evidence, and checklist: **`docs/incident-distance-matrix-cost.md`**

Summary: the `within-drive-km` and `nearby-multi` endpoints generated ~186,000 billable Distance Matrix elements in March 2026 (~$730 unexpected cost). A hard guard rail (`MAX_DISTANCE_MATRIX_ELEMENTS = 2000`) was added on 2026-03-25 as stop-gap. Remaining P0 work:

- [ ] Shared Distance Matrix cache (replace per-worker `_dm_cache` with file/SQLite/Redis)
- [ ] Increase cache TTL from 1h to 24–48h
- [x] ~~Add haversine pre-filter to `within-drive-km`~~ — DONE (2026-03-25). Both endpoints now have haversine sort + top-N cap (`DM_CANDIDATES_PER_BASE=20`)
- [ ] Cap Moby's Distance Matrix usage in `ai_chat.py`
- [ ] Set Google Cloud daily quota on Distance Matrix API

---

## 1. ~~Remove dead OpenAI client instantiation from `ai_chat.py`~~ — DONE (2026-03-17)

**Observation:** Line 73 of `ai_chat.py`:
```python
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
```
The `client` object is never called in any Moby code path. All LLM calls go through `_claude_chat()`. The `OpenAI` import and instantiation remain from before the Gemini→Claude migration.

**Why it matters:** On ECS, `OPENAI_API_KEY` is not in the Secrets Manager secret (`prod/cts-dashboard/backend`). The OpenAI SDK may log a warning or fail silently at startup. The dead code also misleads anyone reading the file about which LLM is used.

**Files:** `backend/app/routers/ai_chat.py` — remove the `from openai import OpenAI, ...` import and the `client = OpenAI(...)` line. Check if `APITimeoutError`, `APIConnectionError`, `RateLimitError` from openai are referenced anywhere; if not, remove the import.

---

## 2. ~~Extract shared country-normalisation code into a single module~~ — DONE (2026-03-17)

**Observation:** The ISO2 ↔ country-name mapping now exists in **five** separate places:
- `salesforce_explorer.py`: `_ISO2` (name→ISO2) and `_ISO2_TO_DISPLAY` (ISO2→name)
- `members_explorer.py`: `_ISO2` (name→ISO2) and `_ISO2_TO_DISPLAY` (ISO2→name) — identical copy
- `ai_chat.py`: `_COUNTRY_MAP` (name→(sf_name, ISO2)) — different dict structure
- `moby_planner.py`: `_ALIAS_TO_ISO2` — most complete (includes adjective forms, Spanish names)
- `frontend/src/lib/countryUtils.ts`: `ISO2_COUNTRY` (ISO2→name)

**Why it matters:** `members_explorer.py` was added after `salesforce_explorer.py` and its `_ISO2` / `_ISO2_TO_DISPLAY` blocks were literally copied. Adding a new country (e.g., Montenegro `ME`) requires the same edit in at least 3 Python files. `moby_planner.py`'s `_ALIAS_TO_ISO2` is the richest — use it as canonical source.

**Files:** Create `backend/app/utils/country_norms.py`, move the canonical maps there, import in `salesforce_explorer.py` and `members_explorer.py`. Update `ai_chat.py`'s `_COUNTRY_MAP` to derive from the same source if practical.

---

## 3. ~~Add `sf_sessions` table to Alembic migration history~~ — DONE (2026-03-17)

**Observation:** `services/salesforce_oauth.py` creates the `sf_sessions` table using raw psycopg3 DDL (`_ensure_sessions_table()`), not through Alembic. The table is therefore invisible to `alembic current`, `alembic heads`, and `alembic downgrade`.

**Why it matters:** If a new developer runs `alembic upgrade head` on a fresh DB, the `sf_sessions` table will be created by `_ensure_sessions_table()` at startup — but it bypasses migration tracking. A future migration that tries to `ALTER TABLE sf_sessions` will fail unless the table was created via Alembic first.

**Files:** Add a new Alembic migration in `backend/alembic/versions/` that creates `sf_sessions` with `op.create_table(...)` wrapped in a `IF NOT EXISTS` equivalent. Set its `down_revision` to `a1b2c3d4e5f6` (the current head).

---

## 4. Add `within-drive-km` endpoint to E2E test suite

**Observation:** The `within-drive-km` endpoint is fully implemented in `salesforce_explorer.py` and is used by the `NearbyDrawer` in `ExplorerView.tsx`. It also has a dedicated Moby planner handler. However, no entry exists in `frontend/tests/e2e/` that exercises the `NearbyDrawer` UI or mocks the `**/api/explorer/search/within-drive-km` route.

**Why it matters:** The endpoint's filter logic is the most complex in the file (qual + SF + extras + account rules, plus Distance Matrix geocoding). A regression in base-coord resolution or the pass_qual path would not be caught by any current test.

**Files:** Add test cases in `frontend/tests/e2e/filters.spec.ts` or a new `nearby.spec.ts`. Mock `/api/explorer/search/within-drive-km` returning a fixed response. The existing `**/api/salesforce/map/bootstrap` mock pattern in `filters.spec.ts` shows the correct approach.

---

## 5. Wire `sf.Assignment.* ` fields (other than Name) in Explorer filter engine

**Observation:** In `salesforce_explorer.py` lines 2601–2607 and 2614–2620, `sf.Assignment.Name` is explicitly redirected to `extra.AssignmentsNames`. The code comment reads:
```python
# Other sf.Assignment.* fields (Type, Stage, MCA, Payment) silently skipped
```
This means if a user builds a filter on `sf.Assignment.C_Assignment_Stage__c` or `sf.Assignment.C_Payment_Done__c` in the FilterBuilder, it is silently dropped.

**Why it matters:** These fields appear in the SYSTEM_PROMPT for Moby (`Block 10: Direct Account/Contact/Assignment queries`) and in `fields_opportunity_curated.json` entries could expose them to the field catalog. A user selecting them in FilterBuilder would get no results and no error.

**Files:**
- `backend/app/routers/salesforce_explorer.py` — implement fetching `Assignment__c` fields via `batch_fetch_account_extras` or a new SOQL batch, or emit a clear 400 error for unsupported Assignment fields
- `backend/app/config/fields_opportunity_curated.json` — either add the Assignment fields with a note that they require extras, or exclude them from the catalog if they cannot be filtered

---

## 6. Populate `profiling_kv` table or remove from allowed SQL tables

**Observation:** `ProfilingKV` is defined in `backend/app/models/site.py` and the table name `public.profiling_kv` appears in `ai_chat.py`'s `ALLOWED_TABLES` whitelist (line ~104). The `salesforce_sync.py` `sync_profiling` endpoint is the only write path, but `startup.py` `sync_salesforce_subaccounts` is a no-op placeholder. No code path in the current startup sequence populates `profiling_kv`.

**Why it matters:** Moby can SQL-query `profiling_kv` but would get 0 rows. If profiling data should appear in AI responses, the sync endpoint needs to be wired into the startup sequence or scheduled. If profiling data is no longer relevant, the table and allowlist entry should be removed.

**Files:** Either: activate `salesforce_sync.py` sync in `startup.py` (remove the no-op body), or remove `ProfilingKV` from `models/site.py`, remove `profiling_kv` from `ALLOWED_TABLES` in `ai_chat.py`, and drop the model.

---

## 7. ~~Resolve `nearby-multi` vs. `within-drive-km` base-coordinate asymmetry~~ — DONE

**Observation (2026-03-17 inspection):** `within-drive-km` at `salesforce_explorer.py:4457-4489` now uses the merged SF+local strategy: loads Site DB coords into `site_by_acc`, loads all CTS Opportunity accounts via `_build_account_map()`, then merges with SF coords preferred (`lat = a.get("lat") if a.get("lat") is not None else s.get("lat")`). This matches `nearby-multi`.

---

## 8. Add Alembic migration for `geonames_cities` index

**Observation:** `geonames.py` model defines `GeonameCity` with `name`, `country_code`, `population` columns. The qualification upload path queries this table with:
```sql
WHERE lower(name) = :n AND lower(country_code) = :c ORDER BY population DESC
```
The model has no index on `(lower(name), country_code)`. With a full geonames cities500 dataset (~200,000 rows), this query runs a full table scan on every qualification upload.

**Why it matters:** Slow uploads when the geonames table is populated. Alembic migration `9e4ac666b53b_add_geonames_cities_table.py` adds the table but does not add this functional index.

**Files:** Add a new Alembic migration that creates: `CREATE INDEX IF NOT EXISTS ix_geonames_cities_name_country ON geonames_cities (lower(name), lower(country_code))`. Set `down_revision` to `a1b2c3d4e5f6`.

---

## 9. ~~Add E2E tests for the new Salesforce-style FilterBuilder~~ — DONE (2026-03-17)

**Observation:** `FilterBuilder.tsx` was fully rewritten (2026-03-16) to the Salesforce-style numbered-rule + editable-logic-expression model. The logic expression parser (`filterLogic.ts`) has 27 backend unit tests but no Playwright E2E tests covering:
- Adding 3+ rules and verifying the "Include rows matching" bar appears
- Opening the Edit logic modal and typing a custom expression like `(1 AND 2) OR 3`
- Validating error feedback for invalid expressions (e.g., `1 AND 5` when only 2 rules exist)
- Removing a rule via the ✕ button and verifying the expression updates correctly
- Collapse/expand toggle (added 2026-03-17)

**Why it matters:** The filter UX is the primary interaction surface for Explorer users. Regressions in rule removal, expression rendering, modal validation, or collapse toggle would not be caught by the existing `filters.spec.ts` which tests filter *behavior* (search results) rather than filter *composition UI*.

**Files:** `frontend/tests/e2e/filters.spec.ts` — add a describe block `"filter logic expression"` that mocks bootstrap and search, adds rules, reads the "Include rows matching" text, opens the modal, and checks validation feedback.

---

## 11. ~~Fix `FilterBuilder.tsx` — `source` type excludes `"account"` and `"extra"`~~ — DONE (2026-03-17)

**Observation:** `FilterBuilder.tsx` defines `FieldDef.source` as `"sf" | "site" | "qual" | string`. The `GET /api/explorer/fields` response includes fields with `source: "account"` and `source: "extra"` (lines 2500–2521 of `salesforce_explorer.py`). The frontend `FieldDef` type in `api.ts` line 101 declares `source: "site" | "sf" | "qual"` — it does not include `"account"` or `"extra"`.

**Why it matters:** TypeScript compilation does not flag this because of the fallback `string` in FilterBuilder.tsx, but `api.ts` will mistype `FieldDef` objects with `source: "account"` or `source: "extra"`. If any code does `if (f.source === "account")` the TypeScript checker will warn or silent-fail.

**Files:** `frontend/src/lib/api.ts` — extend `FieldDef.source` type to `"site" | "sf" | "qual" | "account" | "extra"`. `frontend/src/components/FilterBuilder.tsx` — update local `FieldDef.source` type to match.

---

## 14. Remaining `_try_planner` handlers — extract to `moby_handlers.py` (Option B, deferred)

**Observation (2026-03-18):** Phase 3 extracted the activity/assignment handlers (3 functions, 62 direct unit tests). The remaining handlers in `_try_planner` (ND, T1D, Stage 1/2, CT3, country sites, nearest sites, pharmacy/overnight, HLA, group count, math) were NOT extracted because they close over `kindex`, `db`, `payload`, `request`, and ~15 tool functions — significantly more complex than the activity handlers. A `PlannerTools` dataclass with 15+ injected callables + expanded `HandlerContext` (adding `kindex`, `db`) would be needed.

**Why it matters:** These handlers are still harder to unit-test in isolation. A regression in, e.g., the ND handler requires tracing through the full `chat_api()` call.

**Files:** `backend/app/routers/ai_chat.py` (`_try_planner` closure), `backend/app/routers/moby_handlers.py` (would receive new handlers).

**Decision:** Deferred. Start with extracting the math handler (pure Python, no SF/httpx) and ND/T1D handlers (use only `sf` + simple SOQL). The km-of-assignment handler (currently at `chat_api` level) should also eventually move to `moby_handlers.py`.

---

## 15. Country-only Moby fast-path — evaluated, not implemented (2026-03-18)

**Observation:** For queries like "sites in Germany" (no extra filters), the existing `_try_planner` country handler already bypasses Claude and runs a direct SOQL query. The `moby_planner.py` `can_short_circuit()` and `can_followup_merge()` cover filter-bearing country queries. The bottleneck is the Salesforce SOQL round-trip (~1–2 s), not Python routing overhead.

**Why not implemented:** A server-side result cache (e.g., 5-min TTL per country) would reduce repeated-query latency but introduces cache invalidation risk and state management complexity. Not justified by observed latency. If Salesforce CDN-level caching or a local replica were available, the equation would change.

**Files:** No change. If implemented later: `backend/app/routers/ai_chat.py` `_try_planner` country handler, using `_cache_get`/`_cache_set` from `salesforce_explorer.py`.

---

## 16. Deploy demo-question handler fixes to ECS — READY TO DEPLOY (2026-03-20)

**Observation (2026-03-20):** All fixable demo questions now passing on local backend. Full 79-question suite result:

**Confirmed working (72/79):** E01–E10, C01–C05, S01–S04, Q01–Q04, Q06–Q08, Q10, M01–M11, G01–G06, P01–P05, A01–A06, F01–F05, X01–X04, MB1–MB4, MB6, MB8

**Data gaps — confirmed empty in prod (7/79):** S05 (C_Referral_Clinical_Partner__c unpopulated), Q05/Q11–Q14 (qual fields not in data), MB7 (no site in Brest — verified across all 195 CTS sites and 247 member SubAccounts), M11 (no assignment named "Germany")

**Structural gaps (1/79):** MB5 (no Germany Research Lab members in prod)

**Fixes applied in 2026-03-20 session:**
- `moby_handlers.py`: near guard in `handle_assignment_sites` + `handle_activity` for G06; sponsor bail-out in `handle_assignment_sites`; `\bsponsore?(?:d|s|ing)?\b` regex fix in `handle_activity`; sponsor handler rewired to fetch sites across all sponsor activities for "sites" intent (A02)
- `ai_chat.py`: ND handler LIMIT fix (E07), T1D threshold in qual handler (Q03), profiling NOT-CTS (F01), profiling stage filter `C_Form_Questionnaire_sent__c` (F02), Stage1+NOT-assignment Explorer handler (X02), km-of-assignment P1c/P1d patterns + P1 stop-word discard (M09/G06)

**Additional Sanofi sponsor fixes (2026-03-20 session 3):**
- `moby_handlers.py`: sponsor regex `(?:the\s+)?sponsor` → `(?:(?:a|the)\s+)?sponsor` (was not matching "is **a** sponsor"); `_act_intent` extended to also fire on "assignment" + "sponsor" keyword combo
- `ai_chat.py`: `_asn_is_sponsor` guard added to assignment Explorer handler so "assignments where X is a sponsor" no longer returns generic unfiltered active-assignment results

**Files:** `backend/app/routers/ai_chat.py`, `backend/app/routers/moby_handlers.py`. Run `python scripts/test_demo_questions.py` with fresh SF session cookie before deploying.

**Demo suite expanded (2026-03-20):** `scripts/test_demo_questions.py` grew from 79 → **116 questions** with 5 new sections: `OC` (3, org-level member contacts), `MAP` (8, map/geographic visual queries), `QU` (7, qualification upload/data quality), `RE` (8, reporting & export), `ST` (11, cross-functional/strategic feasibility + network planning + budget). Run new sections with `--section OC,MAP,QU,RE,ST`.

---

## 12. Commit or discard the untracked root-level deploy script before merging `dev` → `main`

**Observation:** `git status` on `dev` shows `builld_push_ECR_and_deploy_images_in_ECS_sso_profile_juan.sh` (note the typo: "builld") as untracked in the repository root. The tracked deploy scripts are `scripts/deploy.sh` (legacy) and `scripts/deploy_build_push_and_migrate.sh` (current, used per MEMORY.md). The untracked root file appears to be a personal one-off variant.

**Why it matters:** The untracked file will not be included in a `dev` → `main` merge and will be lost if the working directory is cleaned. If it contains important ECS deploy parameters (SSO profile, cluster/service names), those should either be committed (renamed and moved to `scripts/`) or documented.

**Files:**
- Either commit `builld_push_ECR_and_deploy_images_in_ECS_sso_profile_juan.sh` as `scripts/deploy_ecs_sso.sh` (rename to fix typo) before merging
- Or confirm the existing `scripts/deploy_build_push_and_migrate.sh` covers all needed steps and discard the root file

---

## 13. ~~Add Profiling and Qualification Opportunity fields to Explorer filter/column catalog~~ — DONE (2026-03-16)

**Observation:** Salesforce query on `dev` (2026-03-16) confirmed two Opportunity RecordTypes not currently represented in `fields_opportunity_curated.json`:
- `RT_Profiling_Opportunities` — 269 records; 72 fields have ≥ 1 non-null value
- `RT_CTS_Accreditation` (CTS/CTU Qualification) — 130 records

Key Profiling fields with data (% of 269 records): `StageName` (100%), `C_Profiling_Complete__c` (77%), `Form_Questionnaire_received__c` (61%), `C_Center_for_Running_Early_Diagnosis__c` (59%), `C_Form_Questionnaire_sent__c` (57%), `C_Meeting_Date__c` (56%), `No_of_Cases_FU_year__c` (54%), `C_Early_Diag_Capacity__c` (53%), `Center_Type__c` (52%), `C_Date_First_Contact__c` (47%), `Country__c` (47%), `Number_of_T1D_patients_seen_per_year__c` (46%), `Nb_T1D_Islet_Autoantibody_Tests__c` (45%), `C_Autoantibody_Testing_Possible__c` (43%).

**Why it matters:** Users can currently only filter/view CTS Opportunity fields. Profiling and Qualification opportunities represent pre-CTS pipeline data that is useful for site development and planning decisions.

**Files:**
- `backend/app/config/fields_opportunity_curated.json` — add a new `"Profiling"` section (or `"Qualification"` section) with selected fields. Use the same structure as existing sections: `{"api_name": "...", "label": "...", "type": "...", "group": "Profiling"}`.
- `backend/app/routers/salesforce_explorer.py` — ensure `_build_account_map` / `_sf_query_all` fetches the new fields when a Profiling/Qualification filter is active, or handles multi-RecordType queries.
- User confirmation required: which of the 72 Profiling fields (and which Qualification fields) to include.
