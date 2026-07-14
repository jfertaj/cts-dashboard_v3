# Rediseño de los gráficos del Explorer

**Fecha:** 2026-07-13
**Estado:** aprobado (pendiente de plan de implementación)
**Componente:** `frontend/src/components/ChartModal.tsx` (404 líneas) + estado de gráfico en `ExplorerView.tsx`

## El problema

El gráfico actual es un **constructor genérico**: eliges una columna para el eje X, marcas
series numéricas para el eje Y, y eliges bar / line / pie. `ExplorerView` le pasa como X por
defecto `sf.Account.Name`.

Esa decisión es la que rompe el gráfico. Account Name tiene **cardinalidad 215**: el eje X
intenta pintar 215 etiquetas rotadas y se convierte en una mancha ilegible, las barras quedan
de un píxel, el eje Y se escala a 8000 por un par de outliers (así que el 95% de las barras
son invisibles) y la leyenda se solapa con el eje.

No es un problema estético. Es que el gráfico por defecto agrega por la única dimensión que
nunca deberías graficar directamente.

## Las preguntas que el rediseño debe responder

1. **Comparar países** — centros, cribados, Stage 1 / Stage 2 por país.
2. **Ranking de centros** — quién recluta más y quién menos.
3. **Distribución y outliers** — cómo se reparte el volumen; qué centros están parados.
4. **Embudo del reclutamiento** — cribados → Stage 1 → Stage 2; dónde se cae la gente.

## La restricción que manda: los datos son escasos

**La mayoría de los 215 centros NO rellenan** `Individuals screened`, `Stage 1 followed` ni
`Stage 2 followed`. Solo un puñado tiene números.

Esto tiene una consecuencia que atraviesa todo el diseño: **un ranking de "quién criba más"
mide sobre todo quién rellena el campo, no quién recluta.**

### Decisión: subconjunto honesto, nunca relleno con cero

Rellenar los huecos con `0` es la salida fácil y es una mentira: un centro que no ha
reportado aparecería idéntico a uno que no ha reclutado a nadie. **Prohibido.**

En su lugar, cada vista:

- Pinta **solo** los centros con dato para la métrica elegida.
- Declara la cobertura de forma prominente, no como nota al pie:
  `"87 de 215 centros reportan cribados · 128 sin dato, excluidos"`.
- Si la cobertura es 0, muestra un estado vacío que lo explica, en vez de un gráfico en blanco.

**Corolario de diseño:** con esta escasez, *"quién no reporta"* es probablemente la pregunta
más accionable. Por eso la vista de Distribución hace doble trabajo: reparto del volumen **y**
bloque explícito de centros silenciosos.

## Diseño

Todas las vistas operan sobre **las filas actualmente filtradas** en el Explorer — las mismas
que el badge `Chart 215` anuncia y que el contador `N results` cuenta.

**"Filtradas" quiere decir DOS capas, no una.** Esto es exactamente lo que se pasó por alto al
implementar: cuentan el `FilterGroup` que se manda al servidor **y** los filtros de CLIENTE que
la tabla aplica encima sin volver a pedir nada — la búsqueda global y los `filter…` por
columna. La fuente de verdad es `table.getFilteredRowModel()`, la misma de la que ya salían el
contador de resultados y el export TSV; **no** el array de respaldo (`fullRows` /
`fullNearbyRows`), que sólo lleva la primera capa. Alimentar las vistas del array de respaldo
es lo que hacía que el usuario estrechase la tabla a 12 filas, abriese el gráfico y la
cobertura le anunciara "87 de 215 centros reportan": una población que no puede ver.

### Las cinco pestañas

