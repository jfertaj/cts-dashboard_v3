# Refactor notes — Fase 0

Notas vivas de la Fase 0 del refactor de `ai_chat.py`. Se actualiza durante
las fases 1-3 según vayamos descubriendo cosas. **No es un plan**; es un
cuaderno de trampas, sospechas y propuestas.

## 1. Código eliminado en Fase 0

| Símbolo | Ubicación | Razón |
|---|---|---|
| `_semantic_top_matches` | `ai_chat.py` L480-509 | Dead — el agentic loop bypasses `_top_matches` (obs 2505). Single caller era una línea comentada. |
| Bloque comentado dentro de `_top_matches` | `ai_chat.py` L568-573 | Llamada inerte a la función borrada arriba. |

## 2. Helpers verificados como vivos (NO borrar)

Inspeccionados en Fase 0 con `grep -rn` cross-repo. Cuentas internas bajas
(=1) son engañosas: hay imports desde `moby_tools.py` o tests.

- `_first_account_id_from_table` → usado en `moby_tools.py` (2 sitios) y
  `test_tool_dispatch.py`.
- `_is_valid_sf_id` → re-exportado a `moby_tools.py`.
- `_prettify_sf_field_name`, `_introspect_profiling_kv_keys`,
  `_short_circuit_aggregation_lines`, `_detect_lang_hint`,
  `_sf_escape_value`, `_sanitize_soql_basic` → cuenta interna ≥ 2 (def +
  uso) o referenciados desde `chat_api`.

## 3. Sospechas pendientes (no tocar todavía)

Necesitan auditoría individual antes de decidir:

- `_pretty_label` (L709) — vs. `humanize_headers` de `services.sf_labels`
  ¿solapamiento? Verificar en Fase 1 antes de mover a `tables.py`.
- `_normalize_key` anidado (L1074) — definido dentro de algún helper grande;
  posible duplicado de la misma rutina en `salesforce_explorer.py`.
- Helpers anidados `_clean_countries{,_local}` (L2489, L2661, L3084) — TRES
  definiciones distintas con el mismo nombre dentro de `chat_api`. Casi
  seguro DRY-failure. Candidato a extracción común en Fase 4.

## 4. Propuestas de renombre (NO aplicar en Fase 0)

Detectados como ambigüos durante la lectura. Aplicar en Fase 4-5 cuando
los handlers se extraigan, para no inflar el diff.

| Actual | Propuesto | Motivo |
|---|---|---|
| `_top_matches` | `lexical_top_matches` | Ya no es "top matches" tras Option A; es matcher léxico puro. Documentado en su nuevo docstring, pero el nombre sigue mintiendo. |
| `_resolve_metric` | `resolve_metric_alias` | Hace alias-lookup, no resuelve métricas en general. |
| `_qplan` (cuando aparezca en `_try_planner`) | `query_plan` | "qplan" no se entiende sin contexto. |
| `_extract_structured` | `parse_inline_table_blob` | Solo aplica cuando el modelo pegó un JSON directo en texto (ya casi nunca pasa con tools). |

## 5. Bugs latentes detectados (NO arreglar en Fase 0 — scope creep)

Ninguno detectado durante esta pasada. Si aparecen durante Fase 1-3, listar
aquí con SHA del commit donde se descubre.

## 5b. Notas de Fase 1 (extracción a `app/moby/`)

Seis commits, cada uno un módulo. Pytest `624 pass / 3 pre-existing fail`
estable tras cada commit. SHAs:

- `79a4bf5` config.py (constantes)
- `822010b` schemas.py (Pydantic)
- `e092616` streaming.py (`_STREAM_Q`)
- `55a6a87` claude_client.py (`_claude_chat` + adapter)
- `c6c613b` synthesis.py (`_synthesis_fallback`, `_detect_lang_hint`)
- `e9de80c` prompt.py (`SCHEMA_HINT`, `SYSTEM_PROMPT`)

Indirecciones intencionales (que se simplificarán cuando el código
relacionado migre):

- `claude_client._claude_chat` resuelve `TOOLS_SPEC` con import diferido
  desde `app.routers.ai_chat` (TOOLS_SPEC sigue allí — Fase 2/3).
- `claude_client._claude_chat` resuelve el módulo Anthropic SDK con
  `_get_anthropic_sdk()` (atributo dinámico de `app.routers.ai_chat`)
  para que tests que hacen `@patch("app.routers.ai_chat._anthropic_sdk")`
  sigan funcionando.
- `synthesis._synthesis_fallback` resuelve `_claude_chat` igual
  (atributo dinámico) por la misma razón.

Estas tres redirecciones se documentan inline en el módulo. Cuando
TOOLS_SPEC y los tests-de-mock se reubiquen, las redirecciones
desaparecen.

