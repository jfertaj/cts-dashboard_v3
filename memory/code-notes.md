### [2026-07-13] [coder] — Task 3 `chartAggregation`: el `?? 0` del `funnel` es la puerta trasera del bug ausente≠cero, y `distribution(rows, COUNT_METRIC)` no significa nada

**Contexto**: Task 3 del rediseño del chart modal — `topN` / `bottomN` / `distribution` / `funnel` en `frontend/src/lib/chartAggregation.ts` (commit 381eb8c).

**Gotcha/Patrón**:
1. **`funnel()` sólo suma los centros que reportan las TRES métricas** (`SCREENED` && `STAGE1` && `STAGE2`). Un centro que reporta cribados pero no Stage 1 inventaría una caída del embudo que no existe. Dentro de `sum()` hay un `?? 0` que HOY es inalcanzable (el `filter` ya garantiza no-null) — pero es exactamente la puerta trasera del bug: si alguien afloja el filtro (p.ej. `&&` → `||`, o "basta con que reporte cribados"), el `?? 0` rellena en silencio las etapas que faltan con ceros y el embudo miente sin fallar. Mutation check: cambiar `&&` por `||` deja 2 tests en rojo. Si tocas el filtro, quita el `?? 0` a la vez.
2. **`bottomN` es el otro punto caliente**: el centro que no reporta NO puede aparecer como "el peor". Ambas funciones de ranking pasan por el mismo choke point `sitesWithData()` (que descarta los `null` de `valueOf`); nunca mapees `rows` directo con `?? 0`. Mutante probado: `bottomN` sobre `rows.map(... ?? 0)` → 1 test en rojo.
3. **`distribution(rows, COUNT_METRIC)` es un sinsentido**: `valueOf` devuelve `1` para toda fila con `COUNT_METRIC`, así que salen N barras de valor 1, `missingSites: 0` y un Pareto plano. El Pareto sólo tiene sentido con una métrica real (screened / stage1 / stage2 / assignments). Las vistas de Tasks 6-8 no deben ofrecer "Nº de centros" como métrica en la vista de distribución.
4. `cumulativePct` va con `Math.round`, así que son enteros: el último SIEMPRE es 100 exacto (running == total), salvo el caso `total === 0` que devuelve `0` para evitar el `0/0 = NaN`.

**Por qué importa**: la mayoría de los 215 centros no reportan estas métricas. Los dos mutantes de arriba producen gráficos que se pintan perfectos y mienten — no revientan, no logean, sólo dan un número plausible y falso.

**Dónde aplicar**: `frontend/src/lib/chartAggregation.ts` y toda vista del chart modal que lo consuma (Tasks 6/7/8).

---

### [2026-07-13] [coder] — Task 2 fix: JSDoc de `CountryBucket.sites` + test de la rama `"(sin país)"`

**Contexto**: fixes puntuales pedidos por la review de Task 2 (`memory/reviews.md` — approved-with-issues), sobre `frontend/src/lib/chartAggregation.ts` / `chartAggregation.test.ts`. Sin renames, sin refactors.

**Gotcha/Patrón**: el mutation check de la entrada de abajo (punto 3) decía usar copia pristine porque el fichero era NUEVO y sin trackear. Aquí el fichero ya estaba commiteado y limpio (`git status --porcelain` vacío) — con eso `git checkout --` habría sido seguro. Aun así usé copia pristine (`cp` a scratchpad + `diff` tras restaurar) por venir explícitamente pedido en la tarea; es la opción más barata y nunca falla en silencio, así que sirve como default sin tener que verificar primero el estado de git.

**Por qué importa**: confirma que la regla "sites = reportadores, no total del país" ahora está documentada en el tipo (antes solo vivía en code-notes/reviews, invisible para quien solo lee el código), y cierra el mutante superviviente de `"(sin país)"` — 12 tests verdes en el fichero (antes 11), 32/32 en todo el frontend.

**Dónde aplicar**: `frontend/src/lib/chartAggregation.ts` — cualquier vista de Tasks 3/6/7/8 que lea `CountryBucket.sites` como "total de centros del país" está leyendo mal el campo; ahora el JSDoc lo dice explícitamente.

