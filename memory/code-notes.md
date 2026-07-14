### [2026-07-14] [coder] — `row.data` sólo trae lo que alguien PIDIÓ: la búsqueda en bloque devuelve las métricas a `null` y el fill perezoso es su única fuente

**Contexto**: bug encontrado conduciendo la app contra el backend de PRODUCCIÓN — ningún test lo pescó. `frontend/src/pages/ExplorerView.tsx` (efecto AUTO-FILL), nueva `lib/fillColumns.ts`, nueva `tests/e2e/charts-fill.spec.ts` (commits 3648584, 056e6ec).

**Gotcha/Patrón**:
1. **`row.data` NO contiene los valores de todas las columnas: contiene las que el usuario tiene VISIBLES.** El pipeline real tiene dos etapas y sólo la segunda trae datos pesados. (a) `explorerSearch` (`lib/api.ts:341-348`) manda las ~360 claves pero **strippea el prefijo `sf.`** antes del POST; el backend devuelve esas `sf.*` a `null` en las 215 filas. (b) Los valores llegan DESPUÉS, perezosamente, por el efecto AUTO-FILL → `explorerFillColumns` → `POST /api/explorer/columns/fill`, que **sí** manda las columnas con su prefijo intacto (`lib/api.ts:554`). Y ese fill construía `reqCols` **sólo desde `visibleColumns`**. Consecuencia: una métrica que no sea columna de la tabla es `null` en todas las filas, PARA SIEMPRE. Las 4 vistas nuevas del ChartModal agregan desde `row.data` → con las columnas por defecto anunciaban **"0 de 215 centros reportan individuals screened"** cuando la verdad son **81**. Regla: **cualquier feature que lea `row.data` para una clave que no sea columna visible tiene que meter esa clave en el fill** (`lib/fillColumns.ts` → `buildFillColumns`), o leerá `null` y creerá que el dato no existe.
2. **Un fixture que regala los datos convierte el test en teatro.** Los 63 unitarios, los 118 E2E y tres revisores dieron verde sobre este bug porque TODOS los fixtures sirven filas que ya traen los valores en `row.data` (`charts.spec.ts` L22-33). Ninguno reproducía la forma del backend real. El fixture correcto (`charts-fill.spec.ts`) es un contrato: la búsqueda en bloque devuelve las claves a `null`, y el mock del fill **sólo entrega las columnas que se le piden explícitamente** — así un valor sólo puede aparecer en pantalla si el código lo pidió. Con el fixture viejo, un frontend que jamás habla con el servidor pasa igual. **Al escribir un fixture, la pregunta no es "¿tiene los datos que la vista necesita?" sino "¿puede este fixture distinguir el código correcto del roto?"**.
3. **`extra.AssignmentsCount` viene con `0` por defecto del backend** (`salesforce_extras.py:372`, dict de defaults). No es un hueco: es un recuento de registros relacionados, siempre conocible. Su cobertura será siempre "215 de 215" — correcto, pero no lo leas como que la métrica está mejor reportada que las otras.

**Por qué importa**: es el fallo más grave posible en este rediseño — la línea de cobertura existe precisamente para no mentir, y estaba mintiendo *con un número*, que es peor que el bug original (un gráfico vago). Y era invisible: sin error, sin warning, y con toda la suite en verde. El único modo de haberlo visto era conducir la app contra prod o tener un fixture con la forma del backend de verdad.

**Dónde aplicar**: 1 → `pages/ExplorerView.tsx` y toda vista/feature que lea `row.data` (charts, export, "Ask Moby", agregados). 2 → **todo** fixture de `tests/e2e/` de este repo. 3 → las vistas que ofrezcan `ASSIGNMENTS` como métrica.

---

### [2026-07-14] [coder] — Dos parsers de métrica que divergen A PROPÓSITO, y el E2E corre contra un `dist` PRECOMPILADO (el mutation check miente sin `npm run build`)

**Contexto**: fix de negativos en el constructor genérico — `frontend/src/lib/chartDataset.ts` (`toDatasetValue`, `toPieSlices`), `chartAggregation.ts` (solo JSDoc), `components/charts/CustomView.tsx`, `tests/e2e/charts.spec.ts` (commits 7e19ee4, 9800057, d250657).

