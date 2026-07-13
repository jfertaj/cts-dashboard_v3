# Rediseño de los gráficos del Explorer — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sustituir el constructor de gráficos genérico del Explorer por cuatro vistas orientadas a preguntas (Países, Ranking, Distribución, Embudo) más una pestaña "Personalizado" que conserva el constructor actual.

**Architecture:** Toda la lógica de agregación se extrae a `lib/chartAggregation.ts`, funciones puras sobre arrays de filas, testeadas con Vitest sin montar React. `ChartModal` pasa a ser un contenedor delgado con pestañas; cada vista es un fichero. La regla que atraviesa todo: **una fila sin dato para la métrica se excluye, nunca contribuye 0**, y cada vista declara su cobertura.

**Tech Stack:** React 18 + TypeScript, Recharts (ya es dependencia), Vitest (a añadir), Playwright (E2E ya existente).

**Spec:** `docs/superpowers/specs/2026-07-13-explorer-charts-redesign-design.md`

## Global Constraints

- **Nunca rellenar huecos con cero.** Un valor ausente, no numérico o negativo en un campo de métrica se trata como **ausente** (`null`), no como `0`. Esta regla se testea explícitamente.
- Todas las vistas operan sobre las filas **actualmente filtradas** del Explorer (las que ya alimentan el badge `Chart NNN`).
- La métrica `__count__` (número de centros) **solo** se ofrece en la vista de Países. En Ranking y Distribución no se lista: rankear centros por número de centros no significa nada.
- La pestaña "Personalizado" mantiene el comportamiento actual sin cambios.
- Recharts se queda; no se cambia de librería de gráficos.
- El frontend no tiene runner de tests unitarios hoy (solo Playwright E2E). La Task 1 lo añade; sin ella el resto del plan no se puede ejecutar en TDD.

## Estructura de ficheros

| Fichero | Responsabilidad |
|---|---|
| `frontend/src/lib/rowAccess.ts` | **Crear.** `readDataCell()`, extraído de `ExplorerView.tsx` para poder importarlo desde `lib/` sin depender de una página. |
| `frontend/src/lib/chartAggregation.ts` | **Crear.** Agregación pura: parseo de métrica, cobertura, agrupación por país, top/bottom-N, Pareto, embudo. Único sitio con lógica de verdad. |
| `frontend/src/lib/chartAggregation.test.ts` | **Crear.** Tests unitarios de lo anterior. |
| `frontend/src/components/charts/ChartModal.tsx` | **Crear.** Contenedor + pestañas. Delgado. |
| `frontend/src/components/charts/MetricPicker.tsx` | **Crear.** Selector de métrica + línea de cobertura. |
| `frontend/src/components/charts/CountriesView.tsx` | **Crear.** Vista por defecto. |
| `frontend/src/components/charts/RankingView.tsx` | **Crear.** |
| `frontend/src/components/charts/DistributionView.tsx` | **Crear.** |
| `frontend/src/components/charts/FunnelView.tsx` | **Crear.** |
| `frontend/src/components/charts/CustomView.tsx` | **Crear.** El `ChartModal.tsx` actual movido tal cual, sin cambios de comportamiento. |
| `frontend/src/components/ChartModal.tsx` | **Borrar** al final de la Task 5 (su contenido vive ya en `charts/CustomView.tsx`). |
| `frontend/src/pages/ExplorerView.tsx` | **Modificar.** Importa el nuevo modal, le pasa las filas crudas, y deja de calcular `chartData` para las vistas preset. |

---

### Task 1: Infraestructura de tests + extraer el accessor de filas

Sin runner de tests unitarios no hay TDD. Esta task lo monta y, de paso, saca `readDataCell` de `ExplorerView.tsx` (donde vive como función de módulo no exportada, línea ~912) a `lib/`, para que la agregación pura pueda usarlo.

**Files:**
- Modify: `frontend/package.json` (devDependency + scripts)
- Modify: `frontend/vite.config.ts` (bloque `test`)
- Create: `frontend/src/lib/rowAccess.ts`
- Create: `frontend/src/lib/rowAccess.test.ts`
- Modify: `frontend/src/pages/ExplorerView.tsx` (borrar la función local, importar la nueva)

**Interfaces:**
- Consumes: nada (primera task).
- Produces:
  ```ts
  export type DataRow = {
    account_id?: string;
    account_name?: string;
    country?: string;
    city?: string;
    data?: Record<string, unknown>;
  };
  export function readDataCell(row: DataRow, key: string): unknown;
  ```

- [ ] **Step 1: Instalar Vitest**

```bash
cd frontend && npm install -D vitest@^2.1.0
```

- [ ] **Step 2: Añadir los scripts de test a `frontend/package.json`**

En el bloque `"scripts"`, junto a los de `test:e2e` que ya existen:

```json
    "test": "vitest run",
    "test:watch": "vitest",
```

- [ ] **Step 3: Configurar Vitest en `frontend/vite.config.ts`**

Cambia el import de `defineConfig` para que venga de `vitest/config` (es un superset compatible del de `vite`) y añade el bloque `test`. No toques `server`, `proxy` ni `build`:

```ts
import { defineConfig } from "vitest/config";

export default defineConfig({
  // ...plugins/server/build existentes, SIN CAMBIOS...
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
```

`environment: "node"` basta: solo testeamos funciones puras, no componentes.

- [ ] **Step 4: Escribir el test que falla**

