# INDEX — Memory Palace

> Mapa del cuaderno. Toda entrada nueva se referencia aquí en una línea.

## Archivos activos
- [context](context.md) — misión y alcance
- [decisions](decisions.md) — decisiones de arquitectura
- [research](research.md) — investigación en curso
- [code-notes](code-notes.md) — decisiones de código (si aplica)
- [reviews](reviews.md) — hallazgos de revisión (si aplica)
- [blockers](blockers.md) — unknowns activos
- [glossary](glossary.md) — terminología del proyecto

## Últimas entradas
<!-- formato: - [YYYY-MM-DD] [agente] → archivo#anchor — título -->
- [2026-04-28] [coder] → code-notes.md — Moby planner age-vs-threshold (fix ND<18 filtro espurio + agregación short-circuit)
- [2026-05-04] [coder] → code-notes.md — POC resolver semántico de campos (Cohere Embed v3 + Rerank 3.5 vía Bedrock, gated por MOBY_SEMANTIC_FIELD_RESOLVER)
- [2026-05-04] [coder] → code-notes.md — Semantic resolver: dedupe post-rerank por canonical key (strip "sf.")
- [2026-07-13] [coder] → code-notes.md — Vitest en el frontend + `readDataCell` extraído a `lib/rowAccess` (empty-string = ausente; el build NO hace typecheck)
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--5550a881689f4c--task-1 — Task 1 (5550a88..1689f4c) — approved-with-issues (verbatim OK; cobertura de tests floja en las ramas de fallback)
- [2026-07-13] [coder] → code-notes.md — `readDataCell`: L40-41 son código muerto, `0` cuenta como valor, y cobertura de rama != cobertura de línea (mutation check)