**Gotcha/Patrón**:
1. **`toMetricValue` (chartAggregation) y `toDatasetValue` (chartDataset) NO se pueden unificar.** Comparten "ausente/vacío/no-numérico → null", pero difieren en el negativo: `toMetricValue` lo anula (sus 4 vistas miran recuentos de pacientes — "cribados = -3" es dato corrupto) y `toDatasetValue` lo conserva (el constructor genérico pinta columnas ARBITRARIAS — la tabla de Moby trae deltas, y un `-12` es un valor). Reutilizar el primero en el segundo hacía DESAPARECER la fila del gráfico sin mensaje. Ambas definiciones llevan ahora un ⚠️ explicando por qué divergen: quien las "unifique helpfully" romperá una de las dos semánticas en silencio.
2. **El negativo en un pie: excluir + CONTAR, nunca excluir a secas.** Un sector de ángulo negativo no es geometría, y colarlo en el denominador da porcentajes > 100%. Pero "no pintable" ≠ "no existe": `toPieSlices` devuelve `{slices, negativeExcluded}`, y el contador es lo que OBLIGA a la UI a explicar la exclusión. Trampa asociada, encontrada revisando mi propio diff: con TODAS las filas negativas el pie queda vacío y el empty state decía "ningún centro reporta esta métrica" — falso, sí reportan. **Cada vez que una vista descarta filas, el mensaje de vacío tiene que distinguir POR QUÉ está vacía** (sin dato ≠ dato no dibujable), o la misma mentira vuelve por otra puerta.
3. **⚠️ El E2E de este repo corre contra un `dist` PRECOMPILADO, no contra un dev server.** `playwright.config.ts` apunta a `http://localhost:8080` y **no tiene bloque `webServer`**: se engancha a un server ya levantado que sirve `/assets/index-<hash>.js`. Consecuencia brutal para los mutation checks: **si mutas la fuente y NO corres `npm run build`, el mutante nunca llega al navegador y el test pasa**. Me pasó: leí "7 passed" y estuve a punto de concluir que la aserción no tenía dientes, cuando lo que no tenía efecto era el mutante. Todo mutation check sobre E2E aquí es: mutar → `npm run build` → `playwright test` → restaurar → `npm run build`. Comprobación de que el bundle servido es el tuyo: `grep -l "<string nuevo>" dist/assets/*.js`. (Corolario: **tras cambiar código fuente hay que rebuildear antes de correr el E2E**, o estás testeando el build anterior.)

**Por qué importa**: (1) y (2) son fallos que pintan un gráfico perfecto y mienten — sin error, sin warning, solo una fila menos. (3) invalida en silencio cualquier experimento de mutación sobre el E2E: te hace creer que un test es débil (o fuerte) sin base, que es peor que no medirlo.

**Dónde aplicar**: 1-2 → `lib/chartDataset.ts`, `lib/chartAggregation.ts` y toda vista de `components/charts/`. 3 → **todo** mutation check y toda corrida de `tests/e2e/` de este repo.

---

### [2026-07-13] [coder] — Fixes finales de rama: el z-index de un modal es un contrato con los overlays vecinos, y el estado local de una vista que se desmonta es una fuga silenciosa

**Contexto**: los 4 hallazgos de la revisión final de `feat/explorer-table-scroll-resize` (commits 9119403, 838056e, 9f04c1d, c56dc41) — `ModalFrame.tsx`, `ChartModal.tsx`, `CustomView.tsx`, `ExplorerView.tsx`, `ChatView.tsx`.

**Gotcha/Patrón**:
1. **El `z-50` de Tailwind NO es un default seguro en esta app.** El Explorer tiene una escala propia sin documentar: overlay del drawer Nearby `z-[9000]` (con `pointer-events-auto`), drawer `z-[9010]`, pill flotante "Nearby panel" `z-[9050]`. Un modal por debajo de 9050 no se rompe al abrirlo — se rompe solo si antes hubo una búsqueda Nearby y el panel está cerrado, que es cuando el pill existe. Al extraer un modal a un componente compartido, el z-index viaja con él: `ModalFrame` heredó `z-50` en vez del `z-[11000]` del modal viejo. **Cualquier `fixed` nuevo en ExplorerView se compara contra esa escala, no contra `z-50`.**
2. **Estado local en una vista que el padre desmonta condicionalmente = pérdida silenciosa.** `CustomView` vive en `{tab === "custom" && ...}`: cada ida y vuelta de pestaña la remonta. `stacked` ya se había subido al dueño por eso, pero `legendMax` se quedó con `useState` local + `useEffect` de sincronía — el patrón que *parece* controlado y no lo es (el prop solo empuja hacia dentro; la elección del usuario no sale). Regla: si un componente se monta condicionalmente, sus props de configuración son **obligatorias y controladas**, sin default ni espejo interno. Quitarle el `= 8` a `legendMax` es lo que fuerza a los dos dueños a declararlo.
3. **Mock de `/api/explorer/fields` en E2E: tiene que ser `{fields: [...]}`, no un array pelado.** `getExplorerFields` (`lib/api.ts:317`) hace `pick(raw, "fields", [])`; con un array pelado el catálogo sale vacío, `visibleColumns` se filtra contra él y la tabla se queda **sin columnas dinámicas** — incluida Account Name. `charts.spec.ts` mockea un array pelado y pasa igual (el chart no depende de `fieldDefs`), así que el mock roto es invisible hasta que un test mira la tabla. Ojo al copiar ese `beforeEach`.

