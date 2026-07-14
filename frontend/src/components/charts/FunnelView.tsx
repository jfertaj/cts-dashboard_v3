import React, { useMemo } from "react";
import {
  BarChart, Bar, LabelList, XAxis, YAxis, CartesianGrid, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { funnel, type FunnelStage } from "../../lib/chartAggregation";
import { NO_ENTRY_ANIMATION } from "./chartDefaults";

/**
 * Una etapa lista para pintar: el absoluto, lo que retiene de la etapa ANTERIOR
 * y lo que queda de la cohorte inicial.
 *
 * `retentionPct === null` = no hay base sobre la que dividir (la etapa anterior
 * suma 0). No es un 0 %: es una división imposible. Distinguirlos importa —
 * 0/0 daría NaN y 3/0 daría Infinity, y las dos cosas pintan basura en el eje.
 */
export type FunnelStep = {
  stage: string;
  value: number;
  retentionPct: number | null;
  ofFirstPct: number | null;
  /** Longitud de la barra, en la escala de retención. Sin base = 0: no hay barra que pintar. */
  barPct: number;
};

const pctOf = (part: number, whole: number): number | null =>
  whole > 0 ? (part / whole) * 100 : null;

/**
 * Escala de RETENCIÓN, no de absolutos: la barra de cada etapa mide el % que
 * sobrevive de la anterior. Con los datos de producción (22.000 → 512 → 380) el
 * eje absoluto compartido deja las etapas 2 y 3 como slivers de un píxel, y la
 * relación Stage 1 → Stage 2 — la pregunta que esta pestaña existe para
 * responder — es literalmente ilegible. En la escala de retención esa misma
 * relación es una barra del 74 %. La magnitud absoluta no se pierde: va en la
 * etiqueta de cada barra y en la tira de conversión de arriba.
 */
export function toFunnelSteps(stages: FunnelStage[]): FunnelStep[] {
  const first = stages[0]?.value ?? 0;
  return stages.map((stage, i) => {
    const retentionPct = i === 0 ? pctOf(first, first) : pctOf(stage.value, stages[i - 1].value);
    return {
      stage: stage.stage,
      value: stage.value,
      retentionPct,
      ofFirstPct: pctOf(stage.value, first),
      barPct: retentionPct ?? 0,
    };
  });
}

export function formatCount(value: number): string {
  return value.toLocaleString("es-ES");
}

export function formatPct(pct: number | null): string {
  if (pct === null) return "sin base";
  return `${pct.toLocaleString("es-ES", { maximumFractionDigits: 1 })} %`;
}

/** La caída se lee aquí aunque la barra sea un sliver: es texto, no geometría. */
function StepCard({ step, isFirst }: { step: FunnelStep; isFirst: boolean }) {
  return (
    <li
      data-testid="chart-funnel-step"
      data-stage={step.stage}
      className="flex-1 rounded border border-gray-200 bg-gray-50 px-3 py-2"
    >
      <p className="text-xs uppercase tracking-wide text-gray-500">{step.stage}</p>
      <p data-testid="chart-funnel-count" className="text-2xl font-semibold text-gray-900">
        {formatCount(step.value)}
      </p>
      <p data-testid="chart-funnel-conversion" className="text-sm text-gray-600">
        {isFirst
          ? "punto de partida"
          : `${formatPct(step.retentionPct)} de la etapa anterior · ${formatPct(step.ofFirstPct)} de los cribados`}
      </p>
    </li>
  );
}

// Sin MetricPicker: el embudo son siempre las tres etapas (cribados → Stage 1 →
// Stage 2), así que no hay metricKey que elegir ni que desincronizar. La línea
// de cobertura se construye a mano desde sitesIncluded/sitesExcluded.
export default function FunnelView({ rows }: { rows: DataRow[] }) {
  const result = useMemo(() => funnel(rows), [rows]);
  const steps = useMemo(() => toFunnelSteps(result.stages), [result]);

  // El dato de la barra lleva su etiqueta ya formateada: "512 (2,3 %)". Se pinta
  // aunque la barra mida 0 — un cero legítimo tiene que verse como un 0, que es
  // la razón entera de este rediseño.
  const chartData = useMemo(
    () => steps.map((step) => ({
      ...step,
      barLabel: `${formatCount(step.value)} (${formatPct(step.retentionPct)})`,
    })),
    [steps]
  );

  // El eje llega al 100 % salvo que un dato lo desborde (una etapa que reporta
  // MÁS que la anterior). Recortarlo a 100 escondería justo ese dato corrupto.
  const axisMax = useMemo(
    () => Math.max(100, ...steps.map((step) => Math.ceil(step.barPct))),
    [steps]
  );

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

      <ul data-testid="chart-funnel-steps" className="flex gap-2">
        {steps.map((step, i) => (
          <StepCard key={step.stage} step={step} isFirst={i === 0} />
        ))}
      </ul>

      {/* Barras horizontales sobre el eje de retención. Sin Tooltip: lo que diría
          (absoluto + conversión) ya está pintado en la etiqueta de cada barra y en
          la tira de arriba, siempre visible y sin hover. */}
      <ResponsiveContainer width="100%" height={260}>
        <BarChart
          layout="vertical"
          data={chartData}
          margin={{ top: 8, right: 110, bottom: 24, left: 8 }}
        >
          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
          <XAxis
            type="number"
            domain={[0, axisMax]}
            unit="%"
            label={{
              value: "% que sobrevive de la etapa anterior",
              position: "insideBottom",
              offset: -12,
            }}
          />
          <YAxis type="category" dataKey="stage" width={130} />
          <Bar dataKey="barPct" fill="#7c3aed" {...NO_ENTRY_ANIMATION}>
            <LabelList
              dataKey="barLabel"
              position="right"
              fill="#374151"
              fontSize={12}
              {...NO_ENTRY_ANIMATION}
            />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
