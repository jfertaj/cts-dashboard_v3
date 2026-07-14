# Reviews

### [2026-07-13] [revisor] — 5550a88..1689f4c — Task 1: Vitest + extracción de `readDataCell` a `lib/rowAccess`

**Commits revisados**: `349b0d3..1689f4c` (`5550a88` código, `1689f4c` memory notes).
**Tarea/Spec**: `.superpowers/sdd/task-1-brief.md` (Task 1 de 10 — rediseño del chart modal del Explorer).

**Stage 1 — Spec compliance**: ✅
- ✓ Vitest instalado como devDependency; scripts `test` / `test:watch` añadidos sin tocar los `test:e2e*` / `test:smoke` (`frontend/package.json:8-9`).
- ✓ `vite.config.ts`: `defineConfig` importado de `vitest/config`, bloque `test { environment: "node", include: ["src/**/*.test.ts"] }`. `plugins`/`envDir`/`server`/`proxy`/`preview`/`build` intactos.
- ✓ `frontend/src/lib/rowAccess.ts` creado con la interfaz exacta del brief (`DataRow` + `readDataCell(row, key): unknown`).
- ✓ **Cuerpo movido VERBATIM** — verificado con diff mecánico del cuerpo viejo (`git show 349b0d3:…/ExplorerView.tsx`, función en L912) contra `lib/rowAccess.ts`: 32 líneas vs 32 líneas, **únicas diferencias = línea de firma + anotación `const d: Record<string, unknown>`**. Las 4 capas de fallback, su orden, el guard `!== undefined && !== null && String(v).trim() !== ""` y el `return undefined` final son byte-a-byte idénticos.
- ✓ `ExplorerView.tsx`: definición local borrada, `import { readDataCell } from "../lib/rowAccess"` añadido (L39). Los 10 call sites (L988, 1059, 1071, 2201, 2260, 2377, 2397, 2402, 2414, 2418) sin tocar.
- ✓ 5 tests del brief, copiados literalmente, incluida la regla empty-string = ausente.
- ✓ Sin `any` en el código nuevo (`rowAccess.ts` / `rowAccess.test.ts`).
- ⚠ Extra fuera de los 6 ficheros del brief: `memory/code-notes.md` + `memory/INDEX.md` (commit `1689f4c`). **No se considera scope creep**: lo exige el protocolo Memory Palace de `CLAUDE.md` para el rol coder, y va en commit separado. Cero cambios de producto no pedidos.

**Stage 2 — Code quality**:
  - Strengths:
    - Extracción quirúrgica: cero cambios de comportamiento, verificado mecánicamente y no por self-report.
    - Baseline de `tsc --noEmit` verificado de forma independiente por el revisor: **12 errores, misma distribución por fichero que antes** (6 ExplorerView / 2 ChatView / 1 Header / 1 MapView / 1 MemberMapView / 1 SalesforceLinker). Ninguno menciona `rowAccess` ni `readDataCell`. El `TS2347` en `ExplorerView.tsx:985` es sobre `row.getValue<any>()` y existe idéntico en `349b0d3` — pre-existente.
    - Los tests son aserciones reales sobre valores concretos, no tautologías.
    - El coder documentó honestamente en `code-notes.md` que ningún gate del repo hace typecheck — gotcha valioso y correcto.
  - Issues:
    - [Important] Cobertura de tests insuficiente para el riesgo que dice cubrir — `frontend/src/lib/rowAccess.test.ts:1-29`. Los 5 tests cubren la rama 1 (clave exacta), la rama 2 (sin prefijo `sf.`) y **una** de las 6 salidas planas (`account.name`). Quedan SIN test: las variantes con underscore (`k2`/`k3`, `rowAccess.ts:24-28`), `account.id`, `country`, `city`, `account.shippingcountry`, `account.shippingcity` (`rowAccess.ts:35-38`), y la regla empty-string en cualquier rama que no sea la 1ª. Un borrado silencioso de esas ramas —exactamente el failure mode que la spec declara Critical— pasaría la suite en verde. Los tests son los del brief, así que no es incumplimiento; es deuda que hay que saldar antes de que Tasks 2+ se apoyen en esta red.
    - [Minor] `frontend/package.json:47` declara `"vitest": "^2.1.9"`, no `"^2.1.0"` como pedía el brief (Step 1) y como afirma el report ("devDependency `vitest@^2.1.0`"). Resuelve al mismo 2.1.9 y `^2.1.9` ⊂ `^2.1.0`, así que es inocuo — pero el report es inexacto en ese punto.
    - [Minor] Vitest 2.x tiene peer `vite@^5`, así que el lockfile ahora anida un **segundo toolchain** (`vite@5.4.21` + `esbuild@0.21.5` bajo `vite-node`) junto al `vite@6.2.6` raíz. Irrelevante para funciones puras; a tener en cuenta si algún día se testean componentes. Vitest 3 lo alinearía.
    - [Minor] La firma `row: DataRow` no refleja que el cuerpo es defensivo contra `null`/`undefined` (`row?.data`, `row?.account_name`). El coder ya lo anotó como concern; correcto no tocarlo en esta task.

**Veredicto**: approved-with-issues (no bloquea el merge ni la Task 2).
**Acciones requeridas del coder**:
1. (Antes de que Task 2+ dependa de esta red de tests) añadir casos para las ramas `k2`/`k3` con underscore y para las salidas planas `account_id` / `country` / `city` / `account.shippingcountry` / `account.shippingcity`, más un caso de empty-string que caiga a través de ≥2 ramas.
2. (Trivial) Alinear `package.json` con el brief (`"vitest": "^2.1.0"`) o dejar constancia de que la desviación es deliberada.

---

### [2026-07-13] [revisor] — 1689f4c..4604c40 — Task 1 fix: cobertura de fallback de `readDataCell` (re-review del Important)

**Commits revisados**: `b5ff230` (cubre las ramas de fallback), `4604c40` (nota de coder).
**Tarea/Spec**: acción requerida #1 de la entrada anterior (2026-07-13, `5550a88..1689f4c`) — cubrir con tests las ramas de fallback de `frontend/src/lib/rowAccess.ts` que el hallazgo Important dejaba sin proteger.

**Stage 1 — Spec compliance**: ✅
- ✓ `frontend/src/lib/rowAccess.ts` **genuinamente sin tocar**: `git diff 1689f4c -- frontend/src/lib/rowAccess.ts` vacío, y no aparece en absoluto en el diff de revisión (`review-1689f4c..4604c40.diff` sólo toca `rowAccess.test.ts` + `memory/INDEX.md` + `memory/code-notes.md`). Confirmado independientemente, no por self-report.
- ✓ `rowAccess.test.ts` pasó de 5 a 21 tests. Trazado a mano (sin ejecutar la suite) contra `rowAccess.ts:16-47`, todas las ramas alcanzables tienen un test que rompería si se borrara esa rama:
  - Capa 1 (L20, clave exacta): "lee la clave exacta…", "devuelve el 0 numérico…".
  - Capa 2 (L24, sin prefijo `sf.`): aislada por "…aunque conserve puntos…" (el primer test de ese describe, "cae a la clave sin el prefijo sf.", NO aísla la rama por sí solo — ver Minor).
  - Capa 3 k2 (L28) y k3 (L31): aisladas cada una por su test dedicado + el fall-through de null/whitespace.
  - Capa 4 flat `account.name`/`account.id` (L38/L39) y `country`/`city` (L42/L43): cubiertas, incluida la variante case-insensitive de `kb`.
  - Fall-through honesto entre capas: 3 tests (`empty en capa1→capa2`, `null+whitespace→k2`, `4 capas vacías→flat`) con valores reales distintos en cada capa, no placeholders.
- ✓ Reclamo de código muerto en L40-41 **verificado independientemente por trazado manual, no por su palabra**: para cualquier `key` que satisfaga `key === 'sf.Account.Name'`, `base = key.replace(/^sf\./, '')` es determinísticamente `'Account.Name'` y `kb = base.toLowerCase()` es `'account.name'` — la condición de L38 (`kb === 'account.name'`) es siempre cierta primero y hace `return` antes de que el control llegue a L40. Mismo razonamiento para L39/L41 con `account.id`. Confirmado: no existe ningún input capaz de alcanzar L40/L41 con el código de L38/L39 presente; correcto no añadir test (no se puede testear código inalcanzable) y correcto no borrarlas (fuera del scope: "no tocar `rowAccess.ts`").
- Ningún ✗ ni ⚠: no hay features fuera de scope, `rowAccess.ts` no se tocó como exigía la restricción.

**Stage 2 — Code quality**:
  - Strengths:
    - Metodología de mutation-testing manual explicada y verificable: la tabla de "supervivientes por línea" del fix report es internamente consistente con el propio código al trazarla a mano (p.ej. por qué borrar L38 sólo hace fallar 1 de los 2 tests de `account.name` — porque L40, muerta con L38 presente, "resucita" y rescata el caso `sf.Account.Name` pero no el caso `Account.Name` sin prefijo). Esa asimetría es sutil y el report la explica correctamente en vez de ocultarla.
    - El hallazgo de código muerto se documenta y se deja sin tocar respetando el scope de la task — no es un "arreglo" no pedido.
    - Los 3 tests de fall-through usan valores reales distintos por capa (no `true`/`1` genéricos), así que un test en verde realmente prueba que se leyó la capa correcta, no cualquier capa.
  - Issues:
    - [Minor] `frontend/src/lib/rowAccess.test.ts:18-21` ("cae a la clave sin el prefijo sf.") no aísla la capa 2 por sí sola: con `base` sin puntos, `k3` (L31) coincide con `base` y rescataría el mismo valor si se borrara sólo L24. La rama SÍ queda protegida (por el test siguiente, "…aunque conserve puntos…"), pero ese primer test es redundante con la capa 3 tal y como está escrito. No bloquea nada — cosmético.
    - [Minor] La acción #2 de la review anterior (alinear `"vitest": "^2.1.0"` en `package.json`) sigue sin resolver (`frontend/package.json:49` sigue en `"^2.1.9"`). Fuera del scope de este fix (que era sólo cobertura de tests), pero queda pendiente si alguien quiere cerrarla.

