# Moby Agentic Loop — Ship Design

**Date:** 2026-04-21
**Status:** Awaiting Juan's review
**Predecessor:** [2026-04-09-moby-agentic-loop-design.md](2026-04-09-moby-agentic-loop-design.md)
**Goal:** Verify the agentic-loop refactor has no regressions against the 2026-03-25 bulk-eval baseline, then ship the full refactor (backend + frontend + tests) as a single commit and a single ECS deploy.

---

## Context

The agentic-loop refactor designed on 2026-04-09 has been sitting in Juan's working tree since then:

- `backend/app/routers/ai_chat.py` — ~2037 lines of diff (−1332 / +705)
- `backend/app/routers/moby_tools.py` — new, ~949 lines, 28 registered tools behind `@register_tool`
- `backend/app/routers/filter_engine.py`, `backend/app/routers/moby_planner.py` — minor companion changes
- `backend/tests/test_agentic_loop.py`, `backend/tests/test_tool_dispatch.py` — 12 + 15 new unit tests, all passing
- `frontend/src/lib/ai.ts`, `frontend/src/pages/ChatView.tsx` — SSE per-turn progress events (`{type: "progress", turn, tools}`) + per-tool cursor
- `CLAUDE.md`, `docs/current-state.md`, `docs/next-steps.md` — intent + status updates

Verification status before this design: 27/27 new tests pass, 551/551 base tests pass, Juan has done light manual testing. Bulk-eval (`scripts/moby_bulk_eval.py`, 121 questions) has not been re-run against the refactored code. The last known classification is `docs/moby-bulk-eval-initial.yaml` (84 PASS_NOW / 34 KNOWN_GAP / 2 FUTURE_FEATURE / 1 AMBIGUOUS, dated 2026-03-25).

Juan's risk tolerance (confirmed today): **measure locally, ship if no regression** (option `b` of the three presented).

---

## Success criteria

1. **No regression on the 84 PASS_NOW questions.** Definition of regression: the new run returns `status != "OK"` or lacks a table when the baseline marks `output_type: table` or the row count drops by more than one order of magnitude.
2. **Optional upside: KNOWN_GAP items that now work.** Not required for ship, but captured in the delta report.
3. **Prod backend + frontend on new ECR image.** Task def for backend advances (current is `cts-dashboard-backend:114`); frontend task def advances one revision.
4. **Smoke test passes in prod** for 2–3 representative queries (one multi-country, one proximity, one ranking) after deploy.

---

## Section 1 — Verify (~40 min + Claude token spend)

### 1.1 Pre-flight (5–10 min)

1. Restart local backend: `bash scripts/restart_local_backend.sh`. Backend runs on `:8000` with the refactor + my 2026-04-21 DM-cache changes already deployed to prod (harmless locally).
2. Juan pastes a fresh `sf_session` cookie from prod (reference procedure stored in auto-memory `reference_prod_smoke_token.md`).
3. Sanity check with one question before committing to the full run:
   ```
   SF_SESSION_COOKIE="..." curl -s -X POST http://localhost:8000/api/ai/chat \
     -H "Content-Type: application/json" -H "Cookie: sf_session=$SF_SESSION_COOKIE" \
     -d '{"message":"How many CTS sites do we have in total?"}' | jq
   ```
   Expected: `answer` mentions ~197 sites (matches GQ01 in baseline).

### 1.2 Bulk eval (~30 min)

```
SF_SESSION_COOKIE="..." python scripts/moby_bulk_eval.py
```

Output: `docs/moby-bulk-eval-raw.json` (overwrites existing). Estimated Claude spend: **$60–120** (121 questions × ~3 agentic turns × Sonnet 4.6 pricing with extended thinking on turn 1).

### 1.3 Regression check

New helper `scripts/compare_bulk_eval.py` (written as part of this plan, ~80 lines). Inputs:
- `docs/moby-bulk-eval-initial.yaml` (baseline classifications)
- `docs/moby-bulk-eval-raw.json` (new run)

Output: `docs/moby-bulk-eval-20260421-delta.md` with:

- **Per PASS_NOW question:** OK preserved, OK→ERR regression, row-count delta if >1 order of magnitude
- **Per KNOWN_GAP question:** noted if response now contains a table (candidate improvement, flagged for manual review)
- **Summary counts:** preserved / regressed / new-pass / unchanged

### 1.4 Decision rule

- **0 regressions in PASS_NOW** → proceed to Section 2
- **1–3 regressions** → Juan decides case-by-case; fix + re-run if needed
- **≥4 regressions** → stop. Investigate root cause before shipping

---

## Section 2 — Ship (if verify passes)

### 2.1 Commit

One commit, title: `refactor(moby): agentic loop + tool registry + per-turn SSE progress`.

**Explicit stage list** (no `git add -A` per commit-scoping rule):

| File | Status | Notes |
|---|---|---|
| `backend/app/routers/ai_chat.py` | modified | core loop |
| `backend/app/routers/moby_tools.py` | new | tool registry |
| `backend/app/routers/filter_engine.py` | modified | `is_null` synonyms |
| `backend/app/routers/moby_planner.py` | modified | `has_proximity` detection |
| `backend/tests/test_agentic_loop.py` | new | 12 tests |
| `backend/tests/test_tool_dispatch.py` | new | 15 tests |
| `frontend/src/lib/ai.ts` | modified | `ProgressEvent`, `onProgress` |
| `frontend/src/pages/ChatView.tsx` | modified | per-tool cursor |
| `CLAUDE.md` | modified | refactor notes (verify with `git diff` first) |
| `docs/current-state.md` | modified | updated status |
| `docs/next-steps.md` | modified | mark Task 14/15 done |
| `scripts/moby_bulk_eval.py` | new | eval harness |
| `scripts/compare_bulk_eval.py` | new (this session) | regression diff |
| `docs/moby-bulk-eval-initial.yaml` | new | baseline |
| `docs/moby-bulk-eval-20260421-delta.md` | new | ship evidence |

