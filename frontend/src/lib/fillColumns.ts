import { SCREENED, STAGE1, STAGE2, ASSIGNMENTS } from "./chartAggregation";

/**
 * Métricas que las vistas del ChartModal (Países / Ranking / Distribución /
 * Embudo) agregan desde `row.data`.
 *
 * ⚠️ La búsqueda en bloque (`POST /api/explorer/search`) devuelve estas claves a
 * `null` en TODAS las filas: los valores de verdad sólo llegan por el fill
 * perezoso (`POST /api/explorer/columns/fill`). Si el fill sólo pide las
 * columnas VISIBLES, una métrica que no sea columna de la tabla es `null` para
 * siempre — y la línea de cobertura anuncia "0 de 215 centros reportan" cuando
 * la verdad es que nadie se la pidió al servidor. Por eso van SIEMPRE en el
 * fill, con independencia de la visibilidad de columnas.
 *
 * `COUNT_METRIC` no está aquí a propósito: es una pseudo-métrica que se resuelve
 * en cliente (1 por fila), no tiene dato en el servidor.
 */
export const CHART_METRIC_COLUMNS: readonly string[] = [SCREENED, STAGE1, STAGE2, ASSIGNMENTS];

/** Columnas a pedir a `/columns/fill`: las visibles ∪ las métricas del chart, menos las no rellenables. */
export function buildFillColumns(
  visibleColumns: readonly string[],
  unfillable: ReadonlySet<string>
): string[] {
  const all = [...(visibleColumns ?? []), ...CHART_METRIC_COLUMNS];
  return Array.from(new Set(all.filter((c) => c && !unfillable.has(c))));
}