**Veredicto**: approved
**Acciones requeridas del coder**: ninguna bloqueante. Opcional: alinear la versión de `vitest` en `package.json` con el brief, o documentar la desviación como deliberada.

---

### [2026-07-13] [revisor] — 4604c40..1d19965 — Task 2: `chartAggregation` (parseo de métrica, cobertura, agrupación por país)

**Commits revisados**: `1d19965` (único commit del rango). Diff = 2 ficheros nuevos, 132 líneas, sin borrados.
**Tarea/Spec**: `.superpowers/sdd/task-2-brief.md`. Restricción global: *una fila sin dato para una métrica está AUSENTE, nunca es cero* — no suma y no cuenta como centro.

**Stage 1 — Spec compliance**: ✅
- ✓ Fichero de review verificado como fiel: `git diff 4604c40 1d19965` es byte-idéntico al cuerpo de `review-4604c40..1d19965.diff` (sólo difiere en la cabecera prependida). Reviso el código real, no el report.
- ✓ Firmas y nombres exportados **idénticos** al bloque "Produces" del brief: `COUNT_METRIC`, `SCREENED`, `STAGE1`, `STAGE2`, `ASSIGNMENTS`, `type Coverage`, `type CountryBucket`, `toMetricValue(raw: unknown): number | null`, `coverageFor(rows, metricKey): Coverage`, `groupByCountry(rows, metricKey): CountryBucket[]` (`chartAggregation.ts:3-10,19,34,40`).
- ✓ Implementación y tests **verbatim** del brief. Cero scope creep: `git diff --name-only 4604c40 1d19965` → sólo los 2 ficheros que el brief manda crear. No se tocó `rowAccess.ts`.
- ✓ Consume `readDataCell` / `DataRow` de Task 1 (`chartAggregation.ts:1`); no los reimplementa.
- ✓ Sin `any` ni `@ts-` en el fichero nuevo (grep vacío).
- ✓ **Los 4 nombres de campo SF verificados contra el backend** (el report admitía no haberlo hecho): `SCREENED`/`STAGE1`/`STAGE2` coinciden exactos con `backend/app/config/fields_opportunity_curated.json`; `ASSIGNMENTS` = `extra.AssignmentsCount` coincide con `backend/app/routers/salesforce_extras.py:356` y `salesforce_explorer.py:2298`. Sin typos. El concern #4 del coder queda **cerrado con evidencia**.

**Stage 2 — Code quality**:
  - Strengths:
    - `toMetricValue` (`chartAggregation.ts:19-26`) es correcto en las 6 clases de input: `0` → `0` (el `n < 0` es estrictamente menor, el cero legítimo sobrevive); `null`/`undefined` → `null` (L20); `""`/whitespace → `null` (L22, **antes** del `Number()`, que devolvería `0`); no numérico → `null` vía `!Number.isFinite` (L24); negativo → `null` (L24). Trazado a mano, no por el report.
    - `groupByCountry` (`chartAggregation.ts:40-51`): la guarda `if (value === null) continue` (L44) está **antes** de crear/tocar el bucket (L46-49). Una fila sin dato no puede influir ni en `value` ni en `sites` ni provocar la aparición del país — el `continue` precede a `buckets.get(...) ?? {...}`. Corolario correcto: un país donde nadie reporta desaparece del array.
    - `coverageFor` (`chartAggregation.ts:34-38`) cuenta lo que dice: `withData` = filas con `valueOf != null` = centros que reportan; con `COUNT_METRIC`, `valueOf` corta con `1` (L30) y nunca es `null`, así que `withData == total` y `missing == 0` — 100% de cobertura por construcción.
    - Los tests SÍ fijan la regla ausente≠cero — verificado mutando el código a mano, no fiándome de la tabla del report. Spot-checks: **M5** (`n < 0` → `n <= 0`) rompe "conserva el cero legítimo" (`test:31`) **y** el `withData: 2` de `coverageFor` (`test:37-38`, cuyas filas incluyen `site("IT", 0)`) → 2 fallos, coincide. **M6** (borrar el `continue` de L44) hace que `null` se pliegue a `0` por coerción JS y `sites` se incremente igual: rompe `{ country: "ES", value: 15, sites: 2 }` (`test:53`, el `toEqual` compara el objeto entero, incluido `sites`) y rompe "omite países donde nadie reporta" (`test:60`, IT aparecería con `value: 0`) → 2 fallos, coincide. Cambiar `null` por `0` en cualquier punto NO pasa la suite.
  - Issues:
    - [Important] **`CountryBucket.sites` cuenta reportadores, no centros del país — y el nombre no lo dice** (`chartAggregation.ts:10`, sin JSDoc). La **semántica es correcta** y obligada por la restricción global (si contara todos, `value/sites` mentiría: "100 cribados en 20 centros" cuando 19 no reportan). El riesgo real es el inverso al que temía el coder: **el denominador por país NO es recuperable del array devuelto** — `groupByCountry` elimina las filas ausentes, así que una vista que quiera "ES: 2 de 9 centros reportan" no puede sacar el 9 de `CountryBucket`, y `coverageFor` sólo da un total **global**, no por país. Una vista de Task 3/6/7/8 que agarre `.sites` como denominador de cobertura subestimará en silencio. Mitigación mínima: JSDoc en L10 (`sites` = centros que reportan la métrica) + la vista que necesite el total por país debe usar `groupByCountry(rows, COUNT_METRIC)`. Mejor: renombrar a `reportingSites` — pero eso toca el brief y lo importan 4 tasks, así que **decide el orquestador**, no el coder unilateralmente.
    - [Important] **`extra.AssignmentsCount` viene con `0` por defecto del backend** (`backend/app/routers/salesforce_extras.py:363-377`: el dict `defaults` rellena `"extra.AssignmentsCount": 0` para todo account sin assignments). Consecuencia aguas abajo, no defecto de este módulo: para la métrica `ASSIGNMENTS` el "ausente" ya viene plegado a `0` en origen, así que `toMetricValue` lo conserva (correcto: un `COUNT()` de 0 **es** un cero real), pero `coverageFor(rows, ASSIGNMENTS)` dirá siempre **100% de cobertura** y `sites` incluirá a todos los centros. Es decir: `sites` significa una cosa distinta para `ASSIGNMENTS` que para `SCREENED`/`STAGE1`/`STAGE2`. Las vistas no deben pintar banner de cobertura para `ASSIGNMENTS` como si fuera informativo.
    - [Minor] **Rama `"(sin país)"` sin test** (`chartAggregation.ts:45`): ningún test tiene una fila con `country` ausente o en blanco (grep confirmado). Un mutante que borre `|| "(sin país)"` (dejando `""`) **sobrevive** a los 10 tests. No es un bug hoy —el Explorer siempre trae `country`— pero es un hueco real en el mutation score que la tabla de 9 mutantes del report no cubre.
    - [Minor] **Mutación local vs. regla de inmutabilidad del repo**: `bucket.value += value` / `bucket.sites += 1` (L47-48) mutan el objeto, contra `.claude/rules/ecc/common/coding-style.md` ("ALWAYS create new objects, NEVER mutate"). En la práctica es inocuo (acumulador local, el objeto no escapa antes del `return`, la función sigue siendo pura), y viene verbatim del brief. Además `buckets.set(country, bucket)` (L49) es un no-op redundante tras la primera iteración: `buckets.get` ya devuelve la misma referencia. Cosmético.
    - [Minor] **`String(raw).replace(/,/g, "")` trata la coma como separador de millares siempre** (L21): un `"1,5"` con coma decimal (locale europeo) se convierte en `15`, no en `1.5`. Riesgo bajo (SF devuelve estas métricas como números, y son conteos enteros de pacientes), pero es una trampa silenciosa si alguna vez llega un decimal como string localizada. El coder ya anotó bien en `code-notes.md` que el orden comas→`""`→`Number()` es frágil ante un "refactor de limpieza".
  - No verificable desde el código (lo digo explícitamente):
    - **No re-ejecuté la suite** (instrucción del orquestador). Los outputs RED/GREEN (31/31) y la tabla de 9 mutantes del report **no los he confirmado ejecutando**; sí he confirmado por trazado manual del código+tests que M2, M5 y M6 matan lo que el report dice que matan, y que el conteo de fallos que reporta es consistente con los asserts reales. La cobertura de `tsc --noEmit` (12 errores pre-existentes) tampoco la he re-ejecutado.