Crea `frontend/src/lib/rowAccess.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { readDataCell } from "./rowAccess";

describe("readDataCell", () => {
  it("lee la clave exacta tal cual la manda el backend", () => {
    const row = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": 240 } };
    expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBe(240);
  });

  it("cae a la clave sin el prefijo sf.", () => {
    const row = { data: { C_Number_of_Stage1_Individuals_followed__c: 8 } };
    expect(readDataCell(row, "sf.C_Number_of_Stage1_Individuals_followed__c")).toBe(8);
  });

  it("mapea sf.Account.Name al campo plano account_name", () => {
    const row = { account_name: "AOU Careggi_CS-Ad", data: {} };
    expect(readDataCell(row, "sf.Account.Name")).toBe("AOU Careggi_CS-Ad");
  });

  it("devuelve undefined cuando la clave no existe en ninguna variante", () => {
    const row = { data: {} };
    expect(readDataCell(row, "sf.No_Existe__c")).toBeUndefined();
  });

  it("trata la cadena vacía como ausente, no como valor", () => {
    const row = { data: { "sf.C_Number_of_Individuals_screened_intotal__c": "" } };
    expect(readDataCell(row, "sf.C_Number_of_Individuals_screened_intotal__c")).toBeUndefined();
  });
});
```

- [ ] **Step 5: Ejecutar el test y comprobar que falla**

Run: `cd frontend && npm test`
Expected: FAIL — `Failed to resolve import "./rowAccess"`.

- [ ] **Step 6: Crear `frontend/src/lib/rowAccess.ts`**

Copia el cuerpo de `readDataCell` **tal cual** está hoy en `ExplorerView.tsx` (línea ~912 hasta el final de la función), y añade el tipo y el `export`. No cambies la lógica de fallbacks: hay comportamiento del Explorer que depende de ella.

```ts
export type DataRow = {
  account_id?: string;
  account_name?: string;
  country?: string;
  city?: string;
  data?: Record<string, unknown>;
};

/**
 * Lee una celda de una fila del Explorer probando, en orden: la clave exacta,
 * la clave sin prefijo "sf.", las variantes con underscores, y por último los
 * campos planos de la fila (account_name, country, city...).
 * Un valor vacío cuenta como ausente y sigue buscando.
 */
export function readDataCell(row: DataRow, key: string): unknown {
  // === cuerpo idéntico al de ExplorerView.tsx, sin modificar la lógica ===
}
```

- [ ] **Step 7: Ejecutar el test y comprobar que pasa**

Run: `cd frontend && npm test`
Expected: PASS, 5 tests.

- [ ] **Step 8: Reemplazar la función local en `ExplorerView.tsx` por el import**

Borra la definición `function readDataCell(row: any, key: string) { ... }` de `ExplorerView.tsx` y añade arriba, junto a los demás imports de `lib/`:

```ts
import { readDataCell } from "../lib/rowAccess";
```

Todas las llamadas existentes a `readDataCell(...)` siguen funcionando sin tocarlas.

- [ ] **Step 9: Comprobar que el build sigue verde**

Run: `cd frontend && npm run build`
Expected: `✓ built in ...`, sin errores.

- [ ] **Step 10: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/vite.config.ts frontend/src/lib/rowAccess.ts frontend/src/lib/rowAccess.test.ts frontend/src/pages/ExplorerView.tsx
git commit -m "test(frontend): añade Vitest y extrae readDataCell a lib/rowAccess"
```

---

### Task 2: Agregación — parseo de métrica, cobertura y agrupación por país

El corazón del rediseño. La regla "ausente ≠ cero" se implementa y se testea aquí.

**Files:**
- Create: `frontend/src/lib/chartAggregation.ts`
- Create: `frontend/src/lib/chartAggregation.test.ts`

**Interfaces:**
- Consumes: `DataRow`, `readDataCell` de `lib/rowAccess` (Task 1).
- Produces:
  ```ts
  export const COUNT_METRIC = "__count__";
  export const SCREENED = "sf.C_Number_of_Individuals_screened_intotal__c";
  export const STAGE1 = "sf.C_Number_of_Stage1_Individuals_followed__c";
  export const STAGE2 = "sf.C_Number_of_Stage2_Individuals_followed__c";
  export const ASSIGNMENTS = "extra.AssignmentsCount";

  export type Coverage = { withData: number; total: number; missing: number };
  export type CountryBucket = { country: string; value: number; sites: number };

  export function toMetricValue(raw: unknown): number | null;
  export function coverageFor(rows: DataRow[], metricKey: string): Coverage;
  export function groupByCountry(rows: DataRow[], metricKey: string): CountryBucket[];
  ```

- [ ] **Step 1: Escribir los tests que fallan**

Crea `frontend/src/lib/chartAggregation.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import {
  toMetricValue, coverageFor, groupByCountry,
  COUNT_METRIC, SCREENED,
} from "./chartAggregation";
import type { DataRow } from "./rowAccess";

let seq = 0;
const site = (country: string, screened?: unknown): DataRow => ({
  account_id: `${country}-${seq++}`,
  account_name: `Centro ${country}`,
  country,
  data: screened === undefined ? {} : { [SCREENED]: screened },
});

describe("toMetricValue", () => {
  it("parsea números y strings numéricas con comas", () => {
    expect(toMetricValue(240)).toBe(240);
    expect(toMetricValue("1,240")).toBe(1240);
  });

  it("trata ausente, vacío, no numérico y negativo como null (nunca 0)", () => {
    expect(toMetricValue(undefined)).toBeNull();
    expect(toMetricValue(null)).toBeNull();
    expect(toMetricValue("")).toBeNull();
    expect(toMetricValue("n/a")).toBeNull();
    expect(toMetricValue(-5)).toBeNull();
  });

  it("conserva el cero legítimo", () => {
    expect(toMetricValue(0)).toBe(0);
  });
});

