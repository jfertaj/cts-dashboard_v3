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
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--1689f4c4604c40--task-1-fix-cobertura-de-fallback-de-readdatacell-re-review-del-important — Task 1 fix (1689f4c..4604c40) — approved
- [2026-07-13] [coder] → code-notes.md — Task 2 `chartAggregation`: ausente = `null` nunca `0` (sites cuenta reportadores), orden comas-antes-de-Number, y mutation check en ficheros nuevos necesita copia pristine
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--4604c401d19965--task-2-chartaggregation-parseo-de-métrica-cobertura-agrupación-por-país — Task 2 (4604c40..1d19965) — approved-with-issues (regla ausente≠cero correcta y bien testeada; `sites` = reportadores necesita JSDoc; `extra.AssignmentsCount` viene con 0 por defecto del backend)
- [2026-07-13] [coder] → code-notes.md — Task 2 fix: JSDoc de `CountryBucket.sites` + test de la rama `"(sin país)"` (commit f7c1062)
- [2026-07-13] [coder] → code-notes.md — Task 3 `chartAggregation`: el `?? 0` del `funnel` es la puerta trasera del bug ausente≠cero, y `distribution(rows, COUNT_METRIC)` no significa nada (commit 381eb8c)
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--9b0c437f3084cb--task-3-chartaggregation-ranking-topnbottomn-pareto-y-embudo — Task 3 (9b0c437..f3084cb) — approved-with-issues (bottomN y funnel blindados y probados; falta el test del cero legítimo en funnel, y el `?? 0` del sum es código muerto que hay que borrar)
- [2026-07-13] [coder] → code-notes.md — Task 3 fix: un guard `!== null` no está testeado hasta que un caso tiene un cero legítimo (+ extraer dentro del filtro mata el `?? 0`) — commits 1e6885f, fc18954
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--f3084cba3671cc--task-3-fix-de-review-cero-legítimo-en-el-embudo--muerte-del--0 — Task 3 fix (f3084cb..a3671cc) — approved
- [2026-07-13] [coder] → code-notes.md#2026-07-13-coder--task-4-metricpicker-el-prop-contract-no-ata-coverage-a-metrickey-y-funnel-no-tiene-hueco-para-un-selector — Task 4 `MetricPicker` (commit 5e2138e) — componente compartido creado, sin fixes tras loop de revisión
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--a3671cc5e2138e--task-4-metricpicker-selector-de-métrica--línea-de-cobertura — Task 4 (a3671cc..5e2138e) — approved-with-issues (verbatim del brief, coverage honesta en los 3 estados; fallback de `label` puede filtrar keys técnicas si Tasks 6-8 no sincronizan `metricKey`/`options`/`coverage`)
- [2026-07-13] [coder] → code-notes.md#2026-07-13-coder--tasks-6-9-las-4-vistas-del-chart-modal-cierran-los-agujeros-que-metricpicker-deja-abiertos-metrickey--options-coverage-del-mismo-metrickey — Tasks 6-9 (a12b1bb, 908177e, aedd0dd, f36b8a3) — CountriesView / RankingView / DistributionView / FunnelView
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--5e2138ef311710--tasks-6-9-las-cuatro-vistas-del-chart-modal-countries--ranking--distribution--funnel — Tasks 6-9 (5e2138e..f311710) — Spec ✅ x4 · quality approved-with-issues (los 4 hazards verificados y cerrados; falta distinguir "0 filas" de "0 reportadores" y no hay tests de componente)
- [2026-07-13] [coder] → code-notes.md#2026-07-13-coder--task-5-el-chartmodal-viejo-lo-importan-dos-páginas-y-el-marco-del-modal-se-llevaba-puesta-la-altura-del-chart — Task 5 (contenedor con pestañas + `CustomView`) — OJO: `ChatView.tsx` también importa el `ChartModal` viejo
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--f311710bba7063--task-5-contenedor-con-pestañas--customview — Task 5 (bba7063) — Spec ✅ · quality approved-with-issues (move fiel y guard de 0 filas correcto; CRITICAL: el panel sin `max-h`/`overflow` atrapa al usuario con Ranking N=30; ChatView rompe el build si Task 10 borra el fichero viejo)
- [2026-07-13] [coder] → code-notes.md#2026-07-13-coder--task-5-fix-de-revisión-sacar-overflow-y-auto-del-panel-y-ponerlo-solo-en-el-contenido-no-en-todo-el-flex-col — Task 5 fix de revisión (commit 58bd022) — panel acotado a max-h-[90vh] con header/pestañas shrink-0 + overflow-y-auto solo en contenido, Escape-key handler, max-w-6xl restaurado, padding/altura de CustomView alineados con siblings
- [2026-07-13] [revisor] → reviews.md#2026-07-13-revisor--0d1f1d3cfe75c8-rama-featexplorer-table-scroll-resize-29-commits--review-final-de-rama-antes-de-merge — Rama completa `feat/explorer-table-scroll-resize` (0d1f1d3..cfe75c8) — approved-with-issues (regla ausente≠cero sólida en las 4 vistas nuevas; Personalizado sigue rellenando con cero por spec; z-index del modal degradado 11000→50)
- [2026-07-13] [coder] → code-notes.md#2026-07-13-coder--fixes-finales-de-rama-el-z-index-de-un-modal-es-un-contrato-con-los-overlays-vecinos-y-el-estado-local-de-una-vista-que-se-desmonta-es-una-fuga-silenciosa — Fixes finales de rama (9119403, 838056e, 9f04c1d, c56dc41) — z-[11000] restaurado, aviso ámbar en Personalizado, `legendMax` subido a los dueños, tooltip de Account Name; +4 tests E2E (120 specs: 116 pass / 4 skip)
- [2026-07-14] [revisor] → reviews.md#2026-07-14-revisor--c56dc41c6c4b33--el-personalizado-deja-de-contar-como-cero-al-que-no-reporta-row-wise--pie — PR #12 (c56dc41..c6c4b33) — approved (ausente≠cero cerrado en las 3 rutas del constructor; cero legítimo conservado, verificado con 2 mutaciones; solo Minors)
- [2026-07-14] [coder] → code-notes.md#2026-07-14-coder--dos-parsers-de-métrica-que-divergen-a-propósito-y-el-e2e-corre-contra-un-dist-precompilado-el-mutation-check-miente-sin-npm-run-build — Fix de negativos en el constructor genérico (7e19ee4, 9800057, d250657) — `toDatasetValue` propio (el negativo es un valor), el pie excluye+ANUNCIA la porción negativa, cero legítimo pineado en el fixture E2E
- [2026-07-14] [coder] → code-notes.md#2026-07-14-coder--el-array-de-respaldo-fullrows-no-es-lo-que-la-tabla-enseña-tanstack-filtra-en-cliente-encima — Los gráficos agregan las filas filtradas en cliente (`getFilteredRowModel`), no `fullRows` — badge, 4 vistas y constructor alineados con la tabla (+CHART-8/CHART-9)
- [2026-07-14] [coder] → code-notes.md#2026-07-14-coder--rowdata-sólo-trae-lo-que-alguien-pidió-la-búsqueda-en-bloque-devuelve-las-métricas-a-null-y-el-fill-perezoso-es-su-única-fuente — `row.data` sólo trae las columnas VISIBLES: el chart anunciaba "0 de 215 reportan" (verdad: 81) porque nadie pedía la métrica al servidor — `lib/fillColumns.ts` + fixture E2E con la forma del backend real (3648584, 056e6ec)
