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
- E2E coverage: `filters.spec.ts` (11 tests: FILTER-1–5 + FLOGIC-1–6), `table.spec.ts`, `map.spec.ts`, `qualification.spec.ts`, `parity.spec.ts`
- **FilterBuilder E2E tests** — CONFIRMED (2026-03-17): 6 new FLOGIC tests cover logic bar appearance (2+ rules), edit modal + custom expression, invalid expression validation + disabled Apply, rule removal updating expression, collapse/expand toggle, collapsed panel chip visibility. Uses new testids: `filter-add-btn`, `filter-rule`, `filter-rule-remove`, `filter-logic-bar`, `filter-logic-expr`, `filter-logic-edit-btn`, `filter-logic-modal`, `filter-logic-textarea`, `filter-logic-error`, `filter-logic-valid`, `filter-logic-apply-btn`, `filter-panel-collapse-btn`
- Backend unit tests: `test_salesforce_explorer.py` (pure function tests for `_eval_qual_rule`, `_qual_get`); `test_filter_logic.py` (expression evaluator, 27 tests)
- **Multi-country filter** — CONFIRMED (2026-03-16): `pass_site()` now treats multiple `site.country equals X` rules under AND as OR; `_flatten_filter_rules()` recursively extracts rules from nested FilterGroup sub-groups before classification. Tested: `scripts/test_multi_country.py` — 41/41 pass (20 unit + 14 API + 7 Moby).
- **Nested FilterGroup logic preservation** — CONFIRMED (2026-03-17): `_filter_group_to_expr(fg)` added to `salesforce_explorer.py`. Called at the start of `explorer_search()` rule classification, BEFORE `_flatten_filter_rules()`. Converts nested sub-groups to expression format (`{logic: "(1 OR 2) AND 3", rules: [flat_leaf, ...]}`) so inner AND/OR logic is faithfully preserved instead of being discarded during flattening. Without this fix, `(ES OR IT) AND overnight` was incorrectly evaluated as `(ES OR IT OR overnight)` because flattening merged all leaf rules under the root logic. Short-circuits: if no sub-groups exist, or if logic is already an expression string, returns the group unchanged (zero overhead for normal queries). 6 new unit tests in `TestFilterGroupToExpr` (284 total).
- **Multi-country Moby handler** — CONFIRMED (2026-03-16): country planner handler in `ai_chat.py` collects ALL `_COUNTRY_MAP` matches (removed `break`), builds `IN (...)` SOQL for multi-country, returns OR `last_filters`.
- **Country handler → Explorer-based (2026-03-18)**: country planner handler now calls the Explorer internally (`site.country=ISO2`) instead of `Account.ShippingCountry` SOQL filter. The SOQL approach was unreliable for some countries (DE, GB, Nordic) where SF accounts have ShippingCountry blank or stored differently. Explorer `site.country` uses the local DB (reliable ISO2), then Stage1/Stage2/ND values are read from `row.data["sf.*"]` (already in Explorer response from curated fields). Country values in table use `_iso2_to_sf()` for full-name display.
- **Country DB fallback bug fix (2026-03-18)**: when `sf=None` (expired session), DB fallback now queries by ISO2 (`UPPER(s.country) IN ('DE')`) instead of full name (`LOWER(s.country) = 'germany'`) — sites table stores ISO2, so the old query always returned 0 rows. Extended to support multi-country (`IN` clause with all matched ISOs).
- **Activities handler scoping fix (2026-03-18)**: `_HandlerContext(query=qtxt, ...)` → `query=user_text` — `qtxt` was assigned after `_try_planner()` is called, causing `UnboundLocalError` logged as `[AI-CHAT] WARN: planner activities failed` on every query.
- **ISO2 word-boundary country matching (2026-03-19)**: `_COUNTRY_MAP` was matched with `if _cn in s` (substring), causing 2-letter ISO codes to collide with common English words ("me"→Montenegro in "show me", "it"→Italy in "sites", "at"→Austria in "at"). Fixed three matching sites in `_try_planner` to use `re.search(rf"\b{re.escape(k)}\b", s)` for aliases >2 chars and `re.search(rf"\b{re.escape(k.upper())}\b", user_text)` for ≤2 chars (requires uppercase in original query). Same fix applied to `resolve_countries()` in `country_norms.py` and all callers that must pass `user_text` (not lowercased `s`).
- **Members country handler fix (2026-03-19)**: Members institution planner (`_mem_inst_intent`) and SC planner now call `resolve_countries(user_text)` instead of `resolve_countries(s)` so uppercase ISO2 codes like "UK" are matched correctly. Country handler excludes Member queries (`\b(member[s]?|institution[s]?)\b`) to let Claude route to Members API instead of CTS Explorer.
- **Ground truth generator fixes (2026-03-19)**: `members_search()` was using `.get("members", [])` but endpoint returns `"rows"` key → fixed. Pharmacy field lookup now correctly finds `qual.3_6__is_your_pharmacy_on_site_or_off_campus` (categorical "On-site"/"Off-campus") instead of the free-text description field. A08 Scandinavia query updated to "Nordic countries" to match `_REGION_EXPANSIONS` with FI included.
- **Moby test suite results (2026-03-19)**: **92/92 — 100% pass rate.** All groups A–H fully passing. Final 3 failures fixed: H06 (T1D ranking handler added), H09 (PI search uses `title_contains="Investigator"` with `_is_sc` bypass for explicitly-matched contacts), H12 (new branch in `moby_handlers.py` step 12b for "how many sites per activity" → `tool_activity_country_matrix`). Previous session fixed pharmacy/overnight/qual handler, assignment handlers, SF pipeline handler, and patient clarifier.
- **Semantic correctness fixes (2026-03-19)**: Post-deploy testing revealed ground truth passed structurally but answers were semantically wrong. Fixes: (1) qual YES/NO fields changed from `is_not_null` to `contains "yes"` (case-insensitive) for overnight, ZnT8, HLA, insulin — now correctly excludes NO answers; (2) `filter_engine.py` comparison operators (`gt`/`gte`/`lt`/`lte`) now return False for None values instead of falling back to lexicographic string comparison (`str(None) > str(0)` was True); (3) `_asn_not` regex extended to catch "haven't/never" for H26 (sites not in any assignment); (4) `is_null` used for no-assignment filter instead of `equals 0` (None is how batch-extras marks sites with no assignments); (5) Per-country aggregation added to assignment handler when "per country"/"by country" detected (H30); (6) H28 "multiple clinical subaccounts" filter added to member institution handler (filters to `# Sites >= 2`); (7) Generic "autoantibodies" (no insulin/ZnT8 prefix) now triggers ZnT8 filter.
- **Demo question handler-conflict fixes (2026-03-19)**: 79-question demo suite (`scripts/test_demo_questions.py`) exposed multiple handler routing conflicts. Fixed: (1) G03 — country handler bail-out for `nearest/closest` queries; (2) S03 — pipeline handler `_pl_profiling_c` regex extended to match "profiling complete" word order; (3) A04 — assignment handler gains `_asn_most_sites` guard (prevents hijacking "which assignments have the most sites"); (4) A04/G06 — `_asn_near` guard prevents assignment handler from intercepting "near X activity" questions (should go to nearest handler); (5) X02 — Stage-1/2 handler bails out when query contains NOT-assignment language (passes to Claude with `extra.AssignmentsCount` filter); (6) P03 — SC handler gains city extraction + post-filter so "PI at the site in Barcelona" returns city-matched rows only; (7) P04 — SC handler handles "without coordinator" by querying all CTS accounts and subtracting those with coordinators; (8) `_contacts_intent` NameError fixed (variable removed from definition but was still referenced in `if` guard — all PI queries were returning null HTTP 200); (9) Member handler expanded to fire on DxLab/LAB/Patient-Org role queries without requiring "institution" keyword; (10) Member role filters switched from `_validated` SF fields (empty in prod) to proposed fields (`C_Deliver_Clinical_Grade_Services__c`, `C_Perform_Cutting_Edge__c`, `C_Contribute_as_a_Patient_Organization__c`); (11) MB4 patient clarifier — `_needs_patient_clarification` now early-bails when query explicitly asks for "patient organizations" (not a clarification scenario); (12) E07 — T1D ranking handler bails out when `newly diagnosed` detected (`_t1d_rank_has_qual` regex extended with `\bnewly.diagnos|\bnew.diagnos`).
- **Demo question fixes round 2 (2026-03-20)**: Additional handler fixes for 79-question suite: (1) G06/M09 — `handle_activity` gains near-site guard so "near the sites involved in X activity" skips to km-of-assignment handler; `handle_assignment_sites` gains near guard and sponsor guard; (2) E07 — ND handler uses `LIMIT 2000` for threshold queries (not top-N), was capped at 50; (3) Q03 — qual handler detects T1D threshold when Stage field not mentioned, adds `C_Number_of_T1D_Patients_currently_O_18__c > N` filter; (4) F01 — pipeline handler detects "profiling complete + not CTS yet" and adds `INNODIA_Clinical_Trial_Site__c = false` filter; (5) F02/F03 — profiling-stage filter switched from `C_Date_First_Contact__c` (null for all CTS sites in prod) to `C_Form_Questionnaire_sent__c is_not_null`; (6) X02 — new deterministic Explorer call for Stage1>N + NOT-in-assignment queries; (7) km-of-assignment P1c pattern added ("within N km of the sites assigned to NAME") + P1d ("near the sites involved in NAME", default 100 km); P1 stop-word discard prevents "the" from being extracted as assignment name; (8) A02 — sponsor detection regex fixed (`\bsponsore?(?:d|s|ing)?\b`), `handle_assignment_sites` gains sponsor bail-out, `handle_activity` sponsor handler rewired to fetch sites across all matching activities when "sites" intent detected → 77 unique sites across 4 Sanofi activities.
- **MB6/MB8 handler fixes (2026-03-20)**: Two new deterministic sub-handlers in the member institution planner block (`_try_planner`, `ai_chat.py`): (1) **MB6** — regex detects "how many subaccounts/clinical units does [institution] have"; finds institution by partial name match in `tool_members_search` cache; reads `# Sites` from flat row; returns direct count answer (KU Leuven → 3 subaccounts ✓); (2) **MB8** — regex detects "clinical units/subaccounts linked to" inside `_mem_inst_intent` block, after city filter narrows to 1 institution; calls `GET /api/members/{id}/detail` via httpx and returns `subaccounts` list as table (Paris → Hospital Paris Saint-Joseph → 1 unit ✓); (3) **MB7** — "which institution does the site in [city] belong to?" — new reverse SOQL handler added but confirmed **data gap**: no INNODIA site exists in Brest across all 195 CTS sites and 247 member SubAccounts. **Final score: 72/79** (MB6+MB8 fixed; MB7+S05+Q05+Q11–Q14 confirmed data gaps; M11 structural gap).
- **`_needs_patient_clarification` removed (2026-03-20)**: The deterministic patient-type clarifier (50 lines, 7 bypass conditions) was removed from `ai_chat.py`. It intercepted queries containing "patient(s)" or "t1d" and showed structured clarification buttons (currently/newly diagnosed × under 18/over 18). Removed because: (1) maintenance burden — every new handler required adding a bypass or it got silently blocked; (2) Claude handles T1D query ambiguity correctly on its own given the domain glossary in SYSTEM_PROMPT; (3) real INNODIA users write specific enough queries that forced clarification was more friction than help. `_needs_contact_clarification` and `_needs_report_wizard` remain unchanged. No regressions — MB1–MB4, MB6, MB8, E07, Q03 all pass as before.
- **Demo suite expansion + new section fixes (2026-03-20 session 2)**: Demo suite expanded from 79 → 116 questions (added OC, MAP, QU, RE, ST sections). New fixes in `ai_chat.py` and `filter_engine.py`: (1) **`gte`/`lte` operator fix** — `_OP_SYNONYM` in `filter_engine.py` now maps `gte`→`>=`, `lte`→`<=`, `gt`→`>`, `lt`→`<`; these operators were silently skipped in `_build_sf_where`, causing RE6/RE8 date-range filters to return all sites; now correctly returns 78 (profiling in last year) and 19 (meeting Q3 2024); (2) **ST8 — CTS+DxLab** — new SOQL handler traverses `C_Member__r.C_Deliver_Clinical_Grade_Services__c` relationship (DxLab is on RT_Member, not SubAccount); returns 44 CTS sites whose parent institution has DxLab role; (3) **MAP7 — "near Frankfurt airport"** — outer nearest handler extended to trigger on `\bnear(?:\s+to)?\b` (was only `nearest/closest`); city extraction regex now handles `near the [City] airport/corridor` patterns; (4) **Dynamic columns in country handler** — when user asks for specific data (profiling, meeting date, first contact, CTS status), that column is now appended to the country Explorer response; RE3 confirmed — `sf.C_Profiling_Complete__c` appears as 10th column; (5) **DxLab plural regex fix** — `\bdiagnostic\s+lab\b` → `\bdiagnostic\s+lab[s]?\b` in 3 places; (6) **Region expansions** — added `central europe`, `eastern europe`, `western europe`, `southern europe` → MAP6/ST3 correct; (7) **Pipeline date filters** — RE6/RE8 planner handlers added; (8) **OC1/OC2/OC3/MB6/MB8** — member contacts handler, CS role handler, subaccounts handler all working. Data gaps confirmed: OC2 (Edinburgh not in member data), MB7 (no Brest site), QU1/QU3/QU4/QU5/QU6/QU7 (all require specific site data or data not populated). **New section scores: OC 2/3, MAP 8/8, QU ~0/7 (all data gaps), RE 8/8, ST 11/11**.
- **Evidence columns audit + fix (2026-03-20 session 2)**: Audited all deterministic handlers for missing "evidence" columns (the principle: if a handler filters by X, column X must be visible so the user can verify the result). All handlers were already complete except `tool_activities_with_assignments_counts()` — it fetched `CreatedDate` in SOQL but discarded it. Fixed: now tracks `earliest`/`latest` CreatedDate per activity and adds **"Assignment Date Range"** column (`YYYY-MM-DD – YYYY-MM-DD`) to all rows. Always visible (useful even without date filter to see recent activity).
- **Sanofi sponsor filter fix (2026-03-20 session 3)**: Two bugs in sponsor query routing: (1) `moby_handlers.py` sponsor regex `(?:the\s+)?sponsor` didn't match "is **a** sponsor" — fixed to `(?:(?:a|the)\s+)?sponsor` in all 3 sponsor patterns; (2) "assignments where Sanofi is a sponsor" was swallowed by the assignment Explorer handler (no sponsor guard), and `handle_activity` didn't fire because "assignment" ≠ "activit"; fixes: added `_asn_is_sponsor` guard to assignment handler in `ai_chat.py`; extended `_act_intent` in `handle_activity` to also match assignment+sponsor queries. Both "activities where Sanofi is a sponsor" and "assignments where Sanofi is a sponsor" now correctly return filtered activity list (sponsor column visible).
- **Moby handler fixes round 3 (2026-03-23)**: Five additional query failures fixed in `ai_chat.py` and `moby_handlers.py`: (1) **"participating to X opportunity"** — `handle_assignment_sites` and `_has_asn` only matched "participating in"; added "to" as alternative and "opportunity" as synonym for "assignment" in 3 places; (2) **Italy Phase 1 duplicate columns** — CT handler was requesting `sf.Account.Id/Name/ShippingCountry/City` from Explorer causing those to appear as extra columns alongside `account_name/country/city` top-level fields; removed them; also added country detection via `resolve_countries()` so "clinical sites in Italy with Phase 1" now filters to IT only; (3) **"how many stage 2 people in Italy?"** — `asks_country_sites` didn't match "many/people/individuals/patients" so the country handler was bypassed and Claude produced a raw SOQL response with `Account.*` columns; extended regex to catch these words; country handler now fires and sorts by Stage 2 descending; (4) **"investigator names and contact details"** → "couldn't complete" — `pi_intent` only matched "principal investigator" or "pi"; extended to also match standalone `\binvestigator[s]?\b`; (5) **"PI names + SC details"** → only SC returned — when both `sc_intent` AND `pi_intent` true, now runs both queries (SC with `title_contains="Study Coordinator"`, PI with `title_contains="Investigator"`) and merges deduped rows; `sc_title` column discriminates contact type.
- **Moby Step 3 — multi-activity intersection handler (2026-03-23)**: New `handle_multi_activity_intersection` in `moby_handlers.py` (Handler 1b, fires before `handle_assignment_sites`). Detects "sites in BOTH X and Y" via two SOQL queries + Python set intersection. GQ15 ("sites in both DETECT and Fabulinus") → 20 sites correctly. Root cause was that a single SOQL WHERE with AND on two activity names always returns 0 rows — each Assignment__c belongs to exactly one activity. 17 new unit tests, 79 total pass.
- **Moby Step 4 — multi-country AND coverage handler for GQ18 (2026-03-24)**: New sub-path 12c in `handle_activity` in `moby_handlers.py`. Detects "which activities have sites in Germany, France AND Italy?" (2+ countries joined by AND, no OR). Calls `tool_activities_with_countries` (already existed) → Python set filter: keeps only activities where `required_countries ⊆ activity_countries_set`. GQ18 → 7 activities correctly (was: 290 rows / OR semantics). Two-part fix: (1) sub-path 12c detection logic; (2) own country extraction on `s.rstrip('?!.')` because the shared `m_c2` regex uses `$` anchor which fails on queries ending with `?`. 79 unit tests pass. Non-regression confirmed: GQ15 unchanged (20 sites), OR country queries and country-only queries unaffected.
- **Deploy status (2026-03-24)**: All fixes are local only (main branch), not yet deployed to ECS. Last ECS deploy: TD:95 backend / TD:51 frontend.

