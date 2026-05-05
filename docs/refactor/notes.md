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

## 6. Cobertura de tests añadida en Fase 0

| Archivo | Tests | Cubre |
|---|---|---|
| `test_agentic_loop.py` (existente, +2) | 25 | loop normal, fast-exit, retry, dedup, timeout, multi-tool, RateLimit error. |
| `test_synthesis_fallback.py` (nuevo) | 9 | helper + trigger guard (4 ramas). |
| `test_claude_chat.py` (nuevo) | 10 | tool_choice, force_no_tools, override, thinking + beta, streaming, error. |

Total: **44 tests cubren las áreas que se moverán en Fases 1-3**. Todas con
mocks de Anthropic; ninguna requiere SF ni API key real.
