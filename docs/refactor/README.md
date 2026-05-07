# Refactor `ai_chat.py` — baselines + workspace

**Branch**: `refactor/moby-modularize` (off `main` @ `6149d56`).
**Plan**: `docs/refactor-ai-chat-plan.md` — alternativa A (mínimo).
**Estado**: Fase 0 en curso (limpieza pre-refactor + tests faltantes).

## Baselines (snapshot anterior al refactor)

- `baseline-pytest.txt` — `pytest backend/tests/ --tb=no -q`. 603 pass / 3 fail.
  Los 3 fallos son pre-existentes (`test_moby_planner.py::test_pharmacy_bare_*` y
  `test_unresolved_lowers_confidence`); no son regresión.
- `baseline-bulk-eval-on.json` / `baseline-bulk-eval-off.json` — copia de
  `docs/moby-semantic-eval-results-{on,off}.json` capturada antes de tocar nada.

## Cómo re-correr el bulk eval

```bash
SF_SESSION_COOKIE="..." API_BASE="http://localhost:8000" \
  python scripts/run_bulk_eval.py
# salida → docs/moby-semantic-eval-results-{on,off}.json
diff docs/refactor/baseline-bulk-eval-on.json docs/moby-semantic-eval-results-on.json
```

## Métricas a vigilar entre fases

1. `pytest` → mismo conteo (603 pass / 3 pre-existing fail) o mejor.
2. Bulk eval → mismas filas pass/fail; outputs textuales pueden variar
   (Claude no determinista) pero la categoría no debe cambiar.
3. `wc -l backend/app/routers/ai_chat.py` → debe bajar fase a fase.
4. Smoke manual de `/api/ai/chat/stream` con 1 query simple, 1 con tool.