---

### [2026-07-13] [coder] — `chartAggregation`: ausente = `null` (nunca `0`), y el mutation check sobre ficheros NUEVOS necesita copia pristine (no `git checkout --`)

**Contexto**: Task 2 del rediseño del chart modal — `frontend/src/lib/chartAggregation.ts` (`toMetricValue` / `coverageFor` / `groupByCountry`).

**Gotcha/Patrón**:
1. **La regla del módulo: ausente, vacío, no numérico y NEGATIVO → `null`; el `0` reportado se conserva.** Una fila `null` no contribuye NADA: ni al `value` (suma) ni al `sites` (contador) de su `CountryBucket`, y un país donde nadie reporta desaparece del array. La mayoría de los 215 centros no reportan `screened`/`stage1`/`stage2`; si el hueco se plegara como `0`, un centro que nunca reportó sería indistinguible de uno que reclutó a cero — el gráfico saldría bonito y mentiría. Ojo al tocar `groupByCountry`: `bucket.sites` cuenta **centros que reportan**, no centros del país; para el total de centros hay que usar `COUNT_METRIC` (que corta en `valueOf` devolviendo `1` siempre) o `coverageFor(...).total`.
2. **`toMetricValue` strippea comas ANTES de `Number()`** (`"1,240"` → `1240`). Es deliberado: SF devuelve enteros formateados como string. Consecuencia: `Number("")` es `0`, así que la guarda `text === ""` tiene que ir **antes** del `Number()`, no después — invertir ese orden convierte el vacío en un `0` legítimo y rompe la regla entera en silencio.
3. **Mutation check sobre un fichero aún NO trackeado: `git checkout -- <f>` falla con `pathspec ... did not match any file(s)`, y el script sigue.** Me pasó: los mutantes se acumularon uno encima de otro y las 7 "muertes" que leí eran ruido de un fichero corrupto. Para ficheros nuevos: `cp` a una copia pristine y restaurar desde ella, verificando con `diff -q` al final. Añadir siempre una línea de BASELINE al principio y otra al final del script — si la final no vuelve a verde, el experimento no vale nada.

**Por qué importa**: los tests de este módulo son el único gate de la regla "ausente ≠ cero" (recordatorio: en este frontend ni `npm test` ni `npm run build` hacen typecheck). 9 mutantes probados, 9 muertos — pero sólo tras arreglar el método, que en la primera pasada daba falsos positivos.

**Dónde aplicar**: `frontend/src/lib/chartAggregation.ts` (1-2) y todas las vistas del chart modal que lo consuman (Tasks 3/6/7/8); el punto 3 vale para cualquier mutation check del repo sobre ficheros nuevos.

---

### [2026-07-13] [coder] — `readDataCell`: dos líneas muertas, `0` es valor, y la capa 2 sólo se distingue con bases con punto

**Contexto**: `frontend/src/lib/rowAccess.ts`, al ampliar `rowAccess.test.ts` para cubrir las ramas de fallback (hallazgo Important de la review de Task 1).

**Gotcha/Patrón**:
1. **Las líneas 40-41 son inalcanzables.** `if (key === 'sf.Account.Name')` / `'sf.Account.Id'` nunca se ejecutan: si `key` es `sf.Account.Name`, entonces `base` es `Account.Name` y `kb` es `account.name`, así que la L38 ya ha hecho `return`. Verificado por mutación: borrarlas deja los 21 tests en verde. Borrarlas es seguro; no lo hice porque la task prohibía tocar el fichero.
2. **El `0` numérico SÍ se devuelve como valor.** La guarda es `String(v).trim() !== ""`, y `String(0)` es `"0"`. Sólo `undefined`/`null`/`""`/whitespace cuentan como ausentes y siguen bajando por la cadena.
3. **La capa 2 (strip de `sf.`) es indistinguible de `k3` salvo con bases que conserven puntos.** Con `sf.Foo__c`, `base` y `k3` son la misma cadena (`Foo__c`), así que `k3` rescata la lectura si borras la capa 2. Sólo `sf.Account.Name` (base `Account.Name` ≠ k3 `Account_Name`) aísla la capa 2.
4. **Patrón: en tests de caracterización, cobertura de línea ≠ cobertura de rama.** La forma de probar que un test pin-ea una rama es borrar la rama y ver el test en rojo (`sed` + `npm test` + `git checkout --`). La primera pasada dejó 2 supervivientes con los tests "obvios" ya escritos.

