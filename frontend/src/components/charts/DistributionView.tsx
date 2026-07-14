import React, { useMemo, useState } from "react";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { coverageFor, distribution, SCREENED } from "../../lib/chartAggregation";
import { siteCount } from "../../lib/chartText";
import MetricPicker, { SITE_METRIC_OPTIONS } from "./MetricPicker";
import { NO_ENTRY_ANIMATION } from "./chartDefaults";

export default function DistributionView({ rows }: { rows: DataRow[] }) {
  // SCREENED está en SITE_METRIC_OPTIONS (que excluye COUNT_METRIC: un Pareto
  // de "número de centros" son N barras de valor 1 y una recta). `coverage` se
  // recalcula siempre con el mismo metricKey que recibe MetricPicker.
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
          No site in the current result reports this metric. Try another one.
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
              <Bar yAxisId="left" dataKey="value" name="Value" fill="#7c3aed" {...NO_ENTRY_ANIMATION} />
              <Line
                yAxisId="right"
                type="monotone"
                dataKey="cumulativePct"
                name="Cumulative %"
                stroke="#f59e0b"
                dot={false}
                {...NO_ENTRY_ANIMATION}
              />
            </ComposedChart>
          </ResponsiveContainer>
          <p data-testid="chart-silent-sites" className="text-sm text-amber-700">
            {/* "with no data" y no "zero": el centro mudo no está en el gráfico, no está
                en él con una barra a cero. Confundirlo es el bug que el rediseño arregla. */}
            {pareto.missingSites === 0
              ? "Every site in the current result reports this metric."
              : `${siteCount(pareto.missingSites)} with no data for this metric — not shown in the chart.`}
          </p>
        </>
      )}
    </div>
  );
}