**Por qué importa**: los tres son fallos que no se ven en la pantalla feliz. El z-index solo se manifiesta tras un flujo previo (Nearby); el `legendMax` solo al volver de otra pestaña; el mock roto pasa los tests que no miran la tabla. Ninguno lo pesca Vitest — no corre `.tsx` — así que la red es el E2E, y solo si el fixture es correcto.

**Dónde aplicar**: 1 → todo `fixed` nuevo en `pages/ExplorerView.tsx` y `components/charts/`. 2 → cualquier vista montada condicionalmente (las 5 pestañas del ChartModal). 3 → todo `beforeEach` de E2E que llegue a la tabla del Explorer.

---

### [2026-07-13] [coder] — Tasks 6-9: las 4 vistas del chart modal cierran los agujeros que `MetricPicker` deja abiertos (metricKey ∈ options, coverage del mismo metricKey)

**Contexto**: Tasks 6-9 del rediseño del chart modal — `CountriesView` (a12b1bb), `RankingView` (908177e), `DistributionView` (aedd0dd), `FunnelView` (f36b8a3) en `frontend/src/components/charts/`.

**Gotcha/Patrón**:
1. **El invariante que ninguna type ata: `metricKey` ∈ `options` y `coverage = coverageFor(rows, metricKey)` con ESE metricKey.** El patrón que lo garantiza en las 3 vistas con selector: `useState(SCREENED)` + `useMemo(() => coverageFor(rows, metricKey), [rows, metricKey])` declarados juntos, y el metricKey sólo cambia por el `onChange` del `<select>` (cuyas opciones SON `options`). Si alguien mete otra vía de cambio de metricKey (deep-link, prop del contenedor, "recordar última métrica" en localStorage), tiene que validar contra `options` o `MetricPicker` empezará a enseñar `sf.C_Number_of_..._c` como etiqueta y una cobertura falsa. `SCREENED` es seguro como estado inicial en las cuatro porque está tanto en `METRIC_OPTIONS` como en `SITE_METRIC_OPTIONS`.
2. **`COUNT_METRIC` sólo en CountriesView.** Ranking y Distribución importan `SITE_METRIC_OPTIONS`. Rankear centros por "nº de centros" es una lista de unos; su Pareto es una recta.
3. **El brief de Task 6 traía `item: any` en el `formatter` del Tooltip de Recharts.** Se sustituye por `item.payload as CountryBucket` importando el tipo — el `payload` de Recharts es `any` en sus typings, así que el cast es el punto donde se recupera el tipo. Y ojo con el texto: `CountryBucket.sites` NO es "centros del país", son los que reportan la métrica; el tooltip dice "centros que la reportan" para que no se lea como el total.
4. **FunnelView no lleva `MetricPicker`** (resuelve la duda que dejó abierta la nota de Task 4): el embudo son siempre las 3 etapas, no hay metricKey, y la línea de cobertura se construye a mano desde `sitesIncluded`/`sitesExcluded`. Al no haber metricKey, es la única vista que no puede desincronizarse.

**Por qué importa**: la mayoría de los 215 centros no reportan estas métricas. Estas vistas son la capa donde la exclusión se hace VISIBLE (línea de cobertura + empty states que explican el porqué). Un metricKey fuera de `options` o un `coverage` calculado con otra métrica producen una pantalla que se pinta perfecta y miente — y Vitest aquí no corre `.tsx`, así que ningún test lo pesca.

**Dónde aplicar**: `frontend/src/components/charts/*` y el contenedor con tabs que las monte (Task 10).