**Por qué importa**: sin el mutation check, dos ramas de fallback quedaban borrables en silencio por un refactor futuro — que es justo lo que este módulo existe para evitar (el orden de fallbacks es de lo que depende el Explorer en vivo).

**Dónde aplicar**: `frontend/src/lib/rowAccess.ts` (1-3); el punto 4 vale para cualquier test de caracterización del repo.

---

### [2026-07-13] [coder] — Vitest en el frontend + `readDataCell` extraído a `lib/rowAccess`

**Contexto**: Task 1 del rediseño del chart modal. `frontend/` no tenía runner de tests unitarios (sólo Playwright E2E), y `readDataCell` vivía como función de módulo NO exportada en `ExplorerView.tsx:912`.

**Gotcha/Patrón**:
1. **Ni `npm test` ni `npm run build` hacen typecheck en este frontend.** `vitest run` transpila con esbuild (tira los tipos) y `vite build` no invoca `tsc`. Un error de tipos **no rompe ningún gate**. Para saber si has metido una regresión de tipos hay que correr `npx tsc --noEmit` a mano y compararlo contra el baseline — que **hoy tiene 12 errores pre-existentes** (6 en `ExplorerView.tsx`, 2 en `ChatView.tsx`, 1 en Header/MapView/MemberMapView/SalesforceLinker). Truco: `git stash` → contar → `git stash pop` → contar → comparar.
2. **`readDataCell` trata la cadena vacía como AUSENTE, no como valor.** Cada rama hace `!== undefined && !== null && String(v).trim() !== ""` y, si falla, **sigue buscando** en el siguiente fallback (clave exacta → sin prefijo `sf.` → variantes con `_` → campos planos `account_name`/`account_id`/`country`/`city`) y acaba en `undefined`. Ese `undefined` final es deliberado: TanStack Table lo necesita para aplicar `sortUndefined: 'last'`.
3. Vitest configurado con `include: ["src/**/*.test.ts"]` + `environment: "node"` — **no coge `.test.tsx`**. Para testear componentes habrá que ampliar el glob y pasar a `jsdom`.

**Por qué importa**: si alguien "limpia" `readDataCell` y convierte el empty-string en un valor válido (o devuelve `null`/`0` en vez de `undefined`), rompe a la vez el ordenado de la tabla, el filtrado y los charts del Explorer — y **ningún test ni el build lo van a pescar**. La regla global del rediseño ("un valor ausente es `null`, jamás `0`") se apoya justo en este comportamiento.

**Dónde aplicar**: `frontend/src/lib/rowAccess.ts` (la función), todo el frontend (el punto 1 sobre los gates de tipos).

---

### [2026-05-04] [coder] — Tightening Opción A: top_n=4 + score gate + embed cache

A/B test contra prod (6 queries reales) reveló: 1/6 win, regresiones por hint ruidoso en queries triviales (Q1 EN: top-K con `_how_many_beds`), supresión de un clarify legítimo (Q2), y +37% latencia. Tres ajustes:

1. **`_semantic_field_hints`**: `top_n=8 → 4`, filtro post-rerank `score≥0.5`. Si tras el filtro `hits` queda vacío → `return ""` (no se inyecta bloque). Log enriquecido: `[moby-semantic] q=… top=[…] filtered_to=M/N cache=hit|miss latency=Xms`.
2. **Cache de embeddings** (`moby_semantic_resolver.py`): `OrderedDict` LRU module-level, key = `sha256(query)[:16]`, TTL 1h, máx 256 entradas. Solo cachea el vector de query (rerank no — depende del par query+candidatos). `SemanticFieldResolver.last_cache_status` expone `"hit"|"miss"` para que el caller logue. Smoke local: cold 2609ms → cached 175ms (sólo rerank).
3. **Ajuste 2 (skip si planner determinista captura)**: ya implícito — `_semantic_field_hints` se llama en L9401 de `chat_api`, downstream del `if planned: return planned` en L8361. El path Claude es el único que construye el hint. Sin cambio estructural.

