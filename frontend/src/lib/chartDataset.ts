import { COUNT_METRIC } from "./chartAggregation";
import { readDataCell, type DataRow } from "./rowAccess";

/** Celda de un dataset de Recharts. `null` = el centro NO reporta la métrica (hueco, no cero). */
export type ChartDatum = Record<string, string | number | null>;

/**
 * Parser del constructor GENÉRICO. Ausente / vacío / no numérico → `null`
 * (la regla de siempre: el que nunca reportó no puede parecerse al que reportó
 * cero). **El negativo SÍ es un valor y se conserva.**
 *
 * ⚠️ NO lo unifiques con `toMetricValue` (lib/chartAggregation), que además
 * anula los negativos. Los dos parsers difieren A PROPÓSITO:
 *
 * - `toMetricValue` sirve a las cuatro vistas orientadas a pregunta (Países /
 *   Ranking / Distribución / Embudo), cuyas métricas son recuentos de pacientes:
 *   "cribados = -3" es dato corrupto, no un valor, y anularlo es correcto.
 * - `toDatasetValue` sirve al constructor genérico (pestaña "Personalizado" y el
 *   chart de Moby), que pinta columnas ARBITRARIAS: una delta, una diferencia o
 *   un saldo negativo son dato perfectamente legítimo. Anularlos hacía
 *   desaparecer la fila del gráfico sin decir nada.
 */
export function toDatasetValue(raw: unknown): number | null {
  if (raw === null || raw === undefined) return null;
  // Las comas se quitan ANTES de `Number()` (SF manda enteros formateados:
  // "1,200"), y la guarda del vacío va antes del `Number()`, porque `Number("")`
  // es 0 y convertiría el hueco en un cero legítimo.
  const text = String(raw).replace(/,/g, "").trim();
  if (text === "") return null;
  const n = Number(text);
  return Number.isFinite(n) ? n : null;
}

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
      const value = toDatasetValue(readDataCell(row, y));
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

/** Clave sintética donde va el total cuando el pie pinta varias series a la vez. */
export const PIE_TOTAL_KEY = "__total__";
/** Máximo de porciones antes de agrupar la cola en "Others". */
const MAX_SLICES = 15;

export type PieSlice = Record<string, unknown> & { _color?: string };

/**
 * Porciones del pie + cuántas filas se quedaron fuera por tener valor negativo.
 * El contador NO es decorativo: es lo que obliga a la UI a explicar la exclusión
 * en vez de dejar que la fila desaparezca en silencio.
 */
export type PieSlices = { slices: PieSlice[]; negativeExcluded: number };

/**
 * Porciones de un pie a partir de un dataset ya montado (el del Explorer o el de
 * la tabla de Moby).
 *
 * Dos exclusiones, por motivos distintos:
 *
 * 1. **Ausente ≠ cero**: la fila que no reporta NINGUNA de las series
 *    seleccionadas se cae del gráfico. Pintarla sería una porción de tamaño cero
 *    — invisible pero presente en la leyenda y contada como dato — que es
 *    exactamente la mentira que este rediseño existe para evitar. Un cero
 *    reportado de verdad sí es porción.
 * 2. **Negativo = valor, pero no geometría**: `toDatasetValue` conserva los
 *    negativos (en una columna arbitraria son dato legítimo) y bar/line los
 *    pintan bajo el eje. Un pie NO puede: un sector de ángulo negativo no existe,
 *    y colarlo en el denominador da porcentajes por encima del 100%. Así que la
 *    porción negativa se excluye, pero se CUENTA en `negativeExcluded` para que
 *    el caller lo diga en voz alta. Silenciarla sería el bug original otra vez.
 */
export function toPieSlices(
  rows: Array<Record<string, unknown>>,
  xKey: string,
  yKeys: string[],
): PieSlices {
  const multi = yKeys.length > 1;
  const valueKey = multi ? PIE_TOTAL_KEY : (yKeys[0] ?? "value");

  const slices: PieSlice[] = [];
  let negativeExcluded = 0;
  for (const row of rows) {
    const values = (multi ? yKeys : [valueKey]).map((k) => toDatasetValue(row?.[k]));
    if (values.every((v) => v === null)) continue;
    const total = values.reduce<number>((acc, v) => acc + (v ?? 0), 0);
    if (total < 0) {
      negativeExcluded += 1;
      continue;
    }
    slices.push({ ...row, [valueKey]: total });
  }
  slices.sort((a, b) => Number(b[valueKey] ?? 0) - Number(a[valueKey] ?? 0));

  if (slices.length <= MAX_SLICES) return { slices, negativeExcluded };
  const head = slices.slice(0, MAX_SLICES - 1);
  const others = slices
    .slice(MAX_SLICES - 1)
    .reduce((acc, s) => acc + Number(s[valueKey] ?? 0), 0);
  return {
    slices: [...head, { ...head[0], [xKey]: "Others", [valueKey]: others, _color: "#95A5A6" }],
    negativeExcluded,
  };
}

/**
 * Dataset plano para el constructor de ejes (pestaña "Personalizado" y el chart
 * de Moby).
 *
 * Regla: ausente ≠ cero. `toDatasetValue` devuelve `null` para el centro que no
 * reporta, y Recharts lo pinta como hueco, no como una barra de cero
 * indistinguible del centro que sí reportó y no reclutó a nadie. El negativo, en
 * cambio, es un valor: se conserva y se pinta bajo el eje.
 */
export function buildChartDataset(rows: DataRow[], xKey: string, yKeys: string[]): ChartDatum[] {
  if (xKey === "country" || xKey === "city") return groupedDataset(rows, xKey, yKeys);

  return rows.map((row) => {
    const datum: ChartDatum = { [xKey]: xValue(row, xKey), __count__: 1 };
    for (const y of yKeys) {
      if (y === COUNT_METRIC) continue;
      datum[y] = toDatasetValue(readDataCell(row, y));
    }
    return datum;
  });
}