### Qualification Upload & Link — CONFIRMED
- Excel upload → parse → geocode → store in Questionnaire hierarchy + SiteQual JSONB
- Link/unlink site to Salesforce Account
- Preview by site_id endpoint
- Delete questionnaire + SiteQual recompute
- Alembic migration `a1b2c3d4e5f6` normalises legacy dot-subcode keys in existing JSONB data
- E2E coverage: `qualification.spec.ts`
- **ZnT8 duplicate fix** — CONFIRMED (2026-03-17): `"znt8"` and `"are_znt8_tests_available"` added to `QUAL_FIELD_BLACKLIST`; catalog now shows only canonical `qual.3_8__znt8`.
- **Empty filter value guardrail** — CONFIRMED (2026-03-17): Backend `_build_sf_where()` skips rules where operator requires a value but value is empty/null (prevents HTTP 500 from malformed SOQL). Frontend `FilterBuilder.tsx` shows inline amber `⚠ value required` badge on incomplete rules.
- **Account Name always visible** — CONFIRMED (2026-03-17): `applyVisibleColumns()` in `ExplorerView.tsx` always re-adds `sf.Account.Name` if missing. `ColumnPicker.tsx` gains `lockedKeys` prop — locked columns show checked+disabled with "always on" label; `clearColumns` preserves them. `ExplorerView` passes `LOCKED_COLUMNS = ["sf.Account.Name"]`.
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
- **Invalid ID → 404** — CONFIRMED (2026-03-17): `GET /api/members/{id}/detail` now validates the account_id format (15-18 alphanumeric chars) upfront and returns 404 instead of 500; `SalesforceMalformedRequest` also caught → 400.