**Veredicto**: approved-with-issues
**Acciones requeridas del coder**:
1. (Important, en este fichero) Añadir JSDoc a `CountryBucket` (`chartAggregation.ts:10`) diciendo explícitamente que `sites` = **centros que reportan la métrica**, no centros del país, y que el total por país se obtiene con `groupByCountry(rows, COUNT_METRIC)`. Sin cambiar el nombre del campo (lo importan 4 tasks; el rename lo decide el orquestador).
2. (Minor) Añadir 1 test de la rama `"(sin país)"` — fila con `country` en blanco/ausente — para cerrar el mutante superviviente de L45.
3. (Para las tasks de vistas, no para este fichero) No pintar banner de cobertura sobre `ASSIGNMENTS`: el backend ya rellena `0` y la cobertura siempre saldrá 100%.

---

### [2026-07-13] [revisor] — 9b0c437..f3084cb — Task 3 `chartAggregation`: ranking (topN/bottomN), Pareto y embudo

**Commits revisados**: `381eb8c` (código + tests), `f3084cb` (docs). Base `9b0c437`.
**Tarea/Spec**: `.superpowers/sdd/task-3-brief.md`. Restricción global: *una fila sin dato para una métrica está AUSENTE, nunca es cero*. Puntos calientes de esta task: `bottomN` (el mudo no puede ser "el peor") y `funnel` (sólo suman los que reportan las TRES etapas).

**Stage 1 — Spec compliance**: ✅
- ✓ Fichero de review fiel: `git diff --stat 9b0c437..f3084cb` == cabecera del `.diff`, y `git diff f3084cb -- frontend/src/lib/` vacío → el working tree ES el commit revisado. Reviso código real.
- ✓ Firmas/nombres exportados **idénticos** al bloque "Produces": `SiteValue`, `Pareto`, `FunnelStage`, `Funnel`, `topN`, `bottomN`, `distribution`, `funnel` (`chartAggregation.ts:61-64,81,85,89,105`).
- ✓ Implementación y los 11 tests **verbatim** del brief. Cero scope creep: sólo los 2 ficheros del brief + los 2 de memoria (protocolo). No se tocó `rowAccess.ts` ni las funciones de Task 2.
- ✓ Reutiliza el `valueOf()` privado de Task 2 (`chartAggregation.ts:36`); NO hay segundo parser. Sin `any`.

**Stage 2 — Code quality**:
  - Strengths:
    - **`bottomN` es inalcanzable para un centro mudo — probado, no asumido.** Único camino de datos: `bottomN` → `sitesWithData` (`ts:67-79`) → `valueOf` (`ts:36`) → `toMetricValue(readDataCell(...))`. `readDataCell` devuelve `undefined` para clave ausente (`rowAccess.ts:46`) y salta los vacíos (`rowAccess.ts:20-31`); `toMetricValue` mapea `undefined`/`""`/no-numérico/negativo → `null` (`ts:27-31`); `sitesWithData:71` hace `continue` **antes** del `push`. No existe `?? 0` ni coerción en esa ruta. `bottomN` no toca `rows` directamente (`ts:86`), sólo el array ya filtrado. **Confirmado: un centro sin dato no puede salir por ninguna vía.**
    - **`funnel` no deja entrar a los parciales.** `complete` (`ts:106-110`) exige `!== null` en las TRES métricas; `sum()` (`ts:111-112`) reduce **sobre `complete`**, no sobre `rows`. `valueOf` es pura y determinista, así que un parcial no puede colarse en ninguna etapa. `sitesExcluded = rows.length - complete.length` (`ts:120`) es correcto.
    - **Aritmética del Pareto correcta.** `running` acumula sobre `bars` en el mismo orden en que `total` se reduce (`ts:90-96`) → en la última barra `running === total` bit a bit, así que el último `cumulativePct` es **exactamente 100** (no 99 ni 101). Guarda `total === 0 ? 0` evita el `0/0 = NaN` (todos los valores son ≥ 0 porque `toMetricValue` rechaza negativos, luego `total === 0` ⟺ todas las barras valen 0). Caso 1 centro → `[100]`. `missingSites = rows.length - bars.length` correcto por construcción de `bars`.
    - **Los tests SÍ fijan la regla, verificado mutando a mano (no me fío de la tabla del report).** M-A: `sitesWithData` pusheando `value ?? 0` en vez de `continue` → muere en *"los centros sin dato NUNCA aparecen en el bottom"* (`test:114-117`, el mudo saldría primero con 0) **y** en *"cuenta aparte los centros sin dato"* (`test:132-137`, `bars` pasaría de 1 a 3). M-B: `funnel` con `||` en vez de `&&` → muere en `test:163` (`sum(SCREENED)` daría 1099 ≠ 100 y `sitesIncluded` 2 ≠ 1) **y** en `test:175`. M-E: borrar la guarda `total === 0` → `Math.round(NaN)` rompe `test:144`. Sustituir `null` por `0` en el choke point no pasa la suite.
  - Issues:
    - [Important] **El caso "reporta 0 legítimo" del filtro de `funnel` NO está fijado por ningún test — mutante superviviente.** Cambiar `ts:107-109` de `valueOf(row, X) !== null` a un chequeo de verdad (`valueOf(row, SCREENED) && valueOf(row, STAGE1) && ...`) **pasa los 43 tests**: el único centro completo de los tests es `full("completo", 100, 10, 2)`, todos truthy, y el segundo test sólo usa un parcial. Con ese mutante, un centro real que reporta `screened=50, stage1=0, stage2=0` (cribó, nadie progresó — escenario clínico corriente) desaparecería del embudo entero y el total de cribados quedaría subestimado en silencio. Es el reverso exacto de la regla ("un 0 reportado SÍ es un valor") y es el único de los dos filtros del módulo que no lo tiene cubierto (`sitesWithData` sí, vía `test:144`). **Falta 1 test**: `funnel([full("cero", 50, 0, 0)])` → `sitesIncluded: 1`, `sitesExcluded: 0`, stages `[50, 0, 0]`.
    - [Important] **El `?? 0` de `sum()` (`ts:112`) debe irse** — respuesta al concern (a) del coder, y coincido con él. Hoy es **código muerto verificable** (el `filter` de `ts:106-110` garantiza no-null; quitarlo no cambia ningún test), y es literalmente la puerta trasera del bug: convierte cualquier aflojamiento futuro del filtro en un embudo que miente sin fallar, en vez de en un crash o un `NaN` visible. Código muerto que sólo puede hacer daño = borrarlo. Sustituto sin `??`: filtrar a valores ya no-nulos, p.ej. `const sum = (key: string) => complete.reduce((acc, row) => acc + (valueOf(row, key) as number), 0)` es peor (miente al type-checker); lo limpio es extraer los 3 valores **en el filter** (`complete: {s,s1,s2}[]` vía `flatMap`/`reduce`) y sumar sobre ellos — el `null` deja de existir en el tipo y el back door se cierra estructuralmente, no por convención. Viene verbatim del brief, así que el cambio lo autoriza el orquestador.
    - [Important] (carry-over, **no** defecto de este diff) **`bottomN(rows, ASSIGNMENTS)` sí listará centros que no reportan como "los peores"** — pero por el backend, no por este módulo: `backend/app/routers/salesforce_extras.py:363-377` rellena `"extra.AssignmentsCount": 0` por defecto para todo account sin assignments (verificado leyendo el fichero). Ese `0` llega al frontend como un cero legítimo indistinguible. Ya se avisó en la review de Task 2 para `coverageFor`; en Task 3 la consecuencia es más visible (un ranking de "peores centros" lleno de centros sin datos de assignments). Las vistas de Tasks 6-8 no deben ofrecer `ASSIGNMENTS` en la vista de bottom-N, o hay que arreglar el default en el backend (`0` → ausente).
    - [Minor] **`distribution(rows, COUNT_METRIC)` sigue sin guarda** — concern (b), de acuerdo con el diagnóstico: `valueOf` corta con `1` (`ts:37`), así que salen N barras de valor 1, `missingSites: 0` y un Pareto perfectamente plano — un gráfico que se pinta bien y no dice nada. Mi juicio: **no** meter un `throw` en la librería (fuera del alcance de esta task y YAGNI); la puerta correcta es la vista (Tasks 6-8 no deben ofrecer "Nº de centros" como métrica de distribución). Lo mínimo **ahora**: JSDoc en `distribution` diciendo que `COUNT_METRIC` no es una métrica válida aquí, para que el que escriba la vista lo lea en el sitio. `funnel` es inmune (no toma `metricKey`).
    - [Minor] **Ramas de fallback de `sitesWithData` sin test** (`ts:73-75`): ninguna fila de test tiene `account_id` o `account_name` ausentes. Un mutante que borre `?? row.account_name ?? ""` (dejando `String(undefined)` → `"undefined"`) o el `?? "(sin nombre)"` **sobrevive a los 43 tests**. Es exactamente el hueco de cobertura que la review de Task 2 hizo cerrar para `"(sin país)"`; misma clase, mismo fichero. Sobre el concern (c): la colisión de key en React sólo ocurre si `account_id` **y** `account_name` faltan a la vez en ≥2 filas — improbable con datos del Explorer, así que **Minor, no Important**; la mitigación barata es que las vistas usen `key={`${s.accountId}-${i}`}` o el índice, no endurecer el tipo aquí.
    - [Minor] **`funnel` evalúa `valueOf` 6 veces por fila** (3 en el `filter`, 3 en los `sum`), y cada `valueOf` reejecuta la cascada de `readDataCell`. Con 215 centros es irrelevante; lo apunto porque el refactor que cierra el `?? 0` (extraer los 3 valores en el filter) **también** elimina esta redundancia — un solo cambio arregla las dos cosas.
    - [Minor] **`cumulativePct` puede llegar a 100 antes de la última barra** por `Math.round`: con `[1_000_000, 1]` da `[100, 100]`. Cosmético (la línea de Pareto se aplana un bar antes); con 215 centros y estas métricas, improbable. Sin acción.
  - Gate de arrastre (fixes exigidos por la review de Task 2) — **ambos verificados en el código actual**:
    - ✓ (i) JSDoc de `CountryBucket` (`chartAggregation.ts:11-17`): dice explícitamente que `sites` = centros que **REPORTAN** la métrica, no el total del país, y que el total por país se obtiene con `groupByCountry(rows, COUNT_METRIC)`. Cumple lo pedido literalmente.
    - ✓ (ii) Test de la rama `"(sin país)"` (`chartAggregation.test.ts:82-89`): dos filas, una con `country: ""` y otra sin `country`. Trazado el mutante: borrar `|| "(sin país)"` de `ts:52` deja `country === ""` y el `toEqual` contra `{ country: "(sin país)", ... }` falla. El test **mata** el mutante que motivó el hallazgo; no es un test de fachada.
    - (Ambos llegaron en `f7c1062`, anterior a la base de esta review — están en el código actual pero fuera de este diff.)
  - No verificable desde el código (explícito):
    - **No re-ejecuté la suite** (instrucción del orquestador). Los outputs RED (11 failed) / GREEN (43 passed), el `tsc --noEmit` y la tabla de 2 mutantes del report **no los he confirmado ejecutando**. Sí he trazado a mano que M-A, M-B y M-E matan lo que el report dice, que el conteo de fallos es consistente con los asserts reales, y —lo que el report NO dice— que existe un tercer mutante (truthy-check en el filter de `funnel`) que **sobrevive**.

