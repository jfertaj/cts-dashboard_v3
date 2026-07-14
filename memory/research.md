
### [2026-07-14] [investigador] — La búsqueda en bloque no puede devolver `sf.*`: el frontend le quita el prefijo y el constructor del SELECT lo exige

**Pregunta**: ¿Por qué `POST /api/explorer/search` devuelve `null` en los campos `sf.*` de métrica (81 de 215 centros tienen valor real) mientras el `explorerFillColumns` perezoso sí los trae? ¿Es el fill perezoso un diseño de rendimiento intencionado o el strip de `sf.` en `api.ts:347` es un bug?

**Método**: Lectura directa del handler `explorer_search()` y de `explorer_fill_columns()` en `backend/app/routers/salesforce_explorer.py`; trazado de `columns` → SOQL SELECT → `row.data`. `git log -S` sobre la línea del strip y sobre el guard del backend. Conteo del SELECT peor caso desde `backend/app/config/sf_schema_full.json`. Grep de consumidores de `row.data` en `frontend/src`.

**Hallazgo**:
1. **El constructor del SELECT exige el prefijo `sf.`** — `salesforce_explorer.py:2651-2656`:
   `for k in requested_cols: if not k.startswith("sf."): continue` → `fld = k[3:]` → `opp_fields.add(fld)`.
   Una columna que llega **sin** prefijo (`C_Number_of_Individuals_screened_intotal__c`) falla el `startswith` → `continue` → **nunca entra en `opp_fields`** → nunca entra en `select_parts` (2667) ni en `soql_opps` (2672).
2. **El lector de `data` sí tolera la clave pelada, pero ya es tarde** — `salesforce_explorer.py:3178-3231`: la clave pelada no casa con ninguna rama (`site.`/`Account.`/`qual.`/`sf.`/`extra.`) y cae al fallback `data[k] = o.get(k)` (3231). Como el campo nunca se pidió en el SOQL, `o` no tiene la clave → `None`. La clave **existe** en `data` con valor `null`: esto explica exactamente el síntoma "la fila sigue teniendo ~360 claves, todas null".
3. **Asimetría selector/lector**: el lector (3231) funcionaría perfectamente con la clave pelada **si** el selector la hubiera metido en el SELECT. El bug es que el selector (2651) la descarta.
4. **Por qué el fill sí funciona**: `frontend/src/lib/api.ts:554` manda `columns: cols` **verbatim, con el prefijo `sf.` intacto** (no hay strip). El fill tiene el **mismo** guard (`salesforce_explorer.py:3803`: `if not k.startswith("sf."): continue`). Es decir: **los dos endpoints comparten el mismo constructor de SELECT y ambos exigen el prefijo**. El fill acierta sólo porque no quita el prefijo. **No hay ninguna decisión de rendimiento aquí.**
5. **Por qué nadie lo notó antes**: el SELECT se siembra a mano en `salesforce_explorer.py:2639-2644` con `Id, Name, Type, StageName, IsClosed, CloseDate, AccountId, RecordType.*` **más `C_Number_of_new_T1D_diagnosed_U_18__c` y `C_Number_of_new_T1D_diagnosed_O_18__c`**. Esos campos ND vuelven no-nulos siempre. Además, los campos usados en filtros entran en el SELECT vía `sf_rules` (2647-2649). Todo lo demás llega `null`.
6. **`needsFill()` (`ExplorerView.tsx:1055-1069`) es lo que enmascara el bug**: ve `null` en las columnas VISIBLES y dispara el fill, que las repara. Por eso la tabla se ve bien y sólo se rompe lo que lee `row.data` **sin** ser columna visible (los charts).
7. **La intención documentada es que la búsqueda en bloque lo traiga TODO**: `ExplorerView.tsx:1562` `// SIEMPRE pedimos TODAS las columnas` y `ExplorerView.tsx:1893` `// Traemos TODO otra vez`, ambos pasando `allKeys` / `allColumnKeys`. El strip de `api.ts:347` **derrota silenciosamente esa intención**.
8. **Coste real de arreglarlo**: ninguno relevante. El peor caso (los 294 campos de Opportunity) da una cláusula SELECT de **8.495 caracteres**, contra el límite REST de SOQL de **100.000**. Es **una sola query** en ambos casos; el coste de SOQL es por query, no por campo. `docs/incident-distance-matrix-cost.md` **no menciona SOQL ni columnas ni field-count** — ese incidente es de la Distance Matrix de Google Maps, no tiene nada que ver. El único comentario de coste en el fichero (3750) habla de no traer todas las cuentas si nadie abrió la tabla, no del número de campos.
9. **`git log -S`**: tanto el strip (`api.ts`) como el guard del backend vienen del **Initial commit (1214575)**. No hay ningún commit que introduzca el strip como fix deliberado. **No hay evidencia de intención de diseño en ninguna dirección** — es un desajuste original entre las dos mitades.
10. **⚠️ El strip SÍ es load-bearing para `Account.*`**: hoy `sf.Account.Name` se convierte en `Account.Name` y así entra por la rama `k.startswith("Account.")` (3184), que resuelve desde `acc_map` / `account_extras_by_acc`. Si simplemente se deja de hacer strip en `api.ts:347`, `sf.Account.X` caería en la rama `sf.` (3216) → `fld = "Account.X"` → `_exists_on_opportunity`/`_exists_on_account` fallan (el set tiene `Name`, no `Account.Name`) → `data[k] = None`. **Quitar el strip a secas regresiona las columnas `sf.Account.*`** (a `sf.Account.Name` y `sf.Account.Id` los salvaría el fallback de `readDataCell:43-44`, pero no al resto, p.ej. `sf.Account.INNODIA_Clinical_Trial_Site__c`).