Tests: 1 nuevo (`test_resolve_caches_query_embedding`) — 13/13 verde. Plan de bulk eval (≥30 queries multi-idioma) en `docs/moby-semantic-eval-plan.md` para evaluar antes de flip a default ON. NO push, NO deploy.

---

### [2026-05-04] [coder] — Opción A: semantic field hints inyectados en system prompt (Moby)

El POC original enganchaba `_semantic_top_matches` a `_top_matches` en `ai_chat.py`, pero `_top_matches` **no está en la ruta del agentic loop con tool_use** (Claude resuelve campos directamente desde el SYSTEM_PROMPT y TOOLS_SPEC). Cero valor end-to-end. Migrado a **Opción A**: helpers `_semantic_field_hints(user_msg)` + `_format_hints_block(hits)` (≤30 líneas cada uno) que llaman al resolver con `top_n=8`, formatean un bloque markdown ("## Relevant fields (semantic hint, top-K=8)") y se appendean a `msgs[]` justo después del KNOWLEDGE INDEX en `chat_api`. Loggea siempre `[moby-semantic] q=… top=[…] latency=…ms`. Cualquier excepción → `""` (no regresión). La rama `_semantic_top_matches` dentro de `_top_matches` queda **comentada** (no borrada) para evitar la falsa señal anterior. Tests: `backend/tests/test_semantic_hints.py` (7 nuevos) + actualizaciones en `test_semantic_resolver.py` (12/12 verde). Smoke local con flag on confirma logs por consulta y campos top-K relevantes para ES/IT/FR/EN; latencia resolver ~300ms post-warmup.

---

### [2026-05-04] [coder] — Fix imports `backend.app.*` → `app.*` (prod 500 en Moby)

`ai_chat.py` y `moby_tools.py` usaban `from backend.app.routers.X import Y`. En el contenedor (WORKDIR `/app`, sys.path no incluye el padre) eso reventaba con `ModuleNotFoundError: No module named 'backend'` y tiraba Moby a 500. En local funcionaba por casualidad (uvicorn arranca desde la raíz del repo). Patrón aplicado: imports absolutos `from app.X import Y` (consistente con el resto del codebase: `main.py`, `members_explorer.py`, `salesforce_extras.py`, etc.). Reemplazo regex masivo en 5 archivos (~80 líneas). Tests preexistentes siguen igual (3 fallos en `test_moby_planner.py` no relacionados, verificados con `git stash`).

---

### [2026-05-04] [coder] — Semantic resolver: dedupe por canonical key

**Contexto.** El POC del Cohere semantic field resolver devolvía la misma key 2-3 veces en el top-N (ej. `C_Number_of_new_T1D_diagnosed_U_18__c` aparecía como entrada curated y como alias manual sf.-prefixed). Eso contamina el hint que recibe el caller (`_semantic_top_matches` en `ai_chat.py`).

**Fix** (`backend/app/moby_semantic_resolver.py`):
- Dedupe **post-rerank** dentro de `resolve()`. El builder mantiene aliases duplicados a propósito (mejora recall del embed) — no se toca.
- Helper `_canon_key()` strippea el prefijo `sf.` para canonicalizar (el caller normaliza ambas formas igual, así que cuentan como la misma key).
- Helper `_dedupe_by_key()` agrupa por canonical key, conserva el max score, ordena desc, recorta a `top_n`.
- Sobre-pido al rerank `max(top_n*4, top_n)` resultados para que post-dedupe queden top_n keys únicas.

**Test** (`backend/tests/test_semantic_resolver.py::test_resolve_dedupes_by_canonical_key`): mockea rerank con duplicados (`X` y `sf.X`), verifica colapso, max score conservado, orden desc.