**Veredicto**: approved-with-issues
**Acciones requeridas del coder**:
1. (Important) Añadir el test del cero legítimo en `funnel`: un centro con `screened=50, stage1=0, stage2=0` debe entrar (`sitesIncluded: 1`, stages `[50, 0, 0]`). Cierra el mutante superviviente del filtro.
2. (Important, requiere OK del orquestador por tocar código verbatim del brief) Eliminar el `?? 0` de `sum()` (`chartAggregation.ts:112`) extrayendo los tres valores no-nulos en el propio filtro, de modo que el `null` no exista en el tipo que se suma. Elimina de paso la reevaluación 6x de `valueOf`.
3. (Minor) 1 test para las ramas de fallback de `sitesWithData` (fila sin `account_id` → `accountId` cae a `account_name`; fila sin `account_name` → `name` = `"(sin nombre)"`). Misma clase que el fix de `"(sin país)"` de Task 2.
4. (Minor) JSDoc en `distribution` avisando de que `COUNT_METRIC` produce un Pareto sin sentido (N barras de valor 1).
5. (Para las vistas, Tasks 6-8 — no para este fichero) No ofrecer `ASSIGNMENTS` como métrica en bottom-N (el backend rellena `0` por defecto), ni `COUNT_METRIC` en la vista de distribución. Usar una key de React que tolere `accountId === ""`.

---

### [2026-07-13] [revisor] — f3084cb..a3671cc — Task 3 fix de review: cero legítimo en el embudo + muerte del `?? 0`

**Commits revisados**: `1e6885f` (tests), `fc18954` (refactor + JSDoc), `a3671cc` (docs de memoria). Base `f3084cb` (la review anterior de Task 3).
**Tarea/Spec**: cerrar los 4 hallazgos Important/Minor de la review previa (`memory/reviews.md#2026-07-13-revisor--9b0c437f3084cb...`, acciones requeridas 1-4; la 5 es de vistas futuras, fuera de alcance aquí).

**Stage 1 — Spec compliance**: ✅
- ✓ Acción 1 (test del cero legítimo): `chartAggregation.test.ts` añade `it("incluye al centro que reporta CEROS legítimos en las etapas tardías")` con `full("cribó pero no siguió", 50, 0, 0)`, asertando `sitesIncluded: 1`, `sitesExcluded: 0`, `stages: [50, 0, 0]`.
- ✓ Acción 2 (`?? 0` fuera): `funnel` reescrito en `chartAggregation.ts:118-141` — extrae `screened`/`stage1`/`stage2` dentro de un `for` con `continue` en el primer `null`, empuja a `complete: CompleteSite[]` (tipo local `{screened,stage1,stage2}: number`), y `sum(pick)` reduce sobre esa colección. Cero `?? 0` en todo el fichero.
- ✓ Acción 3 (fallbacks de `sitesWithData`): 2 tests nuevos en `describe("topN / bottomN")` — sin `account_id` (cae a `account_name`) y sin ninguno de los dos (`accountId: ""`, `name: "(sin nombre)"`).
- ✓ Acción 4 (JSDoc de `distribution`): añadido en `chartAggregation.ts:89-96`, explica que `COUNT_METRIC` da N barras de valor 1 y un Pareto sin sentido.
- ✓ Sin scope creep: sólo los 2 ficheros de código + los 2 de memoria (protocolo del equipo). La acción 5 (vistas) no se toca, correctamente diferida.

**Stage 2 — Code quality**:
  - Strengths:
    - **La reescritura de `funnel` es funcionalmente idéntica a la anterior para toda clase de input** (ausente, `""`, no-numérico, negativo, cero legítimo, positivo): ambas versiones excluyen con el mismo predicado `=== null` sobre las mismas tres llamadas a `valueOf` (pura, determinista — sin estado, sin I/O), así que evaluarla 2 veces (antes) o 1 vez (ahora) por campo da el mismo número. Firma pública `funnel(rows: DataRow[]): Funnel` y forma del retorno (`stages`/`sitesIncluded`/`sitesExcluded`) sin cambios; `CompleteSite` es un tipo local no exportado, no toca la API pública que Tasks 6-8 consumirán.
    - **Cero casts.** Grep de `as `, `!.`, `!;`, `!)`, `any` sobre el fichero completo: sin resultados. La narrowing de `screened`/`stage1`/`stage2` a `number` tras el `continue` es control-flow analysis normal de TS, no un `as`. `npx tsc --noEmit | grep chartAggregation` sin salida.
    - **El test del cero legítimo SÍ pin-ea la regla, verificado a mano**: sustituyendo el filtro por `if (!screened || !stage1 || !stage2) continue`, la fila `(50, 0, 0)` cae en la rama `!stage1` (0 es falsy) y queda excluida → `sitesIncluded` sería 0, no 1, y el test falla. Coincide con el mutante A'/A de la tabla del report.
    - **Los 2 tests de fallback de `sitesWithData` sí mueren si se borra el fallback que dicen cubrir**: sin `?? row.account_name`, la fila 1 daría `accountId: ""` en vez de `"Centro sin id"`; sin `?? "(sin nombre)"`, la fila 2 daría `name: "undefined"` (de `String(undefined)`) en vez de `"(sin nombre)"`. Ambos rompen el `toEqual` exacto del test correspondiente.
    - El refactor del `?? 0` es estructural, no cosmético: al mover la extracción dentro del filtro, el tipo de `complete` deja de admitir `null`, así que no hay ningún `??` que un futuro cambio pueda reintroducir sin que el compilador se queje primero — coherente con el patrón que el propio coder documentó en `code-notes.md`.
  - Issues: ninguno. Los 4 hallazgos de la review anterior están cerrados con el mecanismo exacto que pedían (test + refactor de tipos, no comentario ni guarda en runtime). No he encontrado nada nuevo introducido por este diff.
  - No verificable desde el código (explícito): no re-ejecuté la suite (instrucción del orquestador); el output GREEN (46/46) y la tabla de mutantes A/B/C/A' del report no los confirmé ejecutando, sólo trazando cada uno a mano contra el código actual.

**Veredicto**: approved
**Acciones requeridas del coder**: ninguna. La acción 5 de la review anterior (ASSIGNMENTS/COUNT_METRIC en las vistas) sigue pendiente para Tasks 6-8, no para este fichero.

---

### [2026-07-13] [revisor] — a3671cc..5e2138e — Task 4 `MetricPicker` (selector de métrica + línea de cobertura)

**Commits revisados**: `5e2138e` (único commit del rango; `a3671cc` es la base, fix de Task 3 ya revisado).
**Tarea/Spec**: `.superpowers/sdd/task-4-brief.md` — crear `frontend/src/components/charts/MetricPicker.tsx` con `MetricOption`, `METRIC_OPTIONS`, `SITE_METRIC_OPTIONS` (sin `COUNT_METRIC`) y el componente `MetricPicker`, código dado verbatim en el brief.

**Stage 1 — Spec compliance**: ✅
- ✓ Único fichero creado: `frontend/src/components/charts/MetricPicker.tsx`, 56 líneas (`git show 5e2138e --stat` confirma 1 file changed, 56 insertions, 0 deletions; sin tocar ningún otro fichero).
- ✓ El diff (`review-a3671cc..5e2138e.diff` líneas 17-72) es **carácter por carácter idéntico** al bloque `tsx` del Step 1 del brief (líneas 26-81) — mismos imports, mismo orden de `METRIC_OPTIONS`, mismo comentario sobre por qué `SITE_METRIC_OPTIONS` excluye `__count__`, misma implementación de `MetricPicker`.
- ✓ `MetricOption`, `METRIC_OPTIONS` (5 entradas, incluye `COUNT_METRIC`), `SITE_METRIC_OPTIONS` y el default export coinciden con la interfaz "Produces" del brief.
- ✓ `SITE_METRIC_OPTIONS = METRIC_OPTIONS.filter((o) => o.key !== COUNT_METRIC)` — verificado leyendo `chartAggregation.ts:3-7`: `COUNT_METRIC = "__count__"`, `SCREENED`/`STAGE1`/`STAGE2`/`ASSIGNMENTS` son 4 keys distintas de `COUNT_METRIC`, así que el filtro deja exactamente 4 entradas y ninguna es `__count__`. La exclusión load-bearing se sostiene.
- ✓ `data-testid="chart-coverage"` presente literal en el `<span>` (línea 63 del diff).
- ✓ Sin `any` en el fichero (grep manual sobre las 56 líneas, ninguna ocurrencia).
- ✓ Sin scope creep: ni ChartModal.tsx ni ninguna vista consumidora fueron tocados en este commit (correcto — eso es Tasks 6-8).