describe("coverageFor", () => {
  it("cuenta cuántos centros reportan la métrica", () => {
    const rows = [site("ES", 10), site("ES"), site("IT", 0), site("IT")];
    expect(coverageFor(rows, SCREENED)).toEqual({ withData: 2, total: 4, missing: 2 });
  });

  it("__count__ siempre tiene cobertura total", () => {
    const rows = [site("ES"), site("IT")];
    expect(coverageFor(rows, COUNT_METRIC)).toEqual({ withData: 2, total: 2, missing: 0 });
  });
});

describe("groupByCountry", () => {
  it("suma por país y NO deja que un centro sin dato contribuya 0", () => {
    const rows = [site("ES", 10), site("ES", 5), site("ES"), site("IT", 100)];
    const out = groupByCountry(rows, SCREENED);
    expect(out).toEqual([
      { country: "IT", value: 100, sites: 1 },
      { country: "ES", value: 15, sites: 2 }, // sites = 2, NO 3: el que no reporta no cuenta
    ]);
  });

  it("omite por completo los países donde nadie reporta", () => {
    const rows = [site("ES", 10), site("IT"), site("IT")];
    const out = groupByCountry(rows, SCREENED);
    expect(out.map(b => b.country)).toEqual(["ES"]);
  });

  it("con __count__ cuenta centros, incluidos los que no reportan métricas", () => {
    const rows = [site("ES", 10), site("ES"), site("IT")];
    const out = groupByCountry(rows, COUNT_METRIC);
    expect(out).toEqual([
      { country: "ES", value: 2, sites: 2 },
      { country: "IT", value: 1, sites: 1 },
    ]);
  });

  it("ordena de mayor a menor valor", () => {
    const rows = [site("ES", 1), site("IT", 50), site("FR", 10)];
    expect(groupByCountry(rows, SCREENED).map(b => b.country)).toEqual(["IT", "FR", "ES"]);
  });

  it("devuelve array vacío sin filas", () => {
    expect(groupByCountry([], SCREENED)).toEqual([]);
  });
});
```

- [ ] **Step 2: Ejecutar y comprobar que falla**

Run: `cd frontend && npm test`
Expected: FAIL — `Failed to resolve import "./chartAggregation"`.

- [ ] **Step 3: Implementar `frontend/src/lib/chartAggregation.ts`**

```ts
import { readDataCell, type DataRow } from "./rowAccess";

export const COUNT_METRIC = "__count__";
export const SCREENED = "sf.C_Number_of_Individuals_screened_intotal__c";
export const STAGE1 = "sf.C_Number_of_Stage1_Individuals_followed__c";
export const STAGE2 = "sf.C_Number_of_Stage2_Individuals_followed__c";
export const ASSIGNMENTS = "extra.AssignmentsCount";

export type Coverage = { withData: number; total: number; missing: number };
export type CountryBucket = { country: string; value: number; sites: number };

/**
 * Convierte el valor crudo de una celda en un número de métrica.
 *
 * Devuelve null (= AUSENTE) para vacíos, no numéricos y negativos. Nunca 0:
 * un centro que no reporta NO es un centro que reclutó a cero personas, y
 * confundirlos es el bug que este rediseño existe para evitar.
 */