### Moby AI Chat — CONFIRMED
- `POST /api/ai/chat/stream` SSE endpoint backed by Claude Sonnet 4.6 (`anthropic` SDK)
- Deterministic planner intercepts 15+ query patterns (country sites, ND top/by-country, Stage 1/2, HLA %, pharmacy/overnight, Study Coordinators/PI, activities, activity-country matrix, sites-per-country chart, nearest sites, km-of-assignment, assignment-sites)
- **Assignment-sites planner handler** — CONFIRMED (2026-03-17): new deterministic handler in `_try_planner()` intercepts queries like "sites that belong to Barricade Delay assignment".
- **Activity/assignment query robustness** — CONFIRMED (2026-03-17): Three bugs fixed in `ai_chat.py`: (1) "belong to X activity" without quotes now extracts the name via regex and calls `tool_sites_by_activity` before falling through to `tool_sites_with_any_activity` (was returning ALL sites instead of filtered ones); (2) `_assign_name_m` regex now allows `()` in the captured name (changed `[\w\s'\-]` → `[\w\s'\-\(\)]`) so "Barricade Delay (JAJJ) assignment" correctly extracts the name; (3) `tool_sites_by_activity` now adds a consonant-deduplication fuzzy variant when building LIKE patterns (e.g. "barricade" → "baricade") so user typos like double-r still match the Salesforce name.
- **Phase 3: Activity handler extraction to `moby_handlers.py`** — CONFIRMED (2026-03-17): The ~380-line monolithic activity+assignment block in `_try_planner()` was extracted to `backend/app/routers/moby_handlers.py` as three standalone handler functions: `handle_followup_activity_sponsor`, `handle_assignment_sites`, `handle_activity`. Handler inputs are made explicit via `HandlerContext` dataclass; tool functions are injected via `ActivityTools` dataclass (avoids circular imports). `_try_planner()` becomes a clean 30-line dispatcher. Priority order preserved. 467 backend unit tests passing (405 pre-extraction + 62 new direct handler tests in `test_moby_handlers.py` covering all 13 sub-paths, both dataclasses, priority order, return contract, and negative cases). The new tests directly instantiate `HandlerContext` + mock `ActivityTools` — no Salesforce connection or `ai_chat.py` imports needed.
- **Phase 1 + 2 Structured Planner** — CONFIRMED (2026-03-17): `backend/app/routers/moby_planner.py` — pure-function structured query parser. `parse_query_plan(text, kindex, last_filters, last_table)` → `MobyPlan` TypedDict with `intent`, `countries` (ISO2), `filters` (resolved field keys), `requested_columns`, `output_mode`, `needs_export`, `unresolved_terms`, `confidence` (0–1), `last_filters_reused`. Phase 1: (a) if `can_short_circuit()` (confidence ≥ 0.80, table_query, all resolved) → calls `tool_explorer_search` directly without LLM, with optional CSV when `needs_export=True`; (b) otherwise injects `plan_to_system_hint()` as a system message. Phase 2 additions: (c) `_extract_catalog_filters()` scans the full kindex for any field mention with "with/without/> N/is available" context (≥5-char aliases); (d) `can_followup_merge()` + `merge_with_last_filters()` combine new countries/filters into existing `last_filters` for followup intents — executed directly, no LLM; (e) `build_clarification()` returns a structured clarify dict for known ambiguities (e.g. bare "pharmacy") instead of calling Claude; (f) `CLARIFICATION_TEMPLATES` drives structured option buttons in the frontend. 76 unit tests in `test_moby_planner.py`, 255 total backend tests passing. Bug fixes (2026-03-17): (1) `_try_planner()` country handler now skips when query starts with followup prefix ("and", "also", "y", etc.) AND `last_filters` is present — passes through to Phase 2 merge instead; (2) country handler `last_filters` now uses ISO2 codes ("DE") instead of full SF names ("Germany") so Phase 2 merge can accumulate countries correctly; (3) `POST /api/explorer/search` now returns `columns` metadata array in response (was returning `{}`), built from `requested_cols` or auto-detected from first row's data keys.
- **Option B: pre-planner followup merge** — CONFIRMED (2026-03-17): `parse_query_plan()` is now called BEFORE `_try_planner()` whenever `last_filters` is present in the payload. If `can_followup_merge()` fires, the merge is executed immediately and returned — `_try_planner` never runs. This catches followup phrasings that don't start with explicit prefix words ("Germany too", "add Portugal", "what about Belgium?", "y también Alemania") that the old `_is_followup_prefix` guard in `_try_planner` would miss. The pre-computed plan is reused in the Phase 1+2 block below to avoid double computation.
- **Phase 4: Complex FilterGroup logic in Moby** — CONFIRMED (2026-03-17): Moby now understands and generates nested AND/OR FilterGroup structures end-to-end. Changes: (1) `validate_filter_group(fg, depth)` added to `moby_planner.py` — pure validation function checking field prefixes (site.*, sf.*, qual.*), operator validity, value presence, and recursive sub-groups (depth ≤ 5); exported and imported in `ai_chat.py`; (2) `tool_explorer_search()` calls `validate_filter_group()` before executing — if errors found, returns structured error dict to Claude so it can self-correct instead of silently misfiring; (3) TOOLS_SPEC `explorer_search` filter description updated with concrete nested group examples (multi-country OR sub-group, `(DE AND overnight) OR (FR AND overnight)`, expression string format); (4) SYSTEM_PROMPT BLOCK 9 updated: country values now ISO-2 (was full names like "Spain"), new BLOCK 9b with 4 complex grouped query examples covering all Phase 4 target patterns; (5) `scripts/test_moby_filter_logic.py` — integration test suite: 5 Explorer direct tests (nested groups, expression strings, 3-country OR, deep nesting) + 7 Moby tests + 1 validation — **29/29 pass**; (6) **Backend fix `_filter_group_to_expr()`**: in `salesforce_explorer.py`, converts nested sub-groups to expression format before rule classification so inner AND/OR logic is faithfully preserved (see Nested FilterGroup entry above); (7) **Stage 1/2 handler country guard**: Stage 1/2 site-list handler in `_try_planner()` now skips when query mentions specific countries — falls through to Phase 1 planner to build the correct country+stage FilterGroup (fix: `_s12_has_country = any(k in s for k in _COUNTRY_MAP)`); (8) **Option-B 0-rows fix**: when the followup merge produces 0 rows, the correctly-merged FilterGroup is now returned directly (was falling through to Claude which would generate a different/wrong filter). 284 unit tests passing (23 new in `TestValidateFilterGroup`, 6 new in `TestFilterGroupToExpr`).
- `_is_complex_query()` triggers extended thinking (`interleaved-thinking-2025-05-14` beta)
- `_truncate_history()` limits context to last 12 user turns
- `ChatView.tsx` renders live streaming bubble, structured table (AIResultTable), optional chart
- `AIResultTable.tsx` has export CSV, "Open in Explorer", "Add columns", highlight buttons
- Math handler and conversational no-tools shortcut for follow-up efficiency
- Backend unit tests: `test_ai_chat.py` (pure function tests for `_is_complex_query`, `_truncate_history`)
- E2E coverage: `chat.spec.ts` (deterministic with SSE mock), `chat-live.smoke.spec.ts` (requires real SF session, skipped in CI)