`prompt.py` sí se completó en Fase 1 (no requiere knowledge index).
Los builders de prompt dinámicos que sí dependen del knowledge index
(`_semantic_field_hints`, `_format_hints_block`) siguen en `ai_chat.py`
y se moverán en Fase 3 junto con el módulo `knowledge/`.

`ai_chat.py`: 10.230 → 9.526 líneas (-704 líneas; código real movido +
shims más compactos que los originales). Ningún test cambia su path de
mock. Ningún import circular real (sólo los lazy imports documentados).

## 5c. Notas de Fase 2 (extracción a `app/moby/tools/` + `tools_spec.py`)

Cinco commits, sin tocar Fase 3+. Pytest se mantuvo en `624 pass / 3
pre-existing fail` tras cada commit. SHAs (en orden):

- `7e2952b` `tools_spec.py` (`TOOLS_SPEC` movido fuera de ai_chat;
  `claude_client.py` ya importa directamente desde `app.moby.tools_spec`,
  con lo que la indirección §5b "TOOLS_SPEC sigue allí" queda resuelta).
- `ddfdd33` `tools/salesforce.py` — `tool_salesforce_query`,
  `tool_salesforce_account_extras`, `tool_group_count_sf`,
  `tool_group_agg_sf`, `tool_time_series_sf`, `tool_sql_query_fill_sf`.
- `b552be2` `tools/explorer.py` — `tool_explorer_within_drive_km`,
  `tool_explorer_search`, `tool_nearest_filtered_sites`,
  `tool_rank_sites_by_group`.
- `7f04225` `tools/members.py` — `tool_members_search`,
  `tool_contacts_by_group`.
- `6e57916` `tools/aggregates.py` — `tool_sql_query`, `tool_group_count`,
  `tool_group_count_agg`, `tool_qual_search`.

`ai_chat.py`: 9.526 → 8.021 líneas (-1.505 líneas en Fase 2; -2.209
acumulado desde Fase 0). Cada nuevo módulo importa sus dependencias de
`ai_chat` con `from app.routers import ai_chat as _ai` *dentro* de cada
función (lazy) para evitar el ciclo: `ai_chat.py` re-exporta los
`tool_*` desde el nuevo módulo al final del fichero. Esto preserva
cualquier path de mock `@patch("app.routers.ai_chat.tool_*", ...)` que
puedan tener los tests (verificado con grep antes de cada commit).

Indirecciones que **siguen vivas** tras Fase 2 (se eliminan en Fase 3+):

- `_anthropic_sdk` indirección dinámica (§5b) — sigue justificada por
  los tests que parchean `app.routers.ai_chat._anthropic_sdk`.
- Los nuevos módulos `tools/explorer.py`, `tools/members.py`,
  `tools/aggregates.py`, `tools/salesforce.py` resuelven helpers
  privados (`_dbg`, `_pretty_label`, `_resolve_metric`, `_ok_table`,
  `_normalize_table_for_ui`, `_validate_soql`,
  `_ensure_soql_has_account_id`, `_sanitize_soql_basic`,
  `_account_extras_core`) vía `from app.routers import ai_chat as _ai`.
  Esos helpers se moverán en Fase 3 (a `validation.py`, `tables.py`,
  `metrics.py` o similar) y entonces los `_ai.<helper>` se reemplazarán
  por imports directos.
- `tool_*` no movidos en Fase 2 porque sólo aparecen como standalones
  fuera del scope explícito de la fase: `tool_render_chart`,
  `tool_manipulate_data`, `tool_explorer_set_filters`,
  `tool_study_coordinators_with_activities`, las series
  `tool_activity_*`/`tool_sites_*`/`tool_list_all_activities` y
  `tool_rank_sites`. Quedan para Fase 3 (`chart.py`, `manipulation.py`,
  `coordinators.py`, `activities.py`).

Sorpresas de la fase: ninguna. Todos los movimientos fueron textualmente
puros; los únicos cambios al cuerpo de las funciones fueron sustituir
referencias directas a helpers locales por `_ai.<helper>` y reemplazar
`tool_sql_query`/`tool_salesforce_query` por su forma `_ai.<...>`
cuando se cruzan módulos.

## 6. Cobertura de tests añadida en Fase 0

| Archivo | Tests | Cubre |
|---|---|---|
| `test_agentic_loop.py` (existente, +2) | 25 | loop normal, fast-exit, retry, dedup, timeout, multi-tool, RateLimit error. |
| `test_synthesis_fallback.py` (nuevo) | 9 | helper + trigger guard (4 ramas). |
| `test_claude_chat.py` (nuevo) | 10 | tool_choice, force_no_tools, override, thinking + beta, streaming, error. |

Total: **44 tests cubren las áreas que se moverán en Fases 1-3**. Todas con
mocks de Anthropic; ninguna requiere SF ni API key real.