**Stage 2 — Code quality**:
  - Strengths:
    - **Las tres lecturas honestas de la línea de cobertura, verificadas contra `coverageFor()` real** (`chartAggregation.ts:41-45`, donde `missing = total - withData` siempre, así que `partial = coverage.missing > 0` es equivalente a `withData < total`):
      - Cobertura completa (`missing=0`): `partial=false` → `"215 de 215 centros reportan {label}"`, sin sufijo. Correcto y no ambiguo.
      - Cobertura parcial (`0 < withData < total`): `partial=true` → `"87 de 215 centros reportan {label} · 128 sin dato, excluidos"`. Coincide exactamente con el ejemplo del brief/contexto.
      - **Cobertura cero (`withData=0`, el caso que más puede engañar)**: `partial=true` (porque `missing=total>0`) → `"0 de 215 centros reportan {label} · 215 sin dato, excluidos"`. El texto dice explícitamente "0 de 215" y "215 sin dato, excluidos" — no se puede leer como que el gráfico tiene datos. Cumple el propósito declarado del componente.
    - `label` se computa una sola vez (`options.find(...)`) y se reutiliza en el texto — sin derivación repetida.
    - Cero casts, cero `any`, tipos explícitos en la firma del componente y en `MetricOption`.
    - Import `React` no usado es un falso positivo ya descartado por el coder (`tsconfig.json:7` tiene `"jsx": "react-jsx"`, sin `noUnusedLocals` en `tsconfig.json` — confirmado leyendo el fichero completo — y el mismo patrón de import no usado ya existe en `LoginBanner.tsx`, verificado con grep). No bloquea el build; consistente con el resto del repo.
  - Issues:
    - [Important] **El fallback `label = options.find(...)?.label ?? metricKey` filtra la key técnica cruda al usuario si `metricKey` no está en `options`** — escenario real y no hipotético: cualquier vista que ofrezca `SITE_METRIC_OPTIONS` (Ranking, Distribución) pero mantenga `metricKey` en un estado que todavía apunte a `COUNT_METRIC` (p.ej. al cambiar de una vista con `METRIC_OPTIONS` a una con `SITE_METRIC_OPTIONS` sin resetear el estado) renderiza literalmente `"87 de 215 centros reportan __count__"` — o, con un metric key de Salesforce, algo como `"...reportan sf.C_Number_of_Stage1_Individuals_followed__c"`. Es exactamente el string interno que el resto del componente existe para traducir a lenguaje natural. **No es un defecto de este diff** (el código es verbatim del brief, y el propio report del coder ya lo señala como riesgo para Tasks 6-8), pero sí es un hallazgo de calidad real que debe tratarse como bloqueante de spec en las tasks de las vistas: quien construya Ranking/Distribución debe resetear `metricKey` al cambiar de `options`, o `MetricPicker` seguirá mostrando claves técnicas a usuarios clínicos.
    - [Minor] 4 props (`metricKey`, `options`, `coverage`, `onChange`) técnicamente exceden la regla del repo (`CLAUDE.md`: ">3 props → agrupar en objeto"). Coincido con el juicio del coder de no forzarlo: `options`+`onChange` son configuración de UI y `metricKey`+`coverage` son estado de datos — agruparlos crearía una indirección sin ganancia de legibilidad para un control de 6 líneas. Apunto, sin embargo, que agrupar `metricKey`+`coverage` en un único prop (p.ej. `selection: { metricKey, coverage }`) habría además comunicado estructuralmente el emparejamiento que el hallazgo de abajo echa en falta — no lo pido ahora (YAGNI para Task 4), pero es una opción barata si Tasks 6-8 confirman que el emparejamiento se rompe en la práctica.
    - [Minor] Cero tests para este componente (ni siquiera de snapshot/render). El brief no los pide (sólo `npm run build`) y Vitest hoy no coge `.test.tsx` (nota ya documentada en `code-notes.md` de Task 1: `include` es sólo `src/**/*.test.ts`, entorno `node`), así que no habría infraestructura para correrlos sin trabajo adicional fuera de alcance. No bloqueante para este task, pero la lógica no trivial de este fichero (`partial`, el fallback de `label`) queda sin ningún test de caracterización — recomendable cerrarlo cuando se amplíe Vitest a jsdom para las vistas de Tasks 6-8.
  - Juicio independiente sobre el concern del coder (¿debe `MetricPicker` forzar que `coverage` corresponda al `metricKey` seleccionado?): **no, correctamente dejado a los callers.** `MetricPicker` es un componente puramente presentacional (props → JSX, sin estado, sin fetch); forzar la relación exigiría o (a) pasarle las `rows` crudas y computar `coverageFor` internamente — acoplaría el componente a `chartAggregation` y rompería la separación presentacional/lógica que el propio repo pide (`coding-style.md`: "Optimize for clarity", YAGNI) — o (b) un tipo envolvente que ate `metricKey` y `coverage` en tiempo de compilación, lo cual no impide en runtime que un caller construya ese envoltorio con datos desalineados; TypeScript no puede verificar "estos dos valores se calcularon juntos" sin efectivamente ejecutar el cálculo dentro del componente. La responsabilidad de mantener el par sincronizado es del caller, tal como en cualquier componente controlado de React (mismo patrón que `value`/`onChange` en un `<input>` controlado). El hallazgo Important de arriba es la manifestación concreta de ese riesgo y debe convertirse en un requisito explícito (y testeado) de las tasks 6-8, no en una responsabilidad nueva de este componente.

**Veredicto**: approved-with-issues
**Acciones requeridas del coder**: ninguna sobre este fichero (el Important es de diseño de contrato entre componentes, no un bug en `MetricPicker.tsx`).
**Acciones requeridas para Tasks 6-8 (orquestador, a incluir en los próximos briefs)**:
1. (Important) Cada vista que use `SITE_METRIC_OPTIONS` debe resetear/validar `metricKey` contra la lista de `options` vigente al cambiar de vista o de opciones, para que el fallback `?? metricKey` de `MetricPicker` nunca se dispare con una key técnica cruda visible al usuario.
2. (Important) `coverage` pasado a `MetricPicker` debe calcularse siempre con `coverageFor(rows, metricKey)` usando el **mismo** `metricKey` que se le pasa al componente — verificar esto explícitamente en la implementación y en los tests de cada vista (Ranking/Distribución/Embudo/país).
3. (Minor, oportunista) Si Tasks 6-8 revelan que el emparejamiento `metricKey`+`coverage` se rompe en la práctica, considerar agrupar ambos en un único prop de `MetricPicker` en vez de añadir validación en runtime.

---

### [2026-07-13] [revisor] — 5e2138e..f311710 — Tasks 6-9: las cuatro vistas del chart modal (Countries / Ranking / Distribution / Funnel)

**Commits revisados**: `a12b1bb` (CountriesView), `908177e` (RankingView), `aedd0dd` (DistributionView), `f36b8a3` (FunnelView), `f311710` (docs/code-notes). Diff verificado con `git show --stat 5e2138e..f311710`: 4 ficheros nuevos + `memory/*`, **ningún fichero existente tocado** — sin scope creep.
**Tarea/Spec**: `.superpowers/sdd/task-6-brief.md` … `task-9-brief.md`. Report del coder: `task-6-9-report.md` (no usado como fuente de verdad; todo verificado leyendo el código).

**Stage 1 — Spec compliance**: ✅ (las cuatro)
  - Task 6 CountriesView ✅ — firma `({ rows }: { rows: DataRow[] })`, `METRIC_OPTIONS`, `groupByCountry`, empty state con `data-testid="chart-empty"`. Dos desviaciones **declaradas y ambas correctas** (ver Stage 2).
  - Task 7 RankingView ✅ — verbatim del brief: `SITE_METRIC_OPTIONS`, toggle Top/Bottom, selector N (5/10/20/30), barras horizontales (`layout="vertical"`), altura `max(280, n*32)`.
  - Task 8 DistributionView ✅ — verbatim: `ComposedChart`, `XAxis tick={false}`, eje derecho `[0,100]`, línea `chart-silent-sites` con `missingSites`.
  - Task 9 FunnelView ✅ — verbatim: sin selector, empty state explícito sobre las tres etapas, línea `chart-coverage` desde `sitesIncluded`/`sitesExcluded`.
  - Extras: sólo un comentario de 2-3 líneas por vista documentando el invariante `metricKey`/`options`/`coverage`. No es scope creep; es exactamente lo que el review de Task 4 pidió que las vistas garantizaran.

