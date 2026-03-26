# INCIDENT: Google Maps Distance Matrix cost spike (~$930 in March 2026)

**Status: ACTIVE — development paused until remediation is complete**
**Date discovered: 2026-03-25**
**Severity: HIGH (financial + security)**

---

## What happened

Between March 10–24 2026 the Google Maps Distance Matrix API billed ~186,000 elements against the project's single API key. At $5/1,000 elements this is ~$930, minus the $200/month free credit = **~$730 unexpected cost**.

The worst day was March 24: **119,366 billable elements** in a 3-hour window (15:00–17:50 UTC).

## Root cause

Two backend endpoints in `salesforce_explorer.py` generate an **N × M element explosion** when called with broad parameters:

| Endpoint | Pattern | Worst case |
|----------|---------|------------|
| `POST /api/explorer/search/within-drive-km` | 1 base × all candidate destinations | 1 × 200+ = 200+ elements per call |
| `POST /api/explorer/search/nearby-multi` | N bases × M candidates **in parallel** | 10 bases × 200 dests = 2,000+ elements per call |

Contributing factors:
1. **Process-local cache** (`_dm_cache` at line ~3936) — with 4 uvicorn workers, the same route is fetched up to 4× before all workers warm up.
2. **1-hour TTL** — center coordinates don't change hourly; cached results expire too fast.
3. **No element cap** — before the guard rail (see below), there was nothing preventing a single request from generating thousands of billable elements.
4. **Moby (AI chat)** also calls Distance Matrix via `ai_chat.py:8026` using a sync wrapper of the same logic.

### Evidence (from Google Cloud Monitoring API)

```
Billable elements by day (March 2026):
  Mar 10:       234
  Mar 11:       287
  Mar 12:    28,346
  Mar 17:    19,227
  Mar 20:    18,324
  Mar 24:   119,366   ← 5,914 requests in 3 hours
  TOTAL:   ~186,000   ← ~$730 after free credit
```

Credential confirmed: `apikey:AIzaSyBlizrJ8uQfzh4qYKiUPZ9xX-BL5ddzlO0`
Source confirmed via CloudWatch ECS logs: requests originate from the cts-dashboard backend container, each with 25 destinations spanning all of Europe.

## Guard rail applied (2026-03-25)

A hard cap was added to prevent runaway calls. **This is a stop-gap, not the fix.**

**File:** `backend/app/routers/salesforce_explorer.py`

1. **Constant** (line ~63):
   ```python
   MAX_DISTANCE_MATRIX_ELEMENTS = int(os.getenv("MAX_DISTANCE_MATRIX_ELEMENTS", "2000"))
   ```

2. **Check in `within-drive-km`** (before `_drive_km_matrix` call):
   - Computes `1 × len(dests)`
   - If > limit → HTTP 422 with descriptive message + WARNING log

3. **Check in `nearby-multi`** (before `asyncio.gather`):
   - Computes `len(base_coords) × len(dests)`
   - If > limit → HTTP 422 with descriptive message + WARNING log

This means some legitimate wide searches will now fail with 422. That is intentional until proper fixes are in place.

## What still needs to be done

### P0 — Must fix before resuming normal development

- [ ] **Shared cache for Distance Matrix results** — replace the per-worker `_dm_cache` dict with a shared store (file-based JSON, SQLite, or Redis). This alone would reduce API calls by ~4× in production (4 workers). The cache key is already a good hash (`dm_km:{origin}->{md5(destinations)}`).

- [ ] **Increase TTL from 1h to 24–48h** — center/account coordinates change at most weekly. A 24h TTL is safe and reduces repeat calls dramatically. Change the `ttl=3600` in `_cache_set(ck, mx, ttl=3600)` at line ~4012.

