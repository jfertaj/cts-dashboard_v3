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
