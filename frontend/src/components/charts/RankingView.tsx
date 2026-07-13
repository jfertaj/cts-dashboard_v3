import React, { useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import { bottomN, coverageFor, topN, SCREENED } from "../../lib/chartAggregation";
import MetricPicker, { SITE_METRIC_OPTIONS } from "./MetricPicker";

export default function RankingView({ rows }: { rows: DataRow[] }) {
  // SCREENED está en SITE_METRIC_OPTIONS (que excluye COUNT_METRIC: rankear
  // centros por "número de centros" da una lista de unos). `coverage` se
  // recalcula siempre con el mismo metricKey que recibe MetricPicker.
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
