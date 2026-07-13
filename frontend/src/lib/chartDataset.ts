import { COUNT_METRIC, toMetricValue } from "./chartAggregation";
import { readDataCell, type DataRow } from "./rowAccess";

/** Celda de un dataset de Recharts. `null` = el centro NO reporta la métrica (hueco, no cero). */
export type ChartDatum = Record<string, string | number | null>;

/** El eje X sale de una celda cruda: Recharts necesita un escalar. */
function xValue(row: DataRow, xKey: string): string | number {
  const raw = readDataCell(row, xKey);
  if (raw === null || raw === undefined) return "";
  return typeof raw === "number" ? raw : String(raw);
}

/**
 * Agrupa por país/ciudad: suma la métrica SOLO de los centros que la reportan.
 *
 * Un bucket en el que nadie reporta queda en `null`, no en 0: un país del que no
 * hay dato no es un país que reclutó a cero personas.
 *
 * `__count__` es la excepción deliberada: su etiqueta es "Count (rows)" y su
 * significado es "cuántas filas caen en este grupo", independiente de la métrica
 * que se pinte (puede haber varias series Y a la vez, o ninguna). Contar solo a
 * los reportadores lo haría depender de una serie arbitraria y vaciaría la serie
 * "Count" cuando se elige sola. Sigue contando TODAS las filas del grupo.
 */
function groupedDataset(rows: DataRow[], xKey: string, yKeys: string[]): ChartDatum[] {
  const buckets = new Map<string, ChartDatum>();
  for (const row of rows) {
    const key = String(xValue(row, xKey)).trim() || "(empty)";
    const bucket: ChartDatum = buckets.get(key) ?? { [xKey]: key, __count__: 0 };
    bucket.__count__ = Number(bucket.__count__ ?? 0) + 1;
    for (const y of yKeys) {
      if (y === COUNT_METRIC) continue;
      const value = toMetricValue(readDataCell(row, y));
      if (value === null) {
        // El que no reporta no suma. Si nadie del grupo reporta, la clave se
        // queda en null y Recharts pinta un hueco en vez de una barra de cero.
        if (!(y in bucket)) bucket[y] = null;
        continue;
      }
      bucket[y] = Number(bucket[y] ?? 0) + value;
    }
    buckets.set(key, bucket);
  }
  return Array.from(buckets.values()).sort(
    (a, b) => Number(b.__count__ ?? 0) - Number(a.__count__ ?? 0)
  );
}

/**
 * Dataset plano para el constructor de ejes (pestaña "Personalizado").
 *
 * Regla: ausente ≠ cero. `toMetricValue` — el mismo parser que usan las otras
 * cuatro vistas — devuelve `null` para el centro que no reporta, y Recharts lo
 * pinta como hueco, no como una barra de cero indistinguible del centro que sí
 * reportó y no reclutó a nadie.
 */
export function buildChartDataset(rows: DataRow[], xKey: string, yKeys: string[]): ChartDatum[] {
  if (xKey === "country" || xKey === "city") return groupedDataset(rows, xKey, yKeys);

  return rows.map((row) => {
    const datum: ChartDatum = { [xKey]: xValue(row, xKey), __count__: 1 };
    for (const y of yKeys) {
      if (y === COUNT_METRIC) continue;
      datum[y] = toMetricValue(readDataCell(row, y));
    }
    return datum;
  });
}