---

### [2026-07-13] [coder] — Task 4 `MetricPicker`: el prop contract no ata `coverage` a `metricKey`, y `funnel()` no tiene hueco para un selector

**Contexto**: Task 4 del rediseño del chart modal — `frontend/src/components/charts/MetricPicker.tsx` (commit 5e2138e), componente compartido que consumirán las 4 vistas de Tasks 6-9.

**Gotcha/Patrón**:
1. **Nada dentro de `MetricPicker` garantiza que `coverage` fue calculado para el mismo `metricKey` que se le pasa.** El componente confía ciegamente en el caller: si alguna vista pasa `coverage = coverageFor(rows, SCREENED)` junto con `metricKey = STAGE1`, la frase sale con seguridad absoluta y totalmente falsa ("87 de 215 centros reportan Stage 1..." cuando en realidad esos 87 reportan cribados). Cuando se conecten las vistas reales (Ranking/Distribución/Funnel), quien las escriba debe recalcular `coverage` en el mismo `useMemo`/render que decide `metricKey`, no guardarlos por separado.
2. **`funnel()` no tiene un `metricKey` seleccionable** (agrega SCREENED+STAGE1+STAGE2 fijos), así que no está claro que la vista Funnel vaya a usar `MetricPicker` tal cual — puede que solo necesite construir un `Coverage` a mano desde `Funnel.sitesIncluded`/`sitesExcluded` sin dropdown. Decisión pendiente para esa task.
3. **`SITE_METRIC_OPTIONS` excluye `COUNT_METRIC`** (filtro sobre `METRIC_OPTIONS`) porque rankear/pareto por "número de centros" no significa nada — ver nota de Task 3 sobre `distribution(rows, COUNT_METRIC)`. Es el mismo invariante, ahora reflejado en la capa de UI.

**Por qué importa**: el patrón "ausente≠cero" de Tasks 2-3 solo protege el cálculo; la capa de UI puede seguir mintiendo si el caller desincroniza `metricKey` y `coverage`. Ningún test de este componente pesca ese caso (Vitest no corre `.tsx`, ver nota de Task 1) — la única red es disciplina en las vistas que lo consuman.

**Dónde aplicar**: `frontend/src/components/charts/MetricPicker.tsx` y las 4 vistas de Tasks 6-9 que lo monten.

---

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

### [2026-07-13] [coder] — Task 5: el `ChartModal` viejo lo importan DOS páginas, y el marco del modal se llevaba puesta la altura del chart

**Contexto**: `frontend/src/components/charts/{ChartModal,CustomView}.tsx` — contenedor con pestañas + democión del constructor genérico a la pestaña "Personalizado".

**Gotcha 1 — el borrado de Task 10 rompe el build si sólo mira ExplorerView**: `components/ChartModal.tsx` lo importan **`ExplorerView.tsx:18` Y `ChatView.tsx:4`** (call site `:1203`). El plan asume un único consumidor. Además ChatView no tiene `rows: DataRow[]` — pasa `data`/`xKey`/`yKeys` ya agregados — así que el contenedor nuevo (que exige `rows`) no le encaja: o consume `charts/CustomView` dentro de su propio marco, o se queda con una copia. `grep -rn "ChartModal" frontend/src` antes de borrar nada.

**Gotcha 2 — `<ResponsiveContainer height="100%">` muere al salir del modal**: en el modal viejo su padre era `flex-1` dentro de un panel `h-[78vh]`, o sea altura resuelta. Dentro de una pestaña, `flex-1` no resuelve a nada y el chart colapsa a 0px sin error ni warning. Al mover un chart de un contenedor con altura fija a uno de flujo normal hay que **darle altura explícita al padre** (`h-[420px]`, la misma que usan las otras 4 vistas).

**Gotcha 3 — quitar un prop puede llevarse un comportamiento colateral**: `title` no sólo pintaba la cabecera, también nombraba el PNG descargado (`${title}_chart.png`). Al subir el título al contenedor, la descarga se queda con nombre fijo. Un prop "de presentación" puede tener un segundo uso enterrado 100 líneas más abajo: `grep` el prop entero antes de borrarlo, no sólo el sitio obvio.

**Por qué importa**: los tres son fallos silenciosos — build roto en el peor momento (Gotcha 1), chart invisible sin error en consola (Gotcha 2), regresión de UX que nadie nota hasta que un usuario se queja de sus descargas (Gotcha 3).