**Los cuatro hazards — verificados leyendo el código, no el report**:
  1. ✅ `COUNT_METRIC` fuera de Ranking y Distribución. `RankingView.tsx:7` y `DistributionView.tsx:7` importan `SITE_METRIC_OPTIONS`; `CountriesView.tsx:9` importa `METRIC_OPTIONS`. `MetricPicker.tsx:19-21` define `SITE_METRIC_OPTIONS = METRIC_OPTIONS.filter(o => o.key !== COUNT_METRIC)`. Ninguna de las dos vistas de centro importa `COUNT_METRIC` ni `METRIC_OPTIONS` (grep confirmado).
  2. ✅ Estado inicial ∈ options. Las tres vistas con selector arrancan en `SCREENED` (`CountriesView.tsx:15`, `RankingView.tsx:13`, `DistributionView.tsx:13`). `SCREENED` está en `METRIC_OPTIONS` (`MetricPicker.tsx:10`) y, por construcción del filter, también en `SITE_METRIC_OPTIONS`. El único camino que muta `metricKey` es el `onChange` del `<select>` de `MetricPicker` (`MetricPicker.tsx:40`), cuyas `<option>` son exactamente el array `options` recibido → el estado no puede salirse de `options` ni al inicio ni tras interactuar. El fallback `?? metricKey` de `MetricPicker.tsx:31` (el Important de la review de Task 4) queda inalcanzable desde estas cuatro vistas.
  3. ✅ Tooltip de países no miente. `CountriesView.tsx:38-43`: `"<valor> (<n> centros que la reportan)"` — no "centros del país". Consistente con el JSDoc de `CountryBucket` (`chartAggregation.ts:11-17`).
  4. ✅ Sin drift de coverage. En las tres vistas, `coverage = useMemo(() => coverageFor(rows, metricKey), [rows, metricKey])` con el **mismo** `metricKey` que se pasa a `MetricPicker` (`CountriesView.tsx:16`, `RankingView.tsx:17`, `DistributionView.tsx:14`). No hay segunda fuente de coverage. FunnelView no tiene `metricKey`: su cobertura sale del propio `funnel()` (`FunnelView.tsx:12,26-29`), estructuralmente no puede desincronizarse.

**Stage 2 — Code quality**:
  - Strengths:
    - Cero `any` en los cuatro ficheros (grep). Un único cast, justificado (abajo).
    - Pareto: el fallo que el rediseño existe para evitar está evitado — `DistributionView.tsx:41` `XAxis tick={false}` (0 etiquetas de centro), y la curva acumulada va en su **propio** eje (`yAxisId="right"`, `domain={[0,100]}`, `DistributionView.tsx:43,47-54`), no compartiendo escala con los valores absolutos.
    - Empty state del embudo **honesto**: `FunnelView.tsx:16-19` nombra las tres métricas y dice "a la vez… para no inventarse caídas". Explica el porqué, no es un lienzo en blanco.
    - Vistas verdaderamente delgadas: estado + `useMemo` + chart + empty state. Toda la lógica de exclusión vive en `chartAggregation` (Tasks 2-3, ya testeado). Ninguna vista re-implementa la regla ausente≠cero.
    - `data-testid` presentes en los cuatro (`chart-empty`, `chart-silent-sites`, `chart-coverage`) — el E2E de la última task tiene dónde agarrarse.
  - Issues:
    - [Important] **Ninguna vista distingue "no hay filas" de "hay filas pero ninguna reporta esta métrica"** (`CountriesView.tsx:27-30`, `RankingView.tsx:56-59`, `DistributionView.tsx:30-33`, `FunnelView.tsx:14-21`). Con `rows = []` (búsqueda sin resultados) las tres primeras muestran *"Ningún centro del resultado actual reporta esta métrica. Prueba con otra."* — consejo falso: cambiar de métrica no arreglará un resultado vacío. Igual en Funnel. El texto es **verbatim del brief**, así que no es incumplimiento de spec, pero sí un defecto real de UX en la vista cuyo propósito declarado es *explicar por qué el gráfico está vacío*. **No verificable desde el código si el contenedor (Task 5) filtra `rows.length === 0` antes de montar las vistas**: hoy no existe ningún consumidor de estas vistas en el repo (grep sobre `frontend/src` fuera de `components/charts/`: 0 hits). Debe resolverse en Task 5/10: o el contenedor corta el caso `rows.length === 0` con su propio mensaje, o cada vista lo distingue.
    - [Important] **Cero cobertura de tests sobre los cuatro ficheros.** Vitest sólo coge `src/**/*.test.ts` en entorno `node` (sin jsdom) — los cuatro hazards descansan hoy únicamente en review. El coder lo declara y estaba fuera de alcance del brief; lo registro como deuda a cerrar en la task de E2E, que como mínimo debe cubrir: (a) empty state de cada vista, (b) que el dropdown de Ranking/Distribución NO ofrece "Número de centros", (c) que la línea de cobertura cambia al cambiar de métrica.
    - [Minor] `CountriesView.tsx:41` — `item.payload as CountryBucket` sin optional chaining, mientras el brief usaba `item?.payload?.sites`. Si Recharts llamara al formatter con un item sin `payload`, `bucket.sites` lanzaría un TypeError dentro del render del tooltip. Riesgo real ~nulo (todo item de tooltip de un `<Bar>` lleva su datum), pero es una guarda estrictamente más débil que la del brief a cambio de nada. Sugerencia: `const bucket = item?.payload as CountryBucket | undefined;` + `bucket?.sites ?? 0`, o volver al optional chaining sobre el valor ya tipado.
    - [Minor] `CountriesView.tsx:42` con `COUNT_METRIC` seleccionado (única vista que lo ofrece), el tooltip lee *"12 (12 centros que la reportan)"* — `value === sites` por construcción (`valueOf` devuelve 1 por fila). No es falso, pero es tautológico y raro. Igual la línea de `MetricPicker`: *"215 de 215 centros reportan número de centros"*. Cosmético, y su origen es el contrato de `MetricPicker` (Task 4), no este diff.
    - [Minor] `DistributionView.tsx:60` — `"1 centros no reportan…"` cuando `missingSites === 1` (sin singular). Texto del brief.
    - [Minor] `DistributionView.tsx:44` — `<Tooltip />` por defecto muestra `% acumulado : 84` sin el `%`; el `unit="%"` está en el `YAxis` (`:43`), no en la serie. Cosmético.
    - [Minor] `RankingView.tsx:61` — con N=30 la altura es 960px dentro de un modal. Si scrollea o desborda depende del contenedor de Task 5, que no existe todavía → no verificable ahora.
  - **Juicio sobre las dos desviaciones declaradas del brief (Task 6)**:
    - (a) Quitar el `item: any` del brief: **mejora, y el cast es seguro.** El repo prohíbe `any` explícitamente (`CLAUDE.md` → Code Review Standards). El cast es un downcast desde el `payload?: any` que tipan los tipos de Recharts, así que el compilador no lo verifica — pero es correcto en runtime: el `data` del `<BarChart>` es exactamente `groupByCountry(...)`, cuyo tipo de retorno es `CountryBucket[]` (`chartAggregation.ts:47`), y Recharts pone el datum crudo en `item.payload`. La única pega es la guarda perdida (Minor de arriba), no la corrección del tipo.
    - (b) `"<valor> (<n> centros que la reportan)"` en vez de `"<n> centros"`: **mejora, y necesaria.** El texto del brief se leería como "centros de este país", que es falso para toda métrica con cobertura parcial — precisamente el error que el rediseño existe para eliminar. La desviación cumple el hazard 3; el brief estaba mal.

**Veredicto**: **Spec ✅ (Tasks 6, 7, 8, 9) · Code quality: approved-with-issues**
**Acciones requeridas del coder**:
1. (Minor, en este diff) `CountriesView.tsx:41` — restaurar la guarda contra `payload` ausente sin reintroducir `any`.
2. (Important, NO en este diff — Task 5/10 o brief nuevo) Decidir dónde vive el caso `rows.length === 0` y que su mensaje no diga "prueba con otra métrica".
3. (Important, task de E2E) Cubrir los tres asserts listados arriba; hoy los cuatro hazards no tienen red de tests.

**No verificable por el revisor** (declarado, no asumido): los outputs de `npm run build`, `npx tsc --noEmit` y `npx vitest run` que el report cita — no re-ejecuté suite ni build por instrucción explícita del orquestador. El comportamiento del contenedor (Task 5) no es verificable porque no existe consumidor de estas vistas en el repo.

### [2026-07-13] [revisor] — f311710..bba7063 — Task 5 (contenedor con pestañas + `CustomView`)

**Commits revisados**: `bba7063` (único). Diff real: 2 ficheros nuevos (`charts/ChartModal.tsx` 89 L, `charts/CustomView.tsx` 381 L) + memory. `ExplorerView.tsx` NO tocado, `components/ChartModal.tsx` NO borrado — correcto según el override del orquestador (Task 10 recablea y borra).
**Tarea/Spec**: `.superpowers/sdd/task-5-brief.md` (parcialmente stale) + override del orquestador (build verde, sólo 2 ficheros nuevos).

