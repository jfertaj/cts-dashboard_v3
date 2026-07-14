import { readDataCell, type DataRow } from "./rowAccess";

export const COUNT_METRIC = "__count__";
export const SCREENED = "sf.C_Number_of_Individuals_screened_intotal__c";
export const STAGE1 = "sf.C_Number_of_Stage1_Individuals_followed__c";
export const STAGE2 = "sf.C_Number_of_Stage2_Individuals_followed__c";
export const ASSIGNMENTS = "extra.AssignmentsCount";

export type Coverage = { withData: number; total: number; missing: number };

/**
 * `sites` = número de centros de ese país que REPORTAN esta métrica, NO el
 * total de centros del país (los que no reportan quedan fuera de la suma).
 * El total por país NO es recuperable de un bucket calculado con otra
 * métrica: para obtenerlo hay que llamar a `groupByCountry(rows, COUNT_METRIC)`.
 */
export type CountryBucket = { country: string; value: number; sites: number };

/**
 * Convierte el valor crudo de una celda en un número de métrica.
 *
 * Devuelve null (= AUSENTE) para vacíos, no numéricos y negativos. Nunca 0:
 * un centro que no reporta NO es un centro que reclutó a cero personas, y
 * confundirlos es el bug que este rediseño existe para evitar.
 *
 * ⚠️ Anular los NEGATIVOS es correcto **sólo aquí**. Este parser sirve a las
 * cuatro vistas orientadas a pregunta (Países / Ranking / Distribución /
 * Embudo), cuyas métricas son recuentos de pacientes: "cribados = -3" es dato
 * corrupto, no un valor. El constructor genérico ("Personalizado" y el chart de
 * Moby) pinta columnas ARBITRARIAS, donde un negativo — una delta, una
 * diferencia — sí es dato legítimo; por eso tiene su propio parser,
 * `toDatasetValue` en lib/chartDataset. **NO los unifiques**: sus reglas
 * divergen a propósito y cada una está pineada por sus tests.
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

/**
 * Pareto de centros por métrica.
 *
 * NO llamar con `COUNT_METRIC`: `valueOf` devuelve 1 para toda fila, así que
 * saldrían N barras de valor 1 y una curva acumulada perfectamente recta —
 * un Pareto que no dice nada. "Nº de centros" no es una métrica distribuible;
 * las vistas no deben ofrecerla en este gráfico.
 */
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

/** Centro que reporta las tres métricas del embudo. Un cero aquí es un cero real. */
type CompleteSite = { screened: number; stage1: number; stage2: number };

/**
 * Embudo cribados → Stage 1 → Stage 2.
 * Solo entran los centros que reportan las TRES métricas: sumar un centro que
 * reporta cribados pero no Stage 1 inventaría una caída del embudo que no existe.
 * Ojo: reportar 0 es reportar. El centro que cribó a 50 y no siguió a ninguno
 * entra en el embudo con sus ceros — es el que más interesa ver.
 */
export function funnel(rows: DataRow[]): Funnel {
  // Los valores se extraen DENTRO del filtro, así que la colección que
  // sobrevive lleva `number`s de verdad: no hay ningún `?? 0` por el que un
  // valor ausente pueda colarse como cero si algún día se afloja el filtro.
  const complete: CompleteSite[] = [];
  for (const row of rows) {
    const screened = valueOf(row, SCREENED);
    const stage1 = valueOf(row, STAGE1);
    const stage2 = valueOf(row, STAGE2);
    if (screened === null || stage1 === null || stage2 === null) continue;
    complete.push({ screened, stage1, stage2 });
  }
  const sum = (pick: (site: CompleteSite) => number) =>
    complete.reduce((acc, site) => acc + pick(site), 0);
  return {
    stages: [
      { stage: "Cribados", value: sum((s) => s.screened) },
      { stage: "Stage 1 seguidos", value: sum((s) => s.stage1) },
      { stage: "Stage 2 seguidos", value: sum((s) => s.stage2) },
    ],
    sitesIncluded: complete.length,
    sitesExcluded: rows.length - complete.length,
  };
}