export function toMetricValue(raw: unknown): number | null {
  if (raw === null || raw === undefined) return null;
  const text = String(raw).replace(/,/g, "").trim();
  if (text === "") return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

/** Valor de una fila para la métrica pedida; null si la fila no la reporta. */
function valueOf(row: DataRow, metricKey: string): number | null {
  if (metricKey === COUNT_METRIC) return 1;
  return toMetricValue(readDataCell(row, metricKey));
}

export function coverageFor(rows: DataRow[], metricKey: string): Coverage {
  const total = rows.length;
  const withData = rows.reduce((acc, r) => acc + (valueOf(r, metricKey) === null ? 0 : 1), 0);
  return { withData, total, missing: total - withData };
}

export function groupByCountry(rows: DataRow[], metricKey: string): CountryBucket[] {
  const buckets = new Map<string, CountryBucket>();
  for (const row of rows) {
    const value = valueOf(row, metricKey);
    if (value === null) continue; // el que no reporta no entra: ni suma, ni cuenta
    const country = String(row.country ?? "").trim() || "(sin país)";
    const bucket = buckets.get(country) ?? { country, value: 0, sites: 0 };
    bucket.value += value;
    bucket.sites += 1;
    buckets.set(country, bucket);
  }
  return Array.from(buckets.values()).sort((a, b) => b.value - a.value);
}
```

- [ ] **Step 4: Ejecutar y comprobar que pasa**

Run: `cd frontend && npm test`
Expected: PASS, todos los tests de `chartAggregation` y `rowAccess`.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chartAggregation.ts frontend/src/lib/chartAggregation.test.ts
git commit -m "feat(charts): agregación por país con cobertura, sin rellenar huecos con cero"
```

---

### Task 3: Agregación — ranking, distribución (Pareto) y embudo

**Files:**
- Modify: `frontend/src/lib/chartAggregation.ts`
- Modify: `frontend/src/lib/chartAggregation.test.ts`

**Interfaces:**
- Consumes: `toMetricValue`, `valueOf` (privada), `DataRow` (Tasks 1-2).
- Produces:
  ```ts
  export type SiteValue = { accountId: string; name: string; value: number };
  export type Pareto = { bars: SiteValue[]; cumulativePct: number[]; missingSites: number };
  export type FunnelStage = { stage: string; value: number };
  export type Funnel = { stages: FunnelStage[]; sitesIncluded: number; sitesExcluded: number };

  export function topN(rows: DataRow[], metricKey: string, n: number): SiteValue[];
  export function bottomN(rows: DataRow[], metricKey: string, n: number): SiteValue[];
  export function distribution(rows: DataRow[], metricKey: string): Pareto;
  export function funnel(rows: DataRow[]): Funnel;
  ```

- [ ] **Step 1: Añadir los tests que fallan**

Añade a `frontend/src/lib/chartAggregation.test.ts` (amplía el import existente con `topN, bottomN, distribution, funnel, STAGE1, STAGE2`):

```ts
const named = (name: string, screened?: unknown): DataRow => ({
  account_id: name, account_name: name, country: "ES",
  data: screened === undefined ? {} : { [SCREENED]: screened },
});

describe("topN / bottomN", () => {
  it("topN devuelve los N mayores, de mayor a menor", () => {
    const rows = [named("a", 5), named("b", 50), named("c", 20)];
    expect(topN(rows, SCREENED, 2)).toEqual([
      { accountId: "b", name: "b", value: 50 },
      { accountId: "c", name: "c", value: 20 },
    ]);
  });

  it("bottomN devuelve los N menores, de menor a mayor", () => {
    const rows = [named("a", 5), named("b", 50), named("c", 20)];
    expect(bottomN(rows, SCREENED, 2)).toEqual([
      { accountId: "a", name: "a", value: 5 },
      { accountId: "c", name: "c", value: 20 },
    ]);
  });

  it("los centros sin dato NUNCA aparecen en el bottom como si fueran cero", () => {
    const rows = [named("reporta", 5), named("silencioso")];
    expect(bottomN(rows, SCREENED, 5).map(s => s.name)).toEqual(["reporta"]);
  });

  it("devuelve menos de N si no hay suficientes centros con dato", () => {
    expect(topN([named("a", 1)], SCREENED, 10)).toHaveLength(1);
  });
});

describe("distribution", () => {
  it("ordena descendente y calcula el % acumulado", () => {
    const rows = [named("a", 50), named("b", 30), named("c", 20)];
    const out = distribution(rows, SCREENED);
    expect(out.bars.map(b => b.name)).toEqual(["a", "b", "c"]);
    expect(out.cumulativePct).toEqual([50, 80, 100]);
  });

  it("cuenta aparte los centros sin dato, sin meterlos en las barras", () => {
    const rows = [named("a", 100), named("mudo1"), named("mudo2")];
    const out = distribution(rows, SCREENED);
    expect(out.bars).toHaveLength(1);
    expect(out.missingSites).toBe(2);
  });

  it("con un solo centro el acumulado es 100%", () => {
    const out = distribution([named("a", 7)], SCREENED);
    expect(out.cumulativePct).toEqual([100]);
  });

  it("no divide por cero cuando el total es 0", () => {
    const out = distribution([named("a", 0)], SCREENED);
    expect(out.cumulativePct).toEqual([0]);
    expect(out.bars).toHaveLength(1);
  });

  it("sin centros con dato devuelve barras vacías", () => {
    const out = distribution([named("mudo")], SCREENED);
    expect(out.bars).toEqual([]);
    expect(out.missingSites).toBe(1);
  });
});

describe("funnel", () => {
  const full = (name: string, s: number, s1: number, s2: number): DataRow => ({
    account_id: name, account_name: name, country: "ES",
    data: { [SCREENED]: s, [STAGE1]: s1, [STAGE2]: s2 },
  });

  it("suma solo los centros que reportan las TRES métricas", () => {
    const rows = [full("completo", 100, 10, 2), named("parcial", 999)];
    const out = funnel(rows);
    expect(out.stages).toEqual([
      { stage: "Cribados", value: 100 },
      { stage: "Stage 1 seguidos", value: 10 },
      { stage: "Stage 2 seguidos", value: 2 },
    ]);
    expect(out.sitesIncluded).toBe(1);
    expect(out.sitesExcluded).toBe(1);
  });

  it("sin ningún centro completo, las etapas van a 0 y sitesIncluded es 0", () => {
    const out = funnel([named("parcial", 999)]);
    expect(out.sitesIncluded).toBe(0);
    expect(out.sitesExcluded).toBe(1);
    expect(out.stages.every(s => s.value === 0)).toBe(true);
  });
});
```

- [ ] **Step 2: Ejecutar y comprobar que falla**

Run: `cd frontend && npm test`
Expected: FAIL — `topN is not a function` (y equivalentes).

- [ ] **Step 3: Implementar en `frontend/src/lib/chartAggregation.ts`**

Añade al final del fichero:

```ts
export type SiteValue = { accountId: string; name: string; value: number };
export type Pareto = { bars: SiteValue[]; cumulativePct: number[]; missingSites: number };
export type FunnelStage = { stage: string; value: number };
export type Funnel = { stages: FunnelStage[]; sitesIncluded: number; sitesExcluded: number };

/** Centros que SÍ reportan la métrica, como pares (centro, valor). Los mudos se caen aquí. */
function sitesWithData(rows: DataRow[], metricKey: string): SiteValue[] {
  const out: SiteValue[] = [];
  for (const row of rows) {
    const value = valueOf(row, metricKey);
    if (value === null) continue;
    out.push({
      accountId: String(row.account_id ?? row.account_name ?? ""),
      name: String(row.account_name ?? "(sin nombre)"),
      value,
    });
  }
  return out;
}

export function topN(rows: DataRow[], metricKey: string, n: number): SiteValue[] {
  return sitesWithData(rows, metricKey).sort((a, b) => b.value - a.value).slice(0, n);
}

export function bottomN(rows: DataRow[], metricKey: string, n: number): SiteValue[] {
  return sitesWithData(rows, metricKey).sort((a, b) => a.value - b.value).slice(0, n);
}

export function distribution(rows: DataRow[], metricKey: string): Pareto {
  const bars = sitesWithData(rows, metricKey).sort((a, b) => b.value - a.value);
  const total = bars.reduce((acc, b) => acc + b.value, 0);
  let running = 0;
  const cumulativePct = bars.map((b) => {
    running += b.value;
    return total === 0 ? 0 : Math.round((running / total) * 100);
  });
  return { bars, cumulativePct, missingSites: rows.length - bars.length };
}

/**
 * Embudo cribados → Stage 1 → Stage 2.
 * Solo entran los centros que reportan las TRES métricas: sumar un centro que
 * reporta cribados pero no Stage 1 inventaría una caída del embudo que no existe.
 */
export function funnel(rows: DataRow[]): Funnel {
  const complete = rows.filter((row) =>
    valueOf(row, SCREENED) !== null &&
    valueOf(row, STAGE1) !== null &&
    valueOf(row, STAGE2) !== null
  );
  const sum = (key: string) =>
    complete.reduce((acc, row) => acc + (valueOf(row, key) ?? 0), 0);
  return {
    stages: [
      { stage: "Cribados", value: sum(SCREENED) },
      { stage: "Stage 1 seguidos", value: sum(STAGE1) },
      { stage: "Stage 2 seguidos", value: sum(STAGE2) },
    ],
    sitesIncluded: complete.length,
    sitesExcluded: rows.length - complete.length,
  };
}
```

- [ ] **Step 4: Ejecutar y comprobar que pasa**

Run: `cd frontend && npm test`
Expected: PASS, todos.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/chartAggregation.ts frontend/src/lib/chartAggregation.test.ts
git commit -m "feat(charts): ranking, Pareto y embudo sobre el subconjunto que reporta"
```

---

### Task 4: `MetricPicker` — selector de métrica y línea de cobertura

La línea de cobertura no es decorativa: es lo que impide leer mal el gráfico. Va pegada al selector porque cambiar de métrica cambia la cobertura.

**Files:**
- Create: `frontend/src/components/charts/MetricPicker.tsx`

**Interfaces:**
- Consumes: `Coverage`, `COUNT_METRIC`, `SCREENED`, `STAGE1`, `STAGE2`, `ASSIGNMENTS` (Task 2).
- Produces:
  ```ts
  export type MetricOption = { key: string; label: string };
  export const METRIC_OPTIONS: MetricOption[];          // incluye __count__
  export const SITE_METRIC_OPTIONS: MetricOption[];     // SIN __count__ (Ranking, Distribución)
  export default function MetricPicker(props: {
    metricKey: string;
    options: MetricOption[];
    coverage: Coverage;
    onChange: (key: string) => void;
  }): JSX.Element;
  ```

- [ ] **Step 1: Crear el componente**

```tsx
import React from "react";
import {
  COUNT_METRIC, SCREENED, STAGE1, STAGE2, ASSIGNMENTS,
  type Coverage,
} from "../../lib/chartAggregation";

export type MetricOption = { key: string; label: string };

export const METRIC_OPTIONS: MetricOption[] = [
  { key: SCREENED, label: "Individuals screened (total)" },
  { key: STAGE1, label: "Stage 1 individuals followed" },
  { key: STAGE2, label: "Stage 2 individuals followed" },
  { key: ASSIGNMENTS, label: "Assignments (count)" },
  { key: COUNT_METRIC, label: "Número de centros" },
];

// Ranking y Distribución operan centro a centro: "rankear centros por número de
// centros" no significa nada, así que __count__ no se ofrece ahí.
export const SITE_METRIC_OPTIONS: MetricOption[] = METRIC_OPTIONS.filter(
  (o) => o.key !== COUNT_METRIC
);

export default function MetricPicker({
  metricKey, options, coverage, onChange,
}: {
  metricKey: string;
  options: MetricOption[];
  coverage: Coverage;
  onChange: (key: string) => void;
}) {
  const label = options.find((o) => o.key === metricKey)?.label ?? metricKey;
  const partial = coverage.missing > 0;
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-2">
        <span className="text-gray-600">Métrica</span>
        <select
          className="border rounded-md px-2 py-1"
          value={metricKey}
          onChange={(e) => onChange(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.key} value={o.key}>{o.label}</option>
          ))}
        </select>
      </label>
      <span
        data-testid="chart-coverage"
        className={partial ? "text-amber-700" : "text-gray-500"}
      >
        {coverage.withData} de {coverage.total} centros reportan {label.toLowerCase()}
        {partial ? ` · ${coverage.missing} sin dato, excluidos` : ""}
      </span>
    </div>
  );
}
```

- [ ] **Step 2: Comprobar que compila**

Run: `cd frontend && npm run build`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/charts/MetricPicker.tsx
git commit -m "feat(charts): selector de métrica con línea de cobertura"
```