### Backend Module Structure — CONFIRMED (2026-03-18)

**`salesforce_explorer.py` split (Fase 4 Task A):**
- **`backend/app/routers/filter_engine.py`** (369 lines, NEW): Pure filter evaluation — no SF/DB/network dependencies. Contains `Rule`, `FilterQuery`, `_OP_SYNONYM`, `_OP_MAP`, all scalar comparison functions (`_parse_date_any`, `_coerce_scalar`, `_cmp`, `_normalize_list`), `_eval_qual_rule`, `_qual_get`, `_flatten_filter_rules`, `_filter_group_to_expr`, `_is_logic_expr`, `_eval_logic_expr_be`, `_norm_label_to_key`. Directly tested by `test_filter_logic.py` and `test_salesforce_explorer.py` (updated imports).
- **`backend/app/utils/geo_cache.py`** (95 lines, NEW): Geocoding cache (disk-backed JSON, 10-year TTL) and `_haversine_km`. Contains `_GEO_CACHE`, `_GEO_LOCK`, `_geo_key`, `_save_geo_cache_file`, `_geo_cache_get`, `_geo_cache_put`, `_extract_result_country_iso`. Imported by `salesforce_explorer.py` and `ai_chat.py`.
- **`salesforce_explorer.py`**: 5645 → 5252 lines (−393). Imports from `filter_engine` and `geo_cache`. Two distinct cache locks: `_GEO_LOCK` (in geo_cache.py) for geocode cache, `_CACHE_LOCK` (in salesforce_explorer.py) for drive-matrix cache.
- **`backend/app/routers/explorer.py` DELETED (2026-03-18)**: Dead code — legacy `/api/explorer/fields` + `/api/explorer/search` stub that was never registered in `main.py`. The real explorer routes come from `salesforce_explorer.py`'s `explorer_router`. Never imported anywhere.
- **6 correctness/ops bugs fixed (2026-03-18):**
  1. `pass_account()` now supports expression-mode logic (`"1 AND (2 OR 3)"`): uses `account_rule_indices` + `_eval_logic_expr_be` — was treating any expression string as OR. 8 new unit tests in `TestPassAccountExpressionMode`.
  2. `within_drive_km` and `nearby_multi` now call `_filter_group_to_expr(filters)` before rule classification — nested FilterGroup sub-groups are no longer silently skipped. `nearby_multi` also switched from raw `filters.get("rules")` to `_flatten_filter_rules()`.
  3. `explorer_search` 4 main SF I/O calls wrapped in `asyncio.to_thread()` — prevents blocking the uvicorn event loop during Salesforce HTTP round-trips.
  4. Dead `/healthz` route removed from `main.py` — the real healthz (with DB `SELECT 1`) in `health.py` is always used.
  5. `geo_cache.py` load/save errors now emit `log.warning` instead of silent `pass`.
  6. `ChatView.tsx` now aborts any in-flight SSE stream via `useEffect` cleanup on component unmount.
