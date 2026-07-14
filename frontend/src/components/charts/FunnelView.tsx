import React, { useMemo } from "react";
import {
  BarChart, Bar, LabelList, XAxis, YAxis, CartesianGrid, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { funnel, type FunnelStage } from "../../lib/chartAggregation";
import { siteCount } from "../../lib/chartText";
import { NO_ENTRY_ANIMATION } from "./chartDefaults";

/**
 * Una etapa lista para pintar: el absoluto, lo que retiene de la etapa ANTERIOR
 * y lo que queda de la cohorte inicial.
 *
 * `retentionPct === null` = no hay base sobre la que dividir (la etapa anterior
 * suma 0). No es un 0 %: es una división imposible. Distinguirlos importa —
 * 0/0 daría NaN y 3/0 daría Infinity, y las dos cosas pintan basura.
 */
export type FunnelStep = {
  stage: string;
  value: number;
  retentionPct: number | null;
  ofFirstPct: number | null;
};

const pctOf = (part: number, whole: number): number | null =>
  whole > 0 ? (part / whole) * 100 : null;

/**
 * La barra mide el ABSOLUTO. Es la única geometría honesta que tiene un embudo:
 * las barras encogen porque la cohorte encoge.
 *
 * Hubo una versión que codificaba la RETENCIÓN de la etapa anterior para ganar
 * legibilidad. Con los datos de producción (22.000 → 512 → 380) dibujaba
 * 100 % → 2,3 % → 74 %: barra llena, sliver, barra casi llena. Un lector de un
 * vistazo concluía que hay MÁS gente en Stage 2 que en Stage 1 — exactamente lo
 * contrario de la verdad. Un embudo tiene un contrato visual (las barras
 * encogen) y romperlo cambia un gráfico ilegible por uno que MIENTE.
 *
 * El sliver de Stage 1 no es un fallo del gráfico: es el hallazgo. La caída es
 * así de brutal y la geometría debe decirlo. Lo que la barra no puede contar
 * — el 74 % que Stage 2 retiene de Stage 1 — lo cuenta el TEXTO de las tarjetas,
 * que va arriba, grande y sin hover.
 */
export function toFunnelSteps(stages: FunnelStage[]): FunnelStep[] {
  const first = stages[0]?.value ?? 0;
  return stages.map((stage, i) => ({
    stage: stage.stage,
    value: stage.value,
    retentionPct: i === 0 ? pctOf(first, first) : pctOf(stage.value, stages[i - 1].value),
    ofFirstPct: pctOf(stage.value, first),
  }));
}

/**
 * Tope del eje: la etapa mayor. Eje compartido y absoluto = las tres barras son
 * comparables entre sí, que es lo que hace visible el desplome.
 *
 * El suelo de 1 es sólo para que Recharts no reciba el dominio degenerado [0, 0]
 * cuando todas las etapas suman 0. Las barras siguen midiendo 0: eso es la verdad.
 */
export function funnelAxisMax(steps: FunnelStep[]): number {
  return Math.max(1, ...steps.map((step) => step.value));
}

// Locale explícito ("en-US") y no el del navegador: con un navegador en español
// toLocaleString() sin argumentos escribiría "74,2" y "22.000" dentro de una UI
// inglesa, donde la coma se lee como separador de millar. El número diría otra
// cosa según quién lo mire, que es justo lo que este módulo no puede permitirse.
export function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

export function formatPct(pct: number | null): string {
  if (pct === null) return "no baseline";
  return `${pct.toLocaleString("en-US", { maximumFractionDigits: 1 })}%`;
}

/**
 * La caída, en palabras. "no baseline" no se disfraza de porcentaje: si la etapa
 * anterior suma 0 no hay fracción que calcular, y decir "0%" ahí sería inventar
 * una conversión que nadie ha medido.
 */
export function conversionText(step: FunnelStep, isFirst: boolean): string {
  if (isFirst) return "starting point";
  const retention =
    step.retentionPct === null
      ? "no baseline: the previous stage is 0"
      : `${formatPct(step.retentionPct)} of the previous stage`;
  return `${retention} · ${formatPct(step.ofFirstPct)} of those screened`;
}

/**
 * La tarjeta es la mitad legible del embudo, no un adorno del gráfico: cuando la
 * barra de Stage 1 es un sliver de un píxel, esto es lo ÚNICO que responde "¿y
 * de los que llegaron a Stage 1, cuántos siguieron?". Por eso el porcentaje de
 * retención va en grande y no en un tooltip que hay que descubrir con el ratón.
 */
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
      <p data-testid="chart-funnel-conversion" className="text-sm font-medium text-gray-700">
        {conversionText(step, isFirst)}
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

  const axisMax = useMemo(() => funnelAxisMax(steps), [steps]);

  if (result.sitesIncluded === 0) {
    return (
      <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
        No site in the current result reports all three metrics (screened, Stage 1 and
        Stage 2) at once, which is what the funnel needs in order not to invent drop-offs.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <p data-testid="chart-coverage" className="text-sm text-amber-700">
        Funnel computed over {siteCount(result.sitesIncluded)} reporting all three metrics
        {result.sitesExcluded > 0
          ? ` · ${siteCount(result.sitesExcluded)} excluded for reporting them incompletely`
          : ""}
      </p>

      <ul data-testid="chart-funnel-steps" className="flex gap-2">
        {steps.map((step, i) => (
          <StepCard key={step.stage} step={step} isFirst={i === 0} />
        ))}
      </ul>

      {/* Barras horizontales sobre el eje ABSOLUTO compartido: encogen con el dato.
          Sin Tooltip: las conversiones ya están en las tarjetas de arriba, siempre
          visibles y sin hover.

          La etiqueta de la barra lleva SOLO el absoluto, y no por pereza: el <Text>
          de Recharts parte la etiqueta en <tspan>s para que quepa en el ANCHO DE SU
          PROPIA BARRA. Con los slivers de producción, un "380 · 74,2 % de la
          anterior" se rompería en una columna de palabras sueltas. El absoluto es un
          único token y nunca se parte. */}
      <ResponsiveContainer width="100%" height={260}>
        <BarChart
          layout="vertical"
          data={steps}
          margin={{ top: 8, right: 90, bottom: 24, left: 8 }}
        >
          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
          <XAxis
            type="number"
            domain={[0, axisMax]}
            label={{
              value: "individuals (absolute, shared scale)",
              position: "insideBottom",
              offset: -12,
            }}
          />
          <YAxis type="category" dataKey="stage" width={130} />
          <Bar dataKey="value" fill="#7c3aed" {...NO_ENTRY_ANIMATION}>
            <LabelList
              dataKey="value"
              formatter={(value: number) => formatCount(value)}
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