**Smoke test real (Bedrock)**: 4 queries probadas (EN + ES cross-lingual), ya sin duplicados en top-3.

---

### [2026-04-28] [coder] — Moby planner: separar "age qualifier" de "count threshold" en ND

**Contexto.** El usuario pidió "total number of newly diagnosed <18 in all Italian clinical sites" y Moby devolvía 35 sitios con suma 756 en vez de 70 sitios y total 1.135. Bug: el regex de `_extract_filters()` capturaba `<18` como operador numérico aplicado al campo hardcodeado `_O_18__c` (ND ≥18), generando un filtro espurio `ND≥18 < 18`.

**Fix aplicado** (`backend/app/routers/moby_planner.py`):
- Dos regex `_AGE_UNDER_18` / `_AGE_OVER_18` distinguen el calificador de edad.
- `_extract_filters()` strippea la frase de edad antes de buscar un threshold numérico, eligiendo `_U_18__c` / `_O_18__c` según la edad.
- Sólo se añade filtro ND cuando hay un threshold explícito ("more than N", "> N", etc.); con sólo edad → no se filtra (preserva el scope de país).
- `parse_query_plan()` añade la columna ND-age a `requested_columns` para subir la confianza al threshold de short-circuit (0.80).

**Fix complementario** (`backend/app/routers/ai_chat.py`):
- Nuevo helper `_short_circuit_aggregation_lines(user_text, rows)` que detecta intent de agregación (`total / how many / sum / count`) y añade líneas con sumas por columna numérica relevante a la respuesta del short-circuit.
- Labels HTML-escapados (`&lt;18`) — sin eso, el navegador come `<18</strong>` como tag malformado.

**Trampas / gotchas a recordar**
- Cuidado al meter `<` o `>` en labels de respuesta HTML del backend: hay que `&lt;` / `&gt;` o el cliente los interpreta como apertura de tag.
- `_score_confidence()` necesita `>= 0.80` para short-circuit; "país + table_query bonus + sin unresolved" suma 0.75. Añadir una `requested_columns` aporta el +0.05 que faltaba.
- Cuando `_AGE_UNDER_18` / `_AGE_OVER_18` se actualicen, recordar que `pediatric|child|kid|niño|menores de 18` también caen en under-18.

**Tests añadidos** (`backend/tests/test_moby_planner.py`)
- `test_nd_under_18_no_spurious_count_filter`
- `test_nd_under_18_age_no_threshold_skips_filter`
- `test_nd_under_18_with_threshold_uses_u18_field`
- `test_nd_over_18_uses_o18_field`
- `test_nd_under_18_words_picks_u18`
- `test_nd_under_18_with_country_short_circuits` (test integral del plan + can_short_circuit)

589 tests verdes (3 fallos pharmacy pre-existentes ajenos a este fix).

**Verificación end-to-end.** Pregunta original contra backend local (con cookie de prod): respuesta `Found 70 site(s) in IT. Total Newly Diagnosed T1D <18: 1135 (across 51 of 70 sites with reported values).` — short-circuit, sin llamada a Claude, ~6.8s.

---

### [2026-05-04] [coder] — POC resolver semántico de campos (Cohere Embed v3 + Rerank 3.5 vía Bedrock)

**Contexto.** Op-1 del análisis del 2026-05-04: `_top_matches` en `ai_chat.py` hace matching léxico (intersección de tokens normalizados). Falla con consultas multilingües, sinónimos no curados, o paráfrasis ("immune typing" → no match a `HLA`).

