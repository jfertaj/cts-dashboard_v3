import React, { useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { funnel } from "../../lib/chartAggregation";

// Sin MetricPicker: el embudo son siempre las tres etapas (cribados → Stage 1 →
// Stage 2), así que no hay metricKey que elegir ni que desincronizar. La línea
// de cobertura se construye a mano desde sitesIncluded/sitesExcluded.
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