---

### Task 5: Contenedor con pestañas + mover el constructor actual a `CustomView`

Movimiento puro, sin cambio de comportamiento: el constructor de hoy pasa a ser una pestaña.

**Files:**
- Create: `frontend/src/components/charts/CustomView.tsx` (contenido actual de `components/ChartModal.tsx`)
- Create: `frontend/src/components/charts/ChartModal.tsx`
- Delete: `frontend/src/components/ChartModal.tsx`
- Modify: `frontend/src/pages/ExplorerView.tsx` (ruta del import)

**Interfaces:**
- Consumes: `DataRow` (Task 1).
- Produces:
  ```ts
  export type ChartTab = "countries" | "ranking" | "distribution" | "funnel" | "custom";
  export default function ChartModal(props: {
    open: boolean;
    onClose: () => void;
    title: string;
    onChangeTitle?: (v: string) => void;  // el título editable de hoy sube al contenedor
    rows: DataRow[];                      // filas filtradas, crudas
    custom: React.ReactNode;              // la pestaña Personalizado, inyectada por ExplorerView
  }): JSX.Element | null;
  ```

- [ ] **Step 1: Mover el fichero conservando el historial**

```bash
git mv frontend/src/components/ChartModal.tsx frontend/src/components/charts/CustomView.tsx
```