**Implementación POC** (todo gated, fallback al léxico siempre disponible):
- `scripts/build_field_index_cohere.py` — script offline que embebe SF curated + qual aliases + manual aliases con `cohere.embed-multilingual-v3` (eu-west-1, batch 96, `input_type=search_document`) y persiste a `backend/app/cache/field_index.json` (numpy normalizado, sin FAISS).
- `backend/app/moby_semantic_resolver.py` — `SemanticFieldResolver.resolve()`: embed query → cosine top-50 numpy → rerank top-N en eu-central-1 (`cohere.rerank-v3-5:0`). Errores → `BedrockUnavailable`. Boto3 perezoso, dos sesiones (regiones distintas), credential chain por defecto (task role en prod, perfil `juan` en local).
- `_top_matches()` en `ai_chat.py` añade rama nueva al inicio: si `MOBY_SEMANTIC_FIELD_RESOLVER=1` intenta el resolver; si devuelve algo lo usa, si no cae al matcher léxico.
- Tests `backend/tests/test_semantic_resolver.py` — 5 tests con boto3 mockeado (no toca red).

**Trampas / gotchas a recordar**
- Bedrock Rerank 3.5 NO está en eu-west-1 — hay que hacer cross-region a eu-central-1. Embed v3 sí está en eu-west-1 (mismo VPC que ECS).
- Rerank devuelve `index` relativo al shortlist enviado, no al corpus global → traducir con el array de índices originales.
- El builder offline NO ejecuta site_qual / profiling_kv (DB-dependientes). Para indexarlos habrá que rebuild en runtime al arrancar el servicio o exportar desde la sesión local. POC se limita a SF + aliases estáticos.
- El payload del rerank requiere `"api_version": 2` en Bedrock; sin eso devuelve sin scores.
- `MOBY_FIELD_INDEX_PATH` permite apuntar a otro JSON en tests sin tocar el de prod.

**Para activar el flag en local.** `MOBY_SEMANTIC_FIELD_RESOLVER=1 AWS_PROFILE=juan bash scripts/restart_local_backend.sh`. Si el JSON no existe o Bedrock falla → log warning + fallback léxico, comportamiento idéntico a hoy.

**Pendiente.** Correr `AWS_PROFILE=juan python scripts/build_field_index_cohere.py` (cuesta ~$0.02), evaluar accuracy vs léxico con bulk eval, decidir si se persiste el índice con DB sources.

---

### [2026-07-13] [coder] — Un filtro `!== null` no está testeado hasta que un caso tiene un cero legítimo

**Contexto**: `frontend/src/lib/chartAggregation.ts`, fix de review de Task 3 (commits 1e6885f, fc18954).

**Gotcha/Patrón**: `funnel` filtraba con `valueOf(row, X) !== null` para las tres etapas, pero **los 43 tests pasaban igual si se cambiaba a un check por truthiness**. El único centro completo de la suite era `full("completo", 100, 10, 2)` — los tres valores truthy — así que `!== null` y `Boolean(...)` eran indistinguibles para los tests. En un módulo cuya regla entera es "ausente ≠ cero", la mitad "cero legítimo SÍ entra" estaba sin pinear. Regla general: **todo guard `!== null` necesita un caso de test cuyo valor sea `0`** (o `""`, o `false` — el falsy legítimo del dominio); si no, la suite no distingue el guard correcto del mutante por truthiness.

**Corolario estructural**: el `?? 0` que acompañaba al filtro (`valueOf(row, key) ?? 0` en el `sum()`) era código muerto — pero es la puerta exacta por la que el bug vuelve si alguien afloja el filtro. En vez de dejarlo con un comentario, se extraen los valores DENTRO del paso de filtrado y la colección superviviente lleva `number`s reales (`CompleteSite[]`). El `null` deja de existir en el tipo que se suma, así que **no hay `?? 0` que escribir**: la invariante la sostiene el compilador, no la disciplina del siguiente que toque el archivo. Patrón preferido en este repo sobre "filtrar y luego rellenar con un default".

**Por qué importa**: un centro que cribó a 50 personas y no siguió a ninguna (`screened=50, stage1=0, stage2=0`) desaparecía del embudo bajo el mutante — y es precisamente el centro que un gestor de ensayos quiere ver. El bug habría sido invisible: sin error, sin warning, solo un centro menos.

**Dónde aplicar**: `chartAggregation.ts` y cualquier módulo futuro con la semántica ausente≠cero. El patrón "extrae dentro del filtro para que el tipo no admita null" aplica a todo el repo.
