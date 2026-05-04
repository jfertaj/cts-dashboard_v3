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