- [ ] **Step 2: Renombrar el componente dentro de `CustomView.tsx`**

Cambia únicamente la línea de la declaración. Su cuerpo, props y comportamiento NO se tocan:

```tsx
export default function CustomView({ ... }: { ... }) {
```

Elimina de `CustomView.tsx` el marco del modal (el overlay a pantalla completa, la cabecera con el título y el botón Close): ahora vive dentro de una pestaña y ese marco lo pone el contenedor. Conserva todos los controles (Type, X, Y series, Mode, Legend max), el botón Download y el `<ResponsiveContainer>`. Quita también las props `open`, `onClose`, `title` y `onChangeTitle`, que pasan a ser del contenedor.

- [ ] **Step 3: Crear el contenedor `frontend/src/components/charts/ChartModal.tsx`**

```tsx
import React, { useEffect, useState } from "react";
import type { DataRow } from "../../lib/rowAccess";
import CountriesView from "./CountriesView";
import RankingView from "./RankingView";
import DistributionView from "./DistributionView";
import FunnelView from "./FunnelView";

export type ChartTab = "countries" | "ranking" | "distribution" | "funnel" | "custom";

const TABS: Array<{ key: ChartTab; label: string }> = [
  { key: "countries", label: "Países" },
  { key: "ranking", label: "Ranking" },
  { key: "distribution", label: "Distribución" },
  { key: "funnel", label: "Embudo" },
  { key: "custom", label: "Personalizado" },
];

export default function ChartModal({
  open, onClose, title, onChangeTitle, rows, custom,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  onChangeTitle?: (v: string) => void;
  rows: DataRow[];
  custom: React.ReactNode;
}) {
  const [tab, setTab] = useState<ChartTab>("countries");
  useEffect(() => { if (open) setTab("countries"); }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4">
      <div className="w-full max-w-5xl rounded-xl bg-white shadow-xl" data-testid="chart-modal">
        <div className="flex items-center justify-between border-b px-4 py-3">
          {onChangeTitle ? (
            <input
              className="min-w-0 flex-1 rounded-md border px-2 py-1 font-semibold"
              value={title}
              onChange={(e) => onChangeTitle(e.target.value)}
            />
          ) : (
            <h2 className="font-semibold">{title}</h2>
          )}
          <button className="ml-3 rounded-md border px-3 py-1 text-sm hover:bg-gray-50" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="flex gap-1 border-b px-4 pt-2">
          {TABS.map((t) => (
            <button
              key={t.key}
              data-testid={`chart-tab-${t.key}`}
              onClick={() => setTab(t.key)}
              className={`rounded-t-md px-3 py-1.5 text-sm ${
                tab === t.key
                  ? "border border-b-white bg-white font-medium text-violet-700"
                  : "text-gray-600 hover:bg-gray-50"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="p-4">
          {rows.length === 0 ? (
            <p className="py-16 text-center text-gray-500">
              No hay filas en el resultado actual. Ajusta los filtros del Explorer.
            </p>
          ) : (
            <>
              {tab === "countries" && <CountriesView rows={rows} />}
              {tab === "ranking" && <RankingView rows={rows} />}
              {tab === "distribution" && <DistributionView rows={rows} />}
              {tab === "funnel" && <FunnelView rows={rows} />}
              {tab === "custom" && custom}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Actualizar el import en `ExplorerView.tsx`**

```ts
import ChartModal from "../components/charts/ChartModal";
```

Deja el `<ChartModal ... />` existente como está de momento: la Task 10 lo recablea. El build fallará hasta que existan las cuatro vistas — es lo esperado; las Tasks 6-9 las crean.

- [ ] **Step 5: Commit (build aún rojo, es un movimiento intermedio)**

```bash
git add -A frontend/src/components frontend/src/pages/ExplorerView.tsx
git commit -m "refactor(charts): contenedor con pestañas y constructor movido a CustomView"
```

---

### Task 6: `CountriesView` — la vista por defecto

**Files:**
- Create: `frontend/src/components/charts/CountriesView.tsx`

**Interfaces:**
- Consumes: `groupByCountry`, `coverageFor`, `SCREENED` (Task 2); `MetricPicker`, `METRIC_OPTIONS` (Task 4).
- Produces: `export default function CountriesView(props: { rows: DataRow[] }): JSX.Element;`

- [ ] **Step 1: Crear el componente**

```tsx
import React, { useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { coverageFor, groupByCountry, SCREENED } from "../../lib/chartAggregation";
import MetricPicker, { METRIC_OPTIONS } from "./MetricPicker";

export default function CountriesView({ rows }: { rows: DataRow[] }) {
  const [metricKey, setMetricKey] = useState<string>(SCREENED);
  const coverage = useMemo(() => coverageFor(rows, metricKey), [rows, metricKey]);
  const data = useMemo(() => groupByCountry(rows, metricKey), [rows, metricKey]);

  return (
    <div className="space-y-3">
      <MetricPicker
        metricKey={metricKey}
        options={METRIC_OPTIONS}
        coverage={coverage}
        onChange={setMetricKey}
      />
      {data.length === 0 ? (
        <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
          Ningún centro del resultado actual reporta esta métrica. Prueba con otra.
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={420}>
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="country" interval={0} angle={-45} textAnchor="end" height={70} />
            <YAxis />
            <Tooltip
              formatter={(value: number, _name: string, item: any) =>
                [`${value} (${item?.payload?.sites} centros)`, "Total"]
              }
            />
            <Bar dataKey="value" fill="#7c3aed" />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/charts/CountriesView.tsx
git commit -m "feat(charts): vista de países"
```

---

### Task 7: `RankingView`

**Files:**
- Create: `frontend/src/components/charts/RankingView.tsx`

**Interfaces:**
- Consumes: `topN`, `bottomN`, `coverageFor`, `SCREENED` (Tasks 2-3); `MetricPicker`, `SITE_METRIC_OPTIONS` (Task 4).
- Produces: `export default function RankingView(props: { rows: DataRow[] }): JSX.Element;`

- [ ] **Step 1: Crear el componente**

Barras **horizontales** (`layout="vertical"` en Recharts): los nombres de centro son largos y en vertical no se leen.

```tsx
import React, { useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { bottomN, coverageFor, topN, SCREENED } from "../../lib/chartAggregation";
import MetricPicker, { SITE_METRIC_OPTIONS } from "./MetricPicker";

export default function RankingView({ rows }: { rows: DataRow[] }) {
  const [metricKey, setMetricKey] = useState<string>(SCREENED);
  const [end, setEnd] = useState<"top" | "bottom">("top");
  const [n, setN] = useState<number>(10);

  const coverage = useMemo(() => coverageFor(rows, metricKey), [rows, metricKey]);
  const data = useMemo(
    () => (end === "top" ? topN(rows, metricKey, n) : bottomN(rows, metricKey, n)),
    [rows, metricKey, n, end]
  );

  return (
    <div className="space-y-3">
      <MetricPicker
        metricKey={metricKey}
        options={SITE_METRIC_OPTIONS}
        coverage={coverage}
        onChange={setMetricKey}
      />
      <div className="flex items-center gap-3 text-sm">
        <div className="flex gap-1">
          {(["top", "bottom"] as const).map((e) => (
            <button
              key={e}
              onClick={() => setEnd(e)}
              className={`rounded-md border px-2 py-1 ${
                end === e ? "border-violet-500 bg-violet-50 text-violet-700" : "hover:bg-gray-50"
              }`}
            >
              {e === "top" ? "Top" : "Bottom"}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2">
          <span className="text-gray-600">N</span>
          <select
            className="border rounded-md px-2 py-1"
            value={n}
            onChange={(e) => setN(Number(e.target.value))}
          >
            {[5, 10, 20, 30].map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
      </div>
      {data.length === 0 ? (
        <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
          Ningún centro del resultado actual reporta esta métrica. Prueba con otra.
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={Math.max(280, data.length * 32)}>
          <BarChart data={data} layout="vertical" margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" horizontal={false} />
            <XAxis type="number" />
            <YAxis type="category" dataKey="name" width={240} tick={{ fontSize: 12 }} />
            <Tooltip />
            <Bar dataKey="value" fill="#7c3aed" />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/charts/RankingView.tsx
git commit -m "feat(charts): vista de ranking top/bottom-N"
```

---

### Task 8: `DistributionView` — Pareto + centros silenciosos

**Files:**
- Create: `frontend/src/components/charts/DistributionView.tsx`

**Interfaces:**
- Consumes: `distribution`, `coverageFor`, `SCREENED` (Tasks 2-3); `MetricPicker`, `SITE_METRIC_OPTIONS` (Task 4).
- Produces: `export default function DistributionView(props: { rows: DataRow[] }): JSX.Element;`

- [ ] **Step 1: Crear el componente**

```tsx
import React, { useMemo, useState } from "react";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { coverageFor, distribution, SCREENED } from "../../lib/chartAggregation";
import MetricPicker, { SITE_METRIC_OPTIONS } from "./MetricPicker";

export default function DistributionView({ rows }: { rows: DataRow[] }) {
  const [metricKey, setMetricKey] = useState<string>(SCREENED);
  const coverage = useMemo(() => coverageFor(rows, metricKey), [rows, metricKey]);
  const pareto = useMemo(() => distribution(rows, metricKey), [rows, metricKey]);

  const data = useMemo(
    () => pareto.bars.map((bar, i) => ({ ...bar, cumulativePct: pareto.cumulativePct[i] })),
    [pareto]
  );

  return (
    <div className="space-y-3">
      <MetricPicker
        metricKey={metricKey}
        options={SITE_METRIC_OPTIONS}
        coverage={coverage}
        onChange={setMetricKey}
      />
      {data.length === 0 ? (
        <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
          Ningún centro del resultado actual reporta esta métrica. Prueba con otra.
        </p>
      ) : (
        <>
          <ResponsiveContainer width="100%" height={400}>
            <ComposedChart data={data} margin={{ top: 8, right: 24, bottom: 8, left: 8 }}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              {/* Sin etiquetas de centro en el eje X: son 100+ y es justo el error que
                  este rediseño existe para no repetir. El nombre va en el tooltip. */}
              <XAxis dataKey="name" tick={false} height={12} />
              <YAxis yAxisId="left" />
              <YAxis yAxisId="right" orientation="right" unit="%" domain={[0, 100]} />
              <Tooltip />
              <Legend />
              <Bar yAxisId="left" dataKey="value" name="Valor" fill="#7c3aed" />
              <Line
                yAxisId="right"
                type="monotone"
                dataKey="cumulativePct"
                name="% acumulado"
                stroke="#f59e0b"
                dot={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
          <p data-testid="chart-silent-sites" className="text-sm text-amber-700">
            {pareto.missingSites === 0
              ? "Todos los centros del resultado actual reportan esta métrica."
              : `${pareto.missingSites} centros no reportan esta métrica y no aparecen en el gráfico.`}
          </p>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/components/charts/DistributionView.tsx
git commit -m "feat(charts): vista de distribución (Pareto) con centros silenciosos"
```

---

### Task 9: `FunnelView`

**Files:**
- Create: `frontend/src/components/charts/FunnelView.tsx`

**Interfaces:**
- Consumes: `funnel` (Task 3).
- Produces: `export default function FunnelView(props: { rows: DataRow[] }): JSX.Element;`

Esta vista **no lleva selector de métrica**: el embudo son siempre las tres etapas.

- [ ] **Step 1: Crear el componente**

```tsx
import React, { useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { funnel } from "../../lib/chartAggregation";

export default function FunnelView({ rows }: { rows: DataRow[] }) {
  const result = useMemo(() => funnel(rows), [rows]);

  if (result.sitesIncluded === 0) {
    return (
      <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
        Ningún centro del resultado actual reporta las tres métricas (cribados, Stage 1 y
        Stage 2) a la vez, que es lo que el embudo necesita para no inventarse caídas.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p data-testid="chart-coverage" className="text-sm text-amber-700">
        Embudo calculado sobre {result.sitesIncluded} centros que reportan las tres métricas
        {result.sitesExcluded > 0
          ? ` · ${result.sitesExcluded} centros excluidos por reportarlas de forma incompleta`
          : ""}
      </p>
      <ResponsiveContainer width="100%" height={380}>
        <BarChart data={result.stages} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="stage" />
          <YAxis />
          <Tooltip />
          <Bar dataKey="value" fill="#7c3aed" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
```

- [ ] **Step 2: Comprobar que el build vuelve a estar verde**

Ya existen las cuatro vistas que el contenedor de la Task 5 importaba.

Run: `cd frontend && npm run build`
Expected: `✓ built in ...`.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/charts/FunnelView.tsx
git commit -m "feat(charts): vista de embudo sobre centros con las tres métricas"
```

---

### Task 10: Cablear en `ExplorerView` y smoke E2E

**Files:**
- Modify: `frontend/src/pages/ExplorerView.tsx`
- Create: `frontend/tests/charts.spec.ts`

**Interfaces:**
- Consumes: `ChartModal` (Task 5), `CustomView` (Task 5).

- [ ] **Step 1: Pasar las filas crudas al modal**

En `ExplorerView.tsx`, sustituye el bloque `<ChartModal ... />` (línea ~3453) por:

Las props que hoy se le pasan al modal (verificadas en `ExplorerView.tsx:3453-3472`) se
reparten así: `open` / `onClose` / `title` / `onChangeTitle` se quedan en el contenedor;
todas las de ejes y series bajan a `CustomView`. `legendMax` **no se pasa hoy** (el modal usa
su valor por defecto de 8), así que tampoco se pasa aquí.

```tsx
<ChartModal
  open={chartOpen}
  onClose={() => setChartOpen(false)}
  title={chartTitle}
  onChangeTitle={(t) => setChartTitle(t)}
  rows={nearbyActive ? fullNearbyRows : fullRows}
  custom={
    <CustomView
      data={chartData}
      xKey={chartXKey}
      yKeys={chartYKeys}
      type={chartType}
      xCandidates={chartXCandidates}
      yCandidates={chartYCandidates}
      labelByKey={labelByKey}
      onChangeType={(t) => setChartType(t)}
      onChangeXKey={(x) => setChartXKey(x)}
      onToggleYKey={(y) => {
        setChartYKeys((prev) =>
          prev.includes(y) ? prev.filter((k) => k !== y) : [...prev, y]
        );
      }}
    />
  }
/>
```

Añade el import: `import CustomView from "../components/charts/CustomView";`

Mantén `chartData`, `chartXKey`, `chartYKeys`, `chartXCandidates`, `chartYCandidates`,
`buildChartDataset` y sus `useMemo`: **son de la pestaña Personalizado y siguen vivos**. Las
cuatro vistas nuevas no los usan — agregan ellas a partir de `rows`.

- [ ] **Step 2: Comprobar el build**

Run: `cd frontend && npm run build`
Expected: `✓ built in ...`.

- [ ] **Step 3: Escribir el test E2E que falla**

Crea `frontend/tests/charts.spec.ts`:

```ts
import { test, expect } from "@playwright/test";

test("el gráfico abre en la vista de Países y declara la cobertura", async ({ page }) => {
  await page.goto("/explorer");
  await page.getByTestId("explorer-results-table").waitFor();

  await page.getByRole("button", { name: /Chart/ }).click();

  const modal = page.getByTestId("chart-modal");
  await expect(modal).toBeVisible();

  // La pestaña por defecto es Países, NO el constructor de ejes.
  await expect(modal.getByTestId("chart-tab-countries")).toBeVisible();

  // La cobertura se declara siempre: es lo que impide leer mal el gráfico.
  await expect(modal.getByTestId("chart-coverage")).toContainText(/de \d+ centros reportan/);

  // El constructor sigue accesible en su pestaña.
  await modal.getByTestId("chart-tab-custom").click();
  await expect(modal.getByTestId("chart-tab-custom")).toBeVisible();
});
```

- [ ] **Step 4: Ejecutar el E2E**

Necesita el frontend levantado y una sesión de Salesforce válida.

Run: `cd frontend && npm run test:e2e -- charts.spec.ts`
Expected: PASS.

- [ ] **Step 5: Ejecutar toda la suite unitaria y el build**

Run: `cd frontend && npm test && npm run build`
Expected: todos los tests PASS y `✓ built in ...`.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/ExplorerView.tsx frontend/tests/charts.spec.ts
git commit -m "feat(explorer): las cuatro vistas de gráfico sustituyen al constructor por defecto"
```

- [ ] **Step 7: Actualizar la documentación del proyecto**

`CLAUDE.md` obliga a actualizar `docs/current-state.md` y `docs/next-steps.md` en el mismo cambio, no al final de la sesión. Añade a `docs/current-state.md` que `ChartModal` es ahora un contenedor con pestañas y que la agregación vive en `lib/chartAggregation.ts`, y marca en `docs/next-steps.md` lo que este plan cierra.

```bash
git add docs/current-state.md docs/next-steps.md
git commit -m "docs: estado del rediseño de gráficos del Explorer"
```