**Stage 1 — Spec compliance**: ✅
  - ✓ Contenedor verbatim del Step 3: 5 pestañas, `ChartTab` exportado, default `"countries"`, `useEffect(...[open])` que resetea a Países en cada apertura (`charts/ChartModal.tsx:28-29`).
  - ✓ `data-testid="chart-modal"` (`:35`) y `data-testid="chart-tab-<key>"` (`:55`).
  - ✓ Guard de 0 filas load-bearing (`:72-84`): con `rows.length === 0` pinta el mensaje del Explorer y NO renderiza NINGUNA vista — verificado: las cinco vistas (incluida `custom`) están dentro del `else`. Ninguna vista es alcanzable con 0 filas.
  - ✓ `CustomView` es un MOVE fiel, no un rewrite: diff línea a línea contra `components/ChartModal.tsx` → controles Type/X/Y-series/Mode/Legend-max, botón Download, `ResponsiveContainer`, `label()`, `COLORS`, `Pill`/`Label`, normalización+cap del pie, `legendPayloadForSeries/ForPie` y los tres charts (Bar/Line/Pie) idénticos. Removals sólo los permitidos (overlay, cabecera, Close, props `open`/`onClose`/`title`/`onChangeTitle`).
  - ⚠ Extra menor no pedido: borrado de `showSliceLabels` (era código muerto real — `components/ChartModal.tsx:73,105`, asignada 2 veces, leída 0). Removal correcto, pero es una limpieza fuera del "move puro".
  - Sin scope creep. Sin `any` en código NUEVO (`custom: React.ReactNode`); los `any` de `CustomView` son pre-existentes (`:30` `Record<string, any>`, `(r: any)` del pie, `payload={... as any}`) — heredados del move, correctamente no tocados.

**Stage 2 — Code quality**:
  - Strengths: el guard de 0 filas está bien argumentado y comentado en el código (`:69-71`); el razonamiento sobre eliminar `useEffect(() => setStacked(true), [open])` es correcto (el contenedor desmonta la pestaña, `useState(true)` da el mismo inicial); el report declara honestamente los 3 concerns y no infla la verificación.
  - Issues:
    - [Critical] `charts/ChartModal.tsx:34-35` — el panel del modal es de altura NO acotada: `fixed inset-0 flex items-center justify-center` + panel `w-full max-w-5xl` SIN `max-h-*`, SIN `overflow-y-auto`, sin handler de Escape ni cierre por backdrop. El modal viejo era `h-[78vh]` (`components/ChartModal.tsx:168`) → siempre ≤ viewport. Con contenido alto (p.ej. `RankingView` con N=30: `ResponsiveContainer height={Math.max(280, data.length*32)}` = 960 px, `RankingView.tsx:61` + `:52`) el panel supera el viewport, se centra y desborda por ARRIBA y por ABAJO sin scroll: la cabecera con el botón Close queda fuera de pantalla y el usuario no puede cerrar el modal (sólo recargar). Segunda vía: `CustomView` con muchos `yCandidates` → la fila de controles envuelve y suma altura sobre los 420 px del chart. Fix: `max-h-[90vh] overflow-y-auto` en el panel (o `items-start` + `overflow-y-auto` en el overlay).
    - [Important] `charts/CustomView.tsx:14,143` — el PNG descargado pasa de `${title}_chart.png` a `chart.png` fijo. Es regresión funcional real (el usuario pierde la trazabilidad del nombre). Consecuencia forzada por el brief (quita el prop `title`), no error del coder. Fix en Task 10: bajar `filenameBase?: string` desde ExplorerView, que ya tiene el state del título.
    - [Important] `charts/CustomView.tsx:45` — `stacked` es state LOCAL y la pestaña se desmonta al cambiar de tab (`ChartModal.tsx:82`, render condicional). Hoy no hay pestañas, así que "Grouped" persiste mientras el modal está abierto; con el contenedor, ir a Ranking y volver a Personalizado revierte a "Stacked". Drift de comportamiento que el report NO menciona. `legendMaxUI` NO sufre esto (se re-inicializa del prop `legendMax`, que es controlado desde ExplorerView).
    - [Minor] `charts/CustomView.tsx:170` — el contenedor del chart pasa de `flex-1 p-4` a `h-[420px] pt-4`: (a) la altura deja de ser responsive al viewport y baja de ~590 px (78vh menos chrome) a 420 px fijos → el chart se renderiza más BAJO que hoy; (b) se pierde el padding lateral/inferior, y como `chartRef` es justo ese div, el PNG exportado sale sin márgenes. El diagnóstico del coder (`ResponsiveContainer height="100%"` colapsa a 0 sin padre con altura resuelta) es correcto, pero la solución idiomática del repo es la de las otras 4 vistas: `ResponsiveContainer height={420}` (numérico) y sin altura en el padre (`CountriesView.tsx:32`, `DistributionView.tsx:36`, `FunnelView.tsx:31`).
    - [Minor] `charts/ChartModal.tsx:35` — panel `max-w-5xl` vs `max-w-6xl` del modal actual: Personalizado se renderiza más estrecho que hoy. Viene verbatim del brief; confirmar con el diseño que es intencional.
    - [Minor] `charts/CustomView.tsx:160` — el botón Download cambia `hover:bg-white` → `hover:bg-gray-50` (correcto: ya no está sobre la cabecera `bg-gray-50`). Cosmético, sin impacto.
    - [Minor] Ninguno de los 2 ficheros tiene tests (ni de componente ni E2E). Los `data-testid` están puestos pero nadie los asserta todavía; los 46 tests verdes son de `rowAccess`+`chartAggregation` y no cubren estos ficheros.

**Concerns del coder — juicio independiente**:
  (a) `ChatView.tsx` importa el ChartModal viejo — **CONFIRMADO y real**. `pages/ChatView.tsx:4` importa `../components/ChartModal`; call site en `:1203-1217` pasa `data`/`xKey`/`yKeys`/`type`/`labelByKey` + `xCandidates={[]}` `yCandidates={[]}` y NO tiene `rows: DataRow[]`. Si Task 10 borra el fichero viejo sin recablear ChatView, **rompe el build** (import no resuelto). El contenedor nuevo no le encaja (exige `rows`). Salida limpia: ChatView pasa a usar `charts/CustomView` dentro de su propio marco de modal (necesita reponer overlay+cabecera+Close, que CustomView ya no trae). No estaba en el plan → hay que meterlo en Task 10.
  (b) El filename NO es el único cambio observable. Faltan al coder, como mínimo: el reset de `stacked` al cambiar de pestaña (Important arriba), la altura del chart 420 px fijos vs ~78vh (más bajo y no responsive), la pérdida del padding lateral/inferior en el PNG exportado, y el ancho `max-w-5xl` vs `max-w-6xl`.
  (c) El diagnóstico de `h-[420px]` es correcto (`height="100%"` sí colapsa a 0 en flujo normal) pero SÍ cambia el render: chart más bajo que hoy y sin adaptarse al viewport. Además su justificación ("la misma altura que usan las otras 4 vistas") es imprecisa: Countries=420, Distribution=400, Funnel=380 y Ranking es dinámica (280–960). Preferible alinearse con el patrón del repo: `height={420}` numérico.

**Veredicto**: Spec ✅ · Code quality **approved-with-issues** (needs work antes de Task 10: el Critical es un modal-trap).
**Acciones requeridas del coder**:
  1. [Critical] Acotar el panel: `max-h-[90vh] overflow-y-auto` (o `items-start` + overlay scrollable) en `charts/ChartModal.tsx:35`.
  2. [Important] Task 10: recablear `ChatView.tsx` ANTES/junto al borrado de `components/ChartModal.tsx`, o el build se rompe.
  3. [Important] Task 10: restaurar el nombre del PNG vía `filenameBase?: string`.
  4. [Important] Decidir si `stacked` debe sobrevivir al cambio de pestaña (subirlo a ExplorerView) o aceptarlo y documentarlo.
  5. [Minor] Cambiar `h-[420px]` + `height="100%"` por `height={420}` numérico y devolver el padding al div de `chartRef`.

**No verificable por el revisor** (declarado, no asumido): los outputs de `npm run build`, `npx tsc --noEmit` y `npx vitest run` del report — no re-ejecuté build ni suite por instrucción explícita del orquestador. El Critical de overflow está verificado LEYENDO el layout (clases Tailwind + alturas de `ResponsiveContainer` de las vistas), no renderizando en navegador. El comportamiento en runtime de ambos ficheros sigue sin consumidor en el repo (nadie importa `charts/ChartModal` todavía) → no ejercitable hasta Task 10.

### [2026-07-13] [revisor] — 0d1f1d3..cfe75c8 (rama `feat/explorer-table-scroll-resize`, 29 commits) — Review final de rama antes de merge

**Commits revisados**: `0d1f1d3..cfe75c8` (rama completa: fixes de tabla del Explorer + rediseño del chart modal).
**Tarea/Spec**: `docs/superpowers/specs/2026-07-13-explorer-charts-redesign-design.md` + `docs/superpowers/plans/2026-07-13-explorer-charts-redesign.md`.

**Stage 1 — Spec compliance**: ✅
  - (1) Tabla: `w-max min-w-full` (ExplorerView:3161) sobre el contenedor `overflow-auto` ya existente (:3137) ✓; Account Name exenta de truncado ✓; resize por arrastre con persistencia en localStorage + doble clic para reset + "Reset layout" que limpia (`LS_KEYS.columnWidths`) ✓.
  - (2) Charts: 4 vistas orientadas a preguntas + `CustomView` como 5ª pestaña ✓; módulo puro `lib/chartAggregation.ts` ✓; Vitest añadido ✓; `readDataCell` movido a `lib/rowAccess.ts` ✓; `components/ChartModal.tsx` viejo borrado, sin referencias huérfanas (grep) ✓.
  - Sin features extra fuera de spec.