**Consumidores de `row.data`** (quién come el null):
- **Celdas de la tabla** (`ExplorerView.tsx:2207,2266` vía `readDataCell`) → **correcto**, reparado por el auto-fill.
- **Export TSV** (`ExplorerView.tsx:2469-2491`) → **correcto**: sólo exporta columnas VISIBLES, que están rellenadas.
- **Charts** (`lib/chartAggregation.ts:47`, `lib/chartDataset.ts:36,61,158`) → **era el roto**; mitigado registrando las métricas en `lib/fillColumns.ts` (`CHART_METRIC_COLUMNS`).
- **Mapa** (`points`) → **no afectado**: usa el array `points`, no `row.data`.
- **Hand-off a Moby** (`ExplorerView.tsx:1395-1398` `askAI`) → **no afectado**: manda un prompt de texto; Moby consulta SF en el servidor.
- **MembersView** (`pages/MembersView.tsx:175,301`) → **no afectado**: llama a `/api/members/*` con `fetch` directo, sin pasar por `explorerSearch` ni por el strip.

**Implicaciones**:
- El fill perezoso **NO es un diseño de rendimiento**. Es un caché de celdas (TTL 2 min + LRU, `api.ts:507-524`) que resultó ser el único camino que funciona, por accidente.
- **El fix correcto es en el BACKEND**, no quitar el strip: hacer que el constructor del SELECT (2651-2658) acepte **ambas** formas (pelada y prefijada), resolviendo la clave pelada contra `_exists_on_opportunity()`. Es compatible hacia atrás (el fill sigue mandando prefijo) y no toca la rama `Account.*` que el strip alimenta.
- Registrar las métricas en el fill (lo ya hecho) es una **mitigación válida y de bajo riesgo**, pero deja la trampa armada: la próxima feature que lea `row.data` de un campo no visible volverá a ver `null` en silencio.

**Siguientes preguntas abiertas**:
- ¿Merece la pena, además del fix del SELECT, hacer que `data[k]` devuelva la clave **prefijada** siempre (normalizar la salida) para que `rePrefixDataKeys` deje de ser necesario?
- Con el SELECT arreglado, ¿sigue haciendo falta el auto-fill para columnas visibles, o queda como puro caché de re-render?