**Explicitly excluded** (not staged; remain as working-tree cruft for Juan to decide later):

- `td-frontend.json`, `td-frontend-new.json`, `td-frontend-editable.json` — ECS task def snapshots, unrelated to refactor
- `gq_post_fixes.json` — stale eval run
- `builld_push_ECR_and_deploy_images_in_ECS_sso_profile_juan.sh` — typo'd one-off deploy script (next-steps item 12)
- `scripts/test_agentic_queries.py` — verification helper, superseded by bulk eval
- `docs/moby-gq-baseline.md` — pre-refactor, unchanged
- `docs/superpowers/plans/2026-04-09-moby-agentic-loop.md` and `docs/superpowers/specs/2026-04-09-moby-agentic-loop-design.md` — ask Juan if he wants these committed alongside (they document the refactor itself, would be good provenance)
- `docs/moby-bulk-eval-raw.json` — raw dump, optional (small, could go in)
- `moby-step1/`, `moby-step2/` directories — contents unknown; ask Juan before touching

### 2.2 Deploy

```
git push origin main
bash scripts/deploy.sh              # backend + frontend, no --migrate
```

With the deploy.sh fix shipped earlier today (`15370b0`), the `BACKEND_ARN` capture is now clean; no manual workaround needed. No Alembic migration this round.

Wait for both `services-stable` (~3–5 min). Verify new task-def revisions via `aws ecs describe-services`.

### 2.3 Smoke test in prod

Log into `https://cts-innodia-dashboard.org` and run:

1. **Multi-country**: "Show me all CTS sites in Germany, Italy, and Belgium" — expect 79 rows
2. **Proximity**: "Which sites are within 100 km of Munich?" — expect 2 sites (Augsburg, Innsbruck)
3. **Ranking**: "Rank top 10 sites by combined Stage 1 + Stage 2" — expect 10 rows

CloudWatch `/ecs/cts-dashboard-backend` log group: tail for 15 min after deploy, scan for `ERROR`, `Traceback`, `TimeoutError`, `MOBY_MAX_AGENT_TURNS`.

---

## Section 3 — Safety net

### 3.1 Rollback (≤2 min)

```
aws ecs update-service --cluster cts-dashboard --service backend \
  --task-definition cts-dashboard-backend:114 --force-new-deployment \
  --region eu-west-1 --profile juan
aws ecs update-service --cluster cts-dashboard --service frontend \
  --task-definition cts-dashboard-frontend:<prev-revision> --force-new-deployment \
  --region eu-west-1 --profile juan
```

(`<prev-revision>` captured from `aws ecs describe-services` before deploy.)

No DB rollback needed — `dm_cache` table from today stays in place, refactor did not alter schema.

### 3.2 Post-deploy follow-up (within 24 h)

- Juan runs 5–10 ad-hoc multi-turn queries in the live UI to stress the agentic loop (conversational scope changes — the bigger vision stated today).
- If any tool picks the wrong params or the loop hits `MOBY_MAX_AGENT_TURNS` often, open a follow-up task for system-prompt tuning; do not revert.

---

## Open decisions (for Juan)

1. **Budget check** — OK with $60–120 Claude spend for a single bulk eval? Alternative: run only the 84 PASS_NOW subset (~$45–85).
2. **Eval harness commit scope** — commit `docs/moby-bulk-eval-raw.json` alongside the refactor, or leave it untracked? Small file (~200 KB) but ephemeral. Default: commit, it's provable evidence.
3. **Predecessor spec + plan** — commit `docs/superpowers/{specs,plans}/2026-04-09-moby-agentic-loop*.md` alongside? Provides git-native provenance of the design. Default: yes.
4. **Cookie flow** — paste cookie in-session when we reach step 1.1, or give me the login URL and I wait. Default: in-session paste.

---

## Non-goals / out of scope

- Tuning the agentic loop (turn cap, token budget, system prompt) — only if regression analysis forces it.
- Fixing the 34 KNOWN_GAP items — out of scope; tracked in next-steps.md for future work.
- Addressing other pending items from `project_state_2026_04_20.md` (prompt caching, multi-LLM fallback, SOQL escape bug in ai_chat.py lines 1643/1663/1666/1736/1739) — separate sessions.
- Cleaning `td-frontend*.json`, `moby-step*/`, `gq_post_fixes.json` — ask Juan in a later pass.

---

## Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bulk eval reveals regressions | medium | high | Decision rule in 1.4; ship-fail branches are explicit. |
| Local backend can't start due to working-tree drift | low | medium | Step 1.1 pre-flight; if fails, diagnose before running eval. |
| SF session cookie expires mid-run (~15–20 min per memory) | medium | medium | Bulk eval runs serially ~15s/question × 121 = ~30 min. Split into two halves if needed; script tolerates mid-run interrupt (writes partial results). |
| Prod Claude API rate limits | low | low | Local eval doesn't share quota with prod traffic. |
| ECS frontend deploy slower than backend | low | low | `services-stable` waits for both; no user impact between the two. |
| Rollback reveals `dm_cache` rows written by new code are incompatible | very low | low | Cache schema is generic `(key, value JSONB, created_at, ttl_seconds)`; old code would just miss on lookups and fall back. Safe. |

---

## After approval

Per the `superpowers:brainstorming` → `superpowers:writing-plans` handoff, once Juan approves this spec I invoke `writing-plans` to turn Sections 1–3 into a step-by-step implementation plan with checkpoints.