- `_get_sf()` now returns HTTP 401 with actionable messages instead of generic 403: missing cookie → "Not logged in to Salesforce", invalid signature → "Session invalid or expired", DB session not found → "Session expired. Please log in again." (Fase 4 Task C)
- Country-only Moby fast-path: evaluated and not implemented — the existing `_try_planner` country handler already bypasses Claude; SOQL round-trip is the bottleneck, not routing. Documented in `next-steps.md` item 15. (Fase 4 Task B)
- **DB pool configured (2026-03-18)**: `database.py` now sets `pool_size=5`, `max_overflow=10`, `pool_timeout=10` (raises after 10s waiting for connection instead of blocking forever).
- **`init_db()` failure is fatal (2026-03-18)**: `main.py` `on_startup()` now raises `RuntimeError` if `init_db()` fails — ECS task will crash and restart instead of silently starting without a DB connection.
- **`_sf_query_all` exception isolation (2026-03-18)**: SF exceptions (`SalesforceMalformedRequest`, `SalesforceGeneralError`, `SalesforceRefusedRequest`, `SalesforceResourceNotFound`) are caught and returned as clean HTTP errors; raw SF exception text is never included in HTTP detail (only logged server-side).
- **Distance Matrix retry/backoff (2026-03-18)**: `_drive_km_matrix()` now retries up to 3 times with exponential backoff (1s/2s/4s) on HTTP 429, 5xx, or timeout. `_dm_cache` process-local limitation documented with Redis upgrade path.

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