**Dónde aplicar**: Task 10 (obligatorio leer Gotcha 1 antes de borrar). Gotchas 2 y 3, todo el repo.

### [2026-07-13] [coder] — Task 5 fix de revisión: sacar `overflow-y-auto` del panel y ponerlo solo en el contenido, no en todo el `flex-col`

**Contexto**: `frontend/src/components/charts/ChartModal.tsx`, fix del CRITICAL de revisión (commit 58bd022) — panel del modal sin `max-h` atrapaba al usuario (Ranking con N=30 empuja el chart a 960px y el botón Close se va del viewport, sin scroll ni Escape para salir).

**Gotcha/Patrón**: la tentación obvia es poner `max-h-[90vh] overflow-y-auto` en el panel entero. Eso scrollea header + pestañas + contenido juntos — el botón Close se sigue yendo fuera de vista según cuánto haya scrolleado el usuario, exactamente el bug que se quiere arreglar. El fix correcto: el panel es `flex flex-col max-h-[90vh]` (sin overflow), header y barra de pestañas llevan `shrink-0` (nunca se comprimen, nunca scrollean), y el `overflow-y-auto` va solo en el div de contenido interior. Con flexbox, un hijo con `overflow: auto` recibe automáticamente `min-height: 0` (min-size automático de flex items), así que no hace falta `min-h-0` explícito para que el `max-h` del padre se respete — verificado con codex, no es necesario añadirlo a mano en navegadores modernos.

**Segundo gotcha, más chico**: al perder la altura fija del wrapper (`h-[420px]` → `p-4` a secas, para que `<ResponsiveContainer height={420}>` tome el numérico como los siblings), el placeholder de "Select at least one Y series" que usaba `h-full` colapsa a 0px porque ya no hay una altura de padre de la que heredar. Hay que darle una altura explícita propia (`h-[420px]`) en vez de depender del padre.

**Por qué importa**: un fix de "modal atrapa al usuario" que solo mueve el overflow al panel entero deja el mismo bug con otra forma — el botón Close sigue sin ser alcanzable de forma predecible. La combinación shrink-0 (chrome fijo) + overflow solo en el contenido es la que realmente garantiza que Close esté siempre en pantalla.

**Dónde aplicar**: cualquier modal/panel de este repo con altura variable por contenido (charts, tablas). El patrón "chrome fijo con shrink-0 + scroll solo en el contenido" es preferible a "todo el panel scrollea".

### [2026-07-14] [coder] — El array de respaldo (`fullRows`) NO es lo que la tabla enseña: TanStack filtra en cliente encima

**Contexto**: `frontend/src/pages/ExplorerView.tsx`, fix del hallazgo del review de Codex en el PR #12 — el `ChartModal` recibía `rows={nearbyActive ? fullNearbyRows : fullRows}` y las 4 vistas nuevas agregaban filas que el usuario acababa de filtrar fuera y no podía ver.

**Gotcha/Patrón**: en el Explorer conviven DOS capas de filtrado y es fácil olvidar la segunda. (1) El `FilterGroup` que va al servidor y produce `fullRows` / `fullNearbyRows`. (2) Los filtros de CLIENTE de TanStack — búsqueda global + `filter…` por columna — que estrechan la tabla **sin volver a pedir nada al servidor**. El contador `N results`, el `Export filtered (TSV)` y la paginación ya salían de `table.getFilteredRowModel()`; el badge `Chart NNN`, el `ChartModal` y `buildChartDataset` salían del array de respaldo. Resultado: buscas "IT", la tabla enseña 2 centros, el badge dice 6 y la cobertura anuncia "4 de 6 centros reportan". La fuente de verdad para cualquier cosa que hable de "las filas actuales" es `table.getFilteredRowModel().rows.map(r => r.original)`.

**Y el `.original` no pierde columnas**: `fullRows` existe porque lleva TODAS las columnas (no solo las visibles), y la primera reacción es pensar que el row model de la tabla solo lleva las visibles. No: el row model se construye sobre `viewRows` (que ES `fullRows`), y `row.original` son **los mismos objetos `ExplorerRow`** — la visibilidad de columna solo afecta al render, no al `original`. Así que `readDataCell(row, key)` sigue encontrando cualquier `sf.*` / `qual.*` aunque su columna esté oculta. La rama `nearbyActive` sale gratis: `viewRows` ya conmuta con ella.

