import React, { useMemo, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import type { DataRow } from "../../lib/rowAccess";
import {
  coverageFor, groupByCountry, SCREENED, type CountryBucket,
} from "../../lib/chartAggregation";
import MetricPicker, { METRIC_OPTIONS } from "./MetricPicker";

export default function CountriesView({ rows }: { rows: DataRow[] }) {
  // `metricKey` arranca en SCREENED, que SÍ está en METRIC_OPTIONS, y `coverage`
  // se recalcula con ESTE mismo metricKey: si se desincronizan, MetricPicker
  // enseña la key técnica y una línea de cobertura que miente.
  const [metricKey, setMetricKey] = useState<string>(SCREENED);
  const coverage = useMemo(() => coverageFor(rows, metricKey), [rows, metricKey]);
  const data = useMemo(() => groupByCountry(rows, metricKey), [rows, metricKey]);

  return (
    <div className="space-y-3">
      <MetricPicker
        metricKey={metricKey}
        options={METRIC_OPTIONS}
        coverage={coverage}
        onChange={setMetricKey}
      />
      {data.length === 0 ? (
        <p data-testid="chart-empty" className="py-16 text-center text-gray-500">
          Ningún centro del resultado actual reporta esta métrica. Prueba con otra.
        </p>
      ) : (
        <ResponsiveContainer width="100%" height={420}>
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="country" interval={0} angle={-45} textAnchor="end" height={70} />
            <YAxis />
            <Tooltip
              formatter={(value, _name, item) => {
                // `bucket.sites` NO es "centros del país": son sólo los que
                // reportan esta métrica (groupByCountry descarta a los mudos).
                const bucket = item?.payload as CountryBucket | undefined;
                if (!bucket) return [String(value), "Total"];
                return [`${value} (${bucket.sites} centros que la reportan)`, "Total"];
              }}
            />
            <Bar dataKey="value" fill="#7c3aed" />
          </BarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
