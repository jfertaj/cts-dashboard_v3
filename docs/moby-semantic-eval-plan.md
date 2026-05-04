# Moby Semantic Resolver — Bulk Eval Plan

Context: Cohere semantic resolver Option A is wired but gated behind
`MOBY_SEMANTIC_FIELD_RESOLVER=1`. A/B test on 6 queries (2026-05-04) showed
1/6 win, several regressions, and +37% latency. Before flipping default ON in
prod, run a 30+ query bulk eval to quantify pass-rate, latency, and the
suppression ratio of the score gate.

## Query coverage (≥30, balanced)
- EN simple counts (5): "How many sites are in Spain?", "List CTS in France",
  "How many active sites?", "Sites in Italy?", "How many opps in Germany?"
- EN with synonyms / paraphrase (5): "pediatric T1D follow-up for kids under 18",
  "centres recruiting Stage 2", "HLA typed individuals", "ND consenters with islet
  autoantibodies", "first-degree relatives followed"
- ES (5): "¿Cuántos centros hay en España?", "sitios pediátricos T1D menores 18",
  "¿qué centros tienen ND seguidos en estadio 1?", "actividades por país",
  "coordinadores de estudio en Italia"
- IT (4): "quanti siti in Italia?", "centri pediatrici T1D under 18",
  "stadio 2 individui seguiti", "coordinatori per nazione"
- FR (4): "combien de sites en France?", "suivi pédiatrique T1D moins de 18 ans",
  "centres avec typage HLA", "activités par pays"
- DE (4): "Wie viele Standorte in Deutschland?", "pädiatrische T1D Nachsorge unter 18",
  "Stadium 1 verfolgte Personen", "Studienkoordinatoren pro Land"
- Ambiguous / multi-intent (3): "show me everything in Spain about kids",
  "pediatric centres with HLA and Stage 2", "compare ND and T1D by country"

## Metrics
- **Pass-rate**: answer matches a hand-curated ground truth (rows count, key fields).
  Target: ≥90% with hint ON, no regression vs hint OFF.
- **Latency**: p50/p95 of `[moby-semantic]` block + total chat round-trip.
  Target: hint adds <10% to p50.
- **Hint suppression ratio**: % of queries where `filtered_to=0/N` after the
  score≥0.5 gate. Healthy band: 30–60% (proves the gate is doing real work).
- **Cache hit rate**: % of `cache=hit` after warmup. Target: ≥40% on a repeat-skewed
  workload.

## Command
```bash
MOBY_SEMANTIC_FIELD_RESOLVER=1 SF_SESSION_COOKIE="..." \
  python scripts/run_bulk_eval.py \
  --queries docs/moby-semantic-eval-queries.json \
  --out docs/moby-semantic-eval-results.json
```
(`scripts/run_bulk_eval.py` and the queries file do not exist yet — author
both as part of executing this plan.)