| Vista | Qué pinta | Por qué así |
|---|---|---|
| **Países** | Barras por país (~20 categorías) | Agrega los 215 centros en ~20 países: legible por construcción. Es la vista por defecto al abrir. |
| **Ranking** | Top-N y bottom-N centros, barras **horizontales** | N configurable (defecto 10). Horizontal porque los nombres de centro son largos y en vertical no se leen. |
| **Distribución** | **Pareto**: barras de centros ordenadas de mayor a menor + línea de % acumulado, más un bloque aparte con el recuento de centros sin dato | El Pareto responde "¿5 centros hacen el 80%?" de un vistazo. Responde outliers y silenciosos a la vez. |
| **Embudo** | Cribados → Stage 1 → Stage 2 | Solo sobre el subconjunto que reporta **las tres** métricas. La cabecera lo dice. |
| **Personalizado** | El constructor actual, intacto | Escotilla de escape para lo que no cubran las cuatro. No se toca su comportamiento. |

### Selector de métrica

Presente en Países, Ranking y Distribución. Arranca en **cribados**.

| Etiqueta | Clave |
|---|---|
| Individuals screened (total) | `sf.C_Number_of_Individuals_screened_intotal__c` |
| Stage 1 individuals followed | `sf.C_Number_of_Stage1_Individuals_followed__c` |
| Stage 2 individuals followed | `sf.C_Number_of_Stage2_Individuals_followed__c` |
| Assignments (count) | `extra.AssignmentsCount` |
| Número de centros | `__count__` (siempre 100% de cobertura) |

`__count__` **solo se ofrece en la vista de Países**: contar centros por país tiene sentido,
pero "rankear centros por número de centros" no significa nada (cada centro vale 1). En
Ranking y Distribución el selector no lo lista.

Junto al selector va **siempre** la línea de cobertura de esa métrica. Cambiar de métrica
cambia la cobertura, y eso tiene que verse.

### Estructura de ficheros

`ChartModal.tsx` ya mezcla render y controles en 404 líneas; meterle cuatro vistas dentro lo
volvería inmanejable. Se parte en:

```
components/charts/
  ChartModal.tsx        contenedor + pestañas (delgado)
  CountriesView.tsx
  RankingView.tsx
  DistributionView.tsx
  FunnelView.tsx
  CustomView.tsx        el constructor actual, movido tal cual
  MetricPicker.tsx      selector de métrica + línea de cobertura
lib/chartAggregation.ts  agregación pura, sin React
```

`lib/chartAggregation.ts` es donde vive la lógica que de verdad hay que probar:

- `coverageFor(rows, metricKey)` → `{ withData, total, missing }`
- `groupByCountry(rows, metricKey)` → excluyendo filas sin dato
- `topN(rows, metricKey, n)` / `bottomN(...)`
- `distribution(rows, metricKey)` → acumulado ordenado + recuento de silenciosos
- `funnel(rows)` → solo filas con las tres métricas

Al ser funciones puras sobre arrays de filas, se testean sin montar React ni tocar la red.

## Estados de error y vacío

- **Cobertura 0 para la métrica elegida:** estado vacío explicando que ningún centro filtrado
  reporta esa métrica, y sugiriendo cambiarla. Nunca un gráfico en blanco sin explicación.
- **0 filas filtradas:** el estado vacío que ya existe.
- **Embudo sin filas completas:** mensaje explícito de que ningún centro reporta las tres
  métricas a la vez.
- Un valor no numérico o negativo en un campo de métrica se trata como **ausente**, no como 0.

## Testing

- Tests unitarios de `chartAggregation.ts` (es el único sitio con lógica real):
  cobertura con huecos, agrupación por país excluyendo nulos, top-N con empates,
  distribución con un solo centro, embudo sin filas completas.
- Caso de regresión explícito: **una fila sin dato nunca contribuye 0 a ninguna agregación.**
- La pestaña Personalizado mantiene el comportamiento actual (test de no-regresión).

## Fuera de alcance

- Series temporales: los datos del Explorer no traen fechas de reclutamiento.
- Cambiar de librería de gráficos: seguimos con Recharts, que ya es dependencia.
- Arreglar la baja cobertura de datos en Salesforce — es un problema de datos, no de UI.
  El diseño la **expone**; no la resuelve.
