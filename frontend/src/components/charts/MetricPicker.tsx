import React from "react";
import {
  COUNT_METRIC, SCREENED, STAGE1, STAGE2, ASSIGNMENTS,
  type Coverage,
} from "../../lib/chartAggregation";

export type MetricOption = { key: string; label: string };

export const METRIC_OPTIONS: MetricOption[] = [
  { key: SCREENED, label: "Individuals screened (total)" },
  { key: STAGE1, label: "Stage 1 individuals followed" },
  { key: STAGE2, label: "Stage 2 individuals followed" },
  { key: ASSIGNMENTS, label: "Assignments (count)" },
  { key: COUNT_METRIC, label: "Number of sites" },
];

// Ranking y Distribución operan centro a centro: "rankear centros por número de
// centros" no significa nada, así que __count__ no se ofrece ahí.
export const SITE_METRIC_OPTIONS: MetricOption[] = METRIC_OPTIONS.filter(
  (o) => o.key !== COUNT_METRIC
);

export default function MetricPicker({
  metricKey, options, coverage, onChange,
}: {
  metricKey: string;
  options: MetricOption[];
  coverage: Coverage;
  onChange: (key: string) => void;
}) {
  const label = options.find((o) => o.key === metricKey)?.label ?? metricKey;
  const partial = coverage.missing > 0;
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <label className="flex items-center gap-2">
        <span className="text-gray-600">Metric</span>
        <select
          data-testid="chart-metric-select"
          className="border rounded-md px-2 py-1"
          value={metricKey}
          onChange={(e) => onChange(e.target.value)}
        >
          {options.map((o) => (
            <option key={o.key} value={o.key}>{o.label}</option>
          ))}
        </select>
      </label>
      <span
        data-testid="chart-coverage"
        className={partial ? "text-amber-700" : "text-gray-500"}
      >
        {coverage.withData} of {coverage.total} sites report {label.toLowerCase()}
        {partial ? ` · ${coverage.missing} with no data, excluded` : ""}
      </span>
    </div>
  );
}