- [x] **Apply haversine pre-filter + top-N cap in both endpoints** (2026-03-25) — Both `within-drive-km` and `nearby-multi` now sort candidates by haversine (straight-line) distance and keep only the top `DM_CANDIDATES_PER_BASE` (default 20) nearest candidates per base before calling Distance Matrix. `within-drive-km` previously had NO haversine pre-filter and sent ALL candidates. `nearby-multi` had a broad 2× max_km cut but no top-N cap. New constant: `DM_CANDIDATES_PER_BASE = int(os.getenv("DM_CANDIDATES_PER_BASE", "20"))`. Logs now show: total_candidates → after_prefilter → elements sent to DM.

- [ ] **Cap Moby's Distance Matrix usage** — `ai_chat.py:8026` has a sync Distance Matrix wrapper that bypasses the guard rail. Apply the same element cap there.

- [ ] **Set Google Cloud quota** — in Google Cloud Console → APIs & Services → Distance Matrix API → Quotas, set a daily element limit (suggested: 5,000) as a billing safety net independent of code.

### P1 — Should fix soon

- [ ] **Separate API keys** — currently one key (`AIzaSyBlizrJ8uQfzh4qYKiUPZ9xX-BL5ddzlO0`) is shared across frontend (Vite `VITE_GOOGLE_MAPS_API_KEY`), backend (ECS Secrets Manager), and the Amplify Navigator apps. Create separate keys:
  - `frontend-maps-key`: HTTP Referrer restricted, Maps JavaScript API only
  - `backend-server-key`: IP restricted, Geocoding + Distance Matrix only

- [ ] **Restrict the API key** — the current key has **no application restrictions** (screenshot confirmed: Application restrictions = "None") and **31 APIs enabled**. At minimum, add HTTP Referrer restrictions and reduce to only the APIs actually used (Maps JavaScript, Geocoding, Distance Matrix).

- [ ] **Rotate the API key** — the key is exposed client-side via `VITE_GOOGLE_MAPS_API_KEY` in the compiled JS bundle (`frontend/dist/assets/index-CqnCanAy.js`). Anyone who visited the dashboard could have extracted it. After creating new restricted keys, delete the old one.

- [ ] **Set up billing alerts** — Google Cloud Billing → Budgets → create a $50/month budget with alerts at 50%, 80%, 100%.

### P2 — Nice to have

- [ ] Add observability: log element count per Distance Matrix call, create a CloudWatch metric for total daily elements.
- [ ] Consider persistent DB cache (PostgreSQL table) for Distance Matrix results with weekly TTL.
- [ ] Evaluate whether `within-drive-km` and `nearby-multi` should cap `max_km` server-side (e.g., max 500km) regardless of what the client sends.

## Key files

| File | What it does | Lines |
|------|-------------|-------|
| `backend/app/routers/salesforce_explorer.py` | `_drive_km_matrix()` — core Distance Matrix caller | ~3954–4032 |
| same file | `within-drive-km` endpoint | ~4034–4290 |
| same file | `nearby-multi` endpoint | ~4800–5050 |
| same file | `_dm_cache` — per-worker in-memory cache | ~3936–3952 |
| same file | `MAX_DISTANCE_MATRIX_ELEMENTS` guard rail | ~63 |
| `backend/app/routers/ai_chat.py` | Moby's sync Distance Matrix wrapper | ~8001–8037 |
| `frontend/src/components/MapView.tsx` | Frontend Google Maps (uses `VITE_GOOGLE_MAPS_API_KEY`) | |

## Pricing reference

| API | Cost | Unit |
|-----|------|------|
| Distance Matrix (basic) | $5.00 | per 1,000 elements |
| Distance Matrix (advanced, with traffic) | $10.00 | per 1,000 elements |
| Geocoding | $5.00 | per 1,000 requests |
| Maps JavaScript (map loads) | $7.00 | per 1,000 loads |
| Free monthly credit | $200.00 | per billing account |

One element = one origin–destination pair. A call with 1 origin and 25 destinations = 25 elements.