**Backend unit tests: 475 passing** (2026-03-18)

| Area | Backend unit | Frontend E2E |
|------|-------------|-------------|
| `_eval_qual_rule` + `_qual_get` | Yes (test_salesforce_explorer.py) | — |
| `pass_account()` expression mode | Yes (test_salesforce_explorer.py — 8 new tests) | — |
| Filter logic expressions | Yes (test_filter_logic.py — 27 tests) | Yes (filters.spec.ts FLOGIC-1–6) |
| `_is_complex_query` + `_truncate_history` | Yes (test_ai_chat.py) | — |
| Moby planner handlers | Yes (test_moby_planner.py — 76 tests) | — |
| Activity/assignment handlers | Yes (test_moby_handlers.py — 62 tests) | — |
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

### Moby Integration Test Suite — CONFIRMED (2026-03-18)

Two-step integration test framework for validating Moby answers against 80+ real user questions. **Tier 1 + Tier 2 fixes applied (2026-03-18).**

**Tier 1 — Moby bug fixes in `ai_chat.py` + `moby_handlers.py`:**
1. **Region expansion**: `_REGION_EXPANSIONS` dict maps Scandinavia/Nordic/DACH/Benelux/Baltic/Iberian/Balkans → ISO2 list; fires before `_COUNTRY_MAP` scan in country planner handler
2. **NL ShippingCountry variant**: `_SF_EXTRA_VARIANTS` dict (NL → "The Netherlands", GB → "Great Britain") expands SOQL IN clause for countries with multiple SF spellings
3. **SC/PI handler country extraction**: Replaced broken `m_c` regex (matched "in" inside "investigators") with `resolve_countries(s)` from `country_norms.py`
4. **Nearest handler city terminator**: Added `?!` to regex terminator so "Munich?" is correctly extracted (was returning None)
5. **Members routing**: New planner handler detects "member institution(s)" queries → routes to `tool_members_search` instead of Explorer
6. **D04 "participating in Safeguard"**: `moby_handlers.py` `handle_assignment_sites` now matches "participating in X" / "involved in X" without requiring "assignment" keyword
7. **Country SOQL fix** (2026-03-18): Country planner SOQL had `Account.RecordType.DeveloperName='SubAccount'` and `Account.C_Type__c='Clinical'` — but both are Opportunity-level fields, not Account fields. This caused all Group A tests to return ~90-116 sites instead of the correct country subset. Fixed to `RecordType.DeveloperName='SubAccount'` and `C_Type__c='Clinical'` (without `Account.` prefix); only `Account.ShippingCountry` keeps the prefix.