**Stage 2 — Code quality**:
  - **Strengths**: la regla rectora (ausente ≠ cero) se cumple **por construcción** en las 4 vistas nuevas: todo dato entra por `valueOf` → `toMetricValue` (chartAggregation.ts:26-38), que devuelve `null` y los agregadores hacen `continue`; no queda ni un `?? 0`. `funnel()` extrae los valores DENTRO del filtro, así que la colección superviviente lleva `number`s de verdad. 26 tests de `chartAggregation` + 21 de `rowAccess` (47, verificados en verde por mí).
  - **Issues**:
    - [Important] La pestaña **Personalizado sí rellena con cero**: `buildChartDataset` row-wise (ExplorerView.tsx:2421-2423) hace `Number(String(raw ?? ''))` y `Number("") === 0` → un centro que nunca reportó pinta una barra de valor 0 idéntica a un 0 legítimo. Spec-compliant (design doc L65: "No se toca su comportamiento") y por eso NO bloquea, pero ahora vive dentro del mismo shell que 4 pestañas honestas, sin línea de cobertura. Mínimo: aviso persistente en esa pestaña. (Los sumatorios agrupados por país, :2406-2407, sí son correctos.)
    - [Important] **z-index degradado 11000 → 50** (ModalFrame.tsx:36; el viejo ChartModal era `z-[11000]`). El FAB "Show Nearby panel" (ExplorerView.tsx:3365, `z-[9050]`) pinta ENCIMA del modal cuando `nearbyActive && !nearbyPanelOpen`; al pulsarlo, el overlay del drawer (`z-[9000]`, pointer-events-auto) deja el modal inclicable (Escape lo salva). Fix: un token. El mapa NO se cuela (es `@react-google-maps/api`, `.gm-style` crea su propio stacking context — CLAUDE.md dice Leaflet y está desactualizado).
    - [Minor] Account Name redimensionada pierde el tooltip: `strVal` se fuerza a `""` para esa columna (ExplorerView.tsx:3303), así que `title` nunca se pone ni cuando el usuario la estrecha y sí recorta.
    - [Minor] `legendMax` no se subió al padre como sí se subió `stacked` (CustomView.tsx:49-50): CustomView se desmonta al cambiar de pestaña, así que "Legend max = 20" vuelve a 8 en cada ida y vuelta. Mismo bug de clase, medio arreglado.
    - [Minor] El guard `chart-no-rows` (ChartModal.tsx:67) es inalcanzable: el botón Chart está `disabled` con 0 filas (ExplorerView.tsx:3098) y `askAIAndMaybeShowChart` (:1393) es código muerto pre-existente.
    - [Minor] `BuilderModal.tsx` sólo reenvía 4 props a `ModalFrame` y añade un testId; ChatView podría usar `ModalFrame` directo.

**Veredicto**: approved-with-issues (no bloquea el merge).
**Acciones requeridas del coder**: (1) `z-50` → `z-[11000]` en ModalFrame.tsx:36. (2) Restaurar el `title` de Account Name cuando tiene ancho fijado. (3) Decidir producto: línea de aviso en Personalizado. (4) Opcional: subir `legendMax` al padre.

**No verificable por el revisor**: `npm run build` y Playwright (112/4) no re-ejecutados — sin navegador/sesión. Vitest (47 ✓) y `tsc --noEmit` (11 errores, TODOS pre-existentes en MapView/SalesforceLinker/líneas viejas de ExplorerView y ChatView, CERO en `components/charts/` ni `lib/`) SÍ re-ejecutados por mí. El solapamiento de z-index y el comportamiento de `width: max-content` están derivados leyendo CSS/DOM, no renderizando.

### [2026-07-14] [revisor] — c56dc41..c6c4b33 — El Personalizado deja de contar como cero al que no reporta (row-wise + pie)

**Commits revisados**: `cc45773` (buildChartDataset → lib/chartDataset.ts vía toMetricValue) + `c6c4b33` (toPieSlices + nota de la pestaña). Rama `feat/explorer-table-scroll-resize`, PR #12.
**Tarea/Spec**: cerrar el [Important] que yo mismo abrí en la review de rama (`0d1f1d3..cfe75c8`): la pestaña Personalizado pintaba ~180/215 centros mudos como barras de cero (`Number(String(undefined ?? ''))===0`). Regla rectora: ausente/vacío/no numérico → `null` (no aporta nada); un `0` reportado SÍ es un valor y se conserva.

**Stage 1 — Spec compliance**: ✅
  - Parseo enrutado por `toMetricValue()` de `lib/chartAggregation` — el mismo de las otras 4 vistas (chartDataset.ts:106, :35) ✓
  - `buildChartDataset` extraído de `ExplorerView.tsx` a `lib/chartDataset.ts` (puro y testeable); ExplorerView pierde el `useCallback` y sus deps (:2390-2398) ✓
  - Normalización del pie sacada de `CustomView` a `toPieSlices()` (chartDataset.ts:74-97), que era la puerta trasera del mismo bug (`Number(String(raw ?? 0))`) ✓
  - Aviso ámbar (ya falso) sustituido por nota gris precisa (ChartModal.tsx:79-88); testid renombrado `chart-custom-warning` → `chart-custom-note`, sin referencias huérfanas (grep: 0) ✓
  - Sin features extra. Trabajo COHERENTE pese a los 2 crashes del implementer: no hay medio-aplicados (único residuo: un docblock huérfano, ver Minor 1).

**Stage 2 — Code quality**:
  - **Strengths**:
    - Ninguna de las 3 rutas del constructor puede pintar un cero falso. **bar/line**: `data` va crudo del builder (CustomView.tsx:231, :278) y el builder pone `null` (chartDataset.ts:106) → Recharts no pinta rect ni dot. **pie**: `toPieSlices` descarta la fila que no reporta NINGUNA serie (chartDataset.ts:85) → fuera del DOM, de la leyenda, del total y del tope de 15.
    - El cero legítimo sobrevive en las 3 rutas: `toMetricValue(0) === 0` (chartAggregation.ts:31-32, `n < 0` no captura el 0) y `values.every(v => v===null)` no descarta un `0`. **No hay sobrecorrección.**
    - Camino agrupado (país/ciudad) coherente con `groupByCountry`: suma sólo reportadores y deja el bucket en `null` si nadie reporta (chartDataset.ts:33-41); `__count__` sigue contando TODAS las filas, con el porqué escrito (chartDataset.ts:14-22).
    - Move fiel: tope de 15 porciones + cola "Others" + `_color` gris, total multi-serie bajo `__total__` (= `PIE_TOTAL_KEY`), `showLegend ≤ 25`, payloads de leyenda — todo idéntico al original (verificado contra `git show c56dc41:...CustomView.tsx`: bar/line ya usaban `data`, no `plotData`). Cero `any` nuevo en `chartDataset.ts`.
    - Los tests PINAN la regla en las dos direcciones. Verificado por mí con 2 mutaciones sobre copias desechables: (a) restaurar el parseo viejo → 5 rojos; (b) `n <= 0 → null` (tirar el cero legítimo) → 4 rojos.
  - **Issues**:
    - [Minor] Docblock huérfano: el comentario de `buildChartDataset` quedó pegado sobre `PIE_TOTAL_KEY` (chartDataset.ts:50-58) y la función (:99) se queda sin doc. Residuo de los crashes; cosmético.
    - [Minor] `toMetricValue` anula los NEGATIVOS, y ahora es el parser del constructor genérico. En el Explorer es casi inalcanzable (lat/lng están en `EXCLUDED_VISIBLE_COLUMNS`, ExplorerView.tsx:411-416), pero el pie de **ChatView** come tablas del LLM donde una columna delta/diferencia es plausible: antes se pintaba, ahora la fila desaparece sin decir nada.
    - [Minor] El fixture E2E (charts.spec.ts:16-25) NO tiene ningún centro con un `0` reportado, así que CHART-5 (3 sectores) seguiría verde si el fix hubiera tirado los ceros legítimos. Lo cubren los unit tests, pero el E2E no pinza esa dirección.
    - [Minor] Con `__count__` marcado JUNTO a una métrica, el pie no excluye al mudo: su `__count__` (=1) impide el `every(null)`, y aparece como porción de tamaño 1. No es un cero mentiroso y es semántica pre-existente de mezclar "Count (rows)" con una métrica, pero la nota de la pestaña ("aparece como hueco") no es universal en esa combinación.
    - [Minor] Mensaje vacío bilingüe en el mismo componente: "Select at least one Y series." vs "Ningún centro reporta esta métrica…" (CustomView.tsx:77-82).

**Veredicto**: **approved** — no bloquea el merge. Ningún Critical, ningún Important.
**Acciones requeridas del coder**: ninguna bloqueante. Sugeridas: (1) mover el docblock a `buildChartDataset`; (2) añadir un centro con `0` reportado al fixture E2E; (3) decidir si los negativos deben caerse en silencio en el pie de ChatView.

**Verificado por mí**: `npx vitest run src/lib/` → 58/58 ✓ (3 ficheros). `tsc --noEmit` → errores pre-existentes en MapView/SalesforceLinker/ChatView/ExplorerView(líneas viejas), **cero** en `lib/chartDataset.ts`, `CustomView.tsx`, `ChartModal.tsx`. Mutation testing (2 mutantes) sobre copias temporales, borradas después (`git status` limpio).
**No verificable por el revisor**: Playwright (sin navegador/sesión en este entorno) — CHART-5 no re-ejecutado; el conteo de `.recharts-pie-sector` está leído del código, no renderizado. Que Recharts no pinte rect para un `null` está derivado de su contrato (`Rectangle` devuelve null sin `y` numérico), no observado en un DOM real.