**Por qué importa**: un gráfico que agrega una población distinta de la que la tabla enseña no es un bug cosmético — la línea de cobertura ("87 de 215 centros reportan…") es una afirmación cuantitativa sobre un conjunto que el usuario no puede auditar. Y el fallo es silencioso: no hay error, solo números que no cuadran con la tabla de al lado.

**Dónde aplicar**: todo `ExplorerView.tsx`. Cualquier feature nueva que diga "las filas actuales" (exportar, mandar a Moby, graficar, contar) tiene que salir de `getFilteredRowModel()`, no de `fullRows`. OJO: el botón "Ask Moby" (`activeRows = nearbyActive ? fullNearbyRows : fullRows`) sigue mandando el array de respaldo — fuera del alcance de este fix, pero es el mismo bug esperando.

### [2026-07-14] [coder] — En Recharts la animación de entrada es lo que hace EXISTIR la marca, y la mueve `requestAnimationFrame`

**Contexto**: `frontend/src/components/charts/*` — bug de "cambias de pestaña en el ChartModal y el gráfico sale EN BLANCO". Ejes, rejilla, leyenda y hasta el `<path>` de la línea acumulada del Pareto renderizaban; las barras, no. En el DOM: `<g class="recharts-bar-rectangle">` **sin `<path>` dentro** (20/20 vacíos), y la línea con `stroke-dasharray: 0px, 1115.33px`.

**Gotcha/Patrón**: la animación de entrada de Recharts **no es decoración**. `<Bar>` dibuja interpolando su alto de 0 a su valor, y `<Rectangle>` devuelve `null` cuando el alto es 0 — así que mientras la animación esté en t=0 el grupo está **hueco**, no "pequeño". Quien mueve esa interpolación es react-smooth con `requestAnimationFrame` (`react-smooth/es6/setRafTimeout.js`). Y rAF **no entrega frames** en un tab de fondo, ocluido, o con el hilo principal saturado. Sin frames → animación clavada en t=0 → cero marcas, para siempre. React sigue pintando el resto porque su scheduler usa MessageChannel, no rAF: **por eso los ejes salen y los datos no**, que es la firma exacta del fallo.

Lo que NO era (ambos descartados empíricamente, no por lectura de código): (a) montaje a 0×0 del `ResponsiveContainer` — el `.recharts-surface` medía 1120px y el `d` de la línea tenía largo real; (b) el doble montaje de `React.StrictMode` — el bug no reproduce ni contra el dev server (StrictMode activo) ni contra `dist`, con frames sanos.

El contenedor con pestañas es lo que **multiplicó la exposición**: el modal viejo montaba UN gráfico siempre montado (animaba una vez, al abrir); el nuevo re-monta un gráfico en CADA cambio de pestaña, así que reabre esa ventana una y otra vez.

**Cómo se testea sin escribir teatro**: contar `.recharts-bar-rectangle` da **verde contra un panel vacío** — los grupos siempre están en el DOM. Hay que medir **geometría** (`getBBox()` del hijo `path`/`rect`, ancho y alto > 0). Y aun así, con frames normales el bug **no reproduce en headless**: el test honesto tiene que **matar `requestAnimationFrame`** (`page.addInitScript` antes del `goto`) para modelar el tab sin frames. Ver `tests/e2e/charts-animation.spec.ts` (CHART-11/12), que sin el fix da `painted: 0` y `"0px, 758.906px"`.

**Ojo con el poll**: `ResponsiveContainer` no pinta nada hasta que su ResizeObserver le dice cuánto mide, así que una lectura seca justo tras el click de pestaña lee un DOM legítimamente vacío. Hay que hacer `expect.poll`. Eso NO ablanda el test: una animación congelada sigue en 0 tras el timeout entero (verificado revirtiendo el fix con el poll ya puesto).

**Por qué importa**: el usuario ve un panel vacío y concluye "no hay datos" — la mentira más cara que puede contar este modal, y la misma clase de fallo silencioso que el resto de `chartAggregation`. Y ningún test lo pillaba: 68 unitarios y 125 E2E en verde.

**Dónde aplicar**: **todo gráfico de Recharts de este repo**. Cualquier `<Bar>`, `<Line>` o `<Pie>` nuevo lleva `{...NO_ENTRY_ANIMATION}` (`components/charts/chartDefaults.ts`), que además centraliza el porqué en un único sitio en vez de en siete comentarios que divergen. Un gráfico cuyas marcas dependan de que llegue un frame es un gráfico que puede salir vacío.
