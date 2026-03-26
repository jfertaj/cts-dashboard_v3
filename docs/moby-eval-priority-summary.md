# Moby Evaluation — Priority Summary

Date: 2026-03-26

## Numbers

| Category | Count | % |
|---|---|---|
| PASS_NOW | 84 | 69.4% |
| KNOWN_GAP | 34 | 28.1% |
| FUTURE_FEATURE | 2 | 1.7% |
| AMBIGUOUS | 1 | 0.8% |
| **Total** | **121** | |
| **Benchmark Core** | **42** | 34.7% of total |

Benchmark core: 24 PASS_NOW (regression guards) + 14 KNOWN_GAP (gap tracking) + 2 FUTURE_FEATURE excluded + 2 data quality gaps.

## Top 5 Gap Groups (by priority)

| # | Group | Priority | Status | Key Example |
|---|---|---|---|---|
| 1 | **G1: Numeric thresholds ignored** | HIGH | **CLOSED** (Step 18) | SC02, FA02 fixed |
| 2 | **G2: Filter silently dropped** | HIGH | **CLOSED** (Steps 19-25) | SC08, SS02, BR03, RE04, SS05, OC03 all fixed |
| 3 | **G3: Misrouting / misparse** | HIGH | **CLOSED** (Steps 26-31) | SP05 ✓, SS06 ✓, SC07 ✓, QI02 ✓, GL05 ✓ |
| 4 | **G4: Pipeline semantic mismatch** | MEDIUM | **CLOSED** (PP04 Step 28, PP05 Step 35, SS09 Step 36) | PP04 ✓, PP05 ✓, SS09 ✓ |
| 5 | **G5: Abstract concept unmapped** | MEDIUM | **CLOSED** (QD01 Step 38; QO04+FA04 auto) | QD01 ✓, QO04 ✓, FA04 ✓ |

## Recommended Work Order

1. ~~**G1 first**~~ — **CLOSED** (Step 18, 2026-03-25). Threshold post-filter on stage handler.
2. ~~**G2 selectively**~~ — **CLOSED** (Steps 19-25, 2026-03-25). SC08, SS02, BR03, RE04, SS05, OC03, qual+profiling.
3. ~~**G3 next**~~ — **CLOSED** (Steps 26-31, 2026-03-26). SP05, SS06, SC07, QI02, GL05.
4. ~~**G4 then**~~ — **CLOSED** (Steps 28+35+36, 2026-03-26). PP04, PP05, SS09 (two-SOQL cross-RT fix).
5. ~~**G5 next**~~ — **CLOSED** (Step 38, 2026-03-26). Early diagnosis → autoantibody testing.

G6 CLOSED, G8 PARTIAL (QI03/QI04), G9 CLOSED. G10 LOW.
All HIGH+MEDIUM groups closed. Remaining: G8 partial (schema gaps), G10 (sub-country geo).

## Benchmark Core Status (2026-03-26, after G1–G7+G8partial+G9 — Step 38)

| Metric | Count |
|---|---|
| Total questions | 42 |
| PASS | **38** (90.5%) |
| EXPECTED_NO_DATA | **2** (4.8%) — BM36/OC02, BM42/DR02 |
| KNOWN_GAP (open) | **2** (4.8%) — BM28/G10, BM41/G6-remnant |
| FAIL (unexpected) | **0** |
| Regressions | 0 |
| Was-GAP now PASS | 18 (cumulative) |
| **Effective pass rate** | **40/42 (95.2%)** |

Remaining KNOWN_GAP:
- BM28 (MV02/G10) — "Northern Italy clusters" → returns all 62 Italian sites. Sub-country geo not supported.
- BM41 (QU02/G6) — "Why pharmacy empty for Berlin?" → returns 37 pharmacy sites. Diagnostic intent not supported.

Reclassified:
- BM22 (QD05) — was G8 FAIL → PASS. OGTT mapped to qual.3_8__glucose (Step 34), 40 rows.
- BM36 (OC02) — was G9 FAIL → EXPECTED_NO_DATA. Edinburgh not in org (Step 32).
- BM42 (DR02) — was G6 FAIL → EXPECTED_NO_DATA. Handler works (Step 33), 0 rows correct for data.

## Questions NOT in Benchmark Core (and why)

| Excluded IDs | Reason |
|---|---|
| SC04, SC05, SC10, CL02-CL05 | Redundant with other PASS_NOW entries that test the same handler |
| MR01, MR02, MR05, MR06 | Covered by BM08-BM10 (same handler family) |
| SP01, SP02, SP04 | Covered by BM38 (activities) and BM25 (km-of-assignment) |
| GL03, GL04 | Covered by BM23 (nearest) and BM24 (radius) |
| AA01, AA02, AA04, AA06 | Covered by BM38-BM39 (activities + no-assignment) |
| MI02, MI04, MI05, MR06b, MR08 | Covered by BM33-BM34 (members by country/role) |
| MV01, MV03-MV05, ML01-ML03 | Map/geo covered by BM23-BM28 |
| SS01, SS03, SS07, SS08 | Pipeline covered by BM12-BM14 |
| QO02, QO03, QO05, QI01 | Qual covered by BM17-BM20 |
| RE01, RE03, RE05, RT01, RT02 | Covered by other PASS in same family |
| FA03, FA05, BR01, BR02 | Covered by other PASS in same family |
| QU01 | AMBIGUOUS (placeholder "site X") |
| RE02, DR01 | FUTURE_FEATURE (CSV export, data reconciliation) |
| QD03, MC01, MC02, MC04 | Multi-condition PASS covered by BM02 and BM20 |
| QU03, DR03, QI03, QI04 | Represented by BM41-BM42 (data quality) or G8 (schema gaps) |
| SS09, QO04, FA04 | Represented by equivalent gap entries in core |

## Files Generated

- `moby-benchmark-core.yaml` — 42 questions (24 PASS + 14 GAP + 2 FUTURE + 2 DQ)
- `moby-known-gaps-priority.yaml` — 10 gap groups, 34 questions total
- `moby-eval-priority-summary.md` — this file