**Step 1 — `scripts/generate_ground_truth.py`**: Runs each test case against the Explorer/Members/Nearby API (not Moby) and records the expected `account_ids` as a JSON fixture (`fixtures/moby_ground_truth.json`). Discovers qual field keys dynamically from `/api/explorer/fields`. Covers 8 groups:
- Group A (10): Country filters
- Group B (17): SF field filters — **B01-B06** (Stage1/2/ND numeric), **B07/B09/B10/B15/B16** (Account fields) all converted to `moby_only` (Explorer returns HTTP 400 or wrong counts for these); **B08** (C_Phase_I_Type1__c) kept as `explorer_filter`; **B11-B17** moby_only
- Group C (up to 19): Qualification data — pharmacy, overnight, ZnT8, HLA, autoantibodies, etc. (keys discovered at runtime); **C04** (overnight+Stage2) moby_only; pharmacy field discovery uses specific keyword priority ("available"/"on-site"/"service" before plain "pharmacy")
- Group D (5): Activity/assignment filters; **D05** fixed (removed Account field, keeps only `extra_not_has`)
- Group E (5–6): Complex multi-filter; **E01-E04/E06** moby_only (sf_gt or Account fields); **E05** (country+qual) kept as `explorer_filter`
- Group F (6): Geographic/nearby baseline; all cases use `tolerance="ordered_top_n"` (precision ≥ 80%) — "subset" was too strict since Moby may return valid SF accounts not in the local Explorer DB
- Group G (5): Members view
- Group H (30): Moby natural language — structural checks only (has_table, min_rows, has_answer)

**Step 2 — `scripts/test_moby_questions.py`**: Sends each natural language question to Moby, extracts `table.rows` account_ids, compares with expected using precision/recall. Supports `--groups`, `--id`, `--tags`, `--fast` (skip moby_only), `--precision-threshold`, `--recall-threshold` flags. Exit code 1 on any failure.

```bash
# Generate ground truth (once per SF data refresh, ~2 min)
SF_SESSION_COOKIE="..." python scripts/generate_ground_truth.py

# Run all tests (~15-20 min)
SF_SESSION_COOKIE="..." python scripts/test_moby_questions.py

# Run only exact-verifiable groups
SF_SESSION_COOKIE="..." python scripts/test_moby_questions.py --groups A B C D E --fast

# Re-run only failed cases
SF_SESSION_COOKIE="..." python scripts/test_moby_questions.py --id B01 D03
```

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
