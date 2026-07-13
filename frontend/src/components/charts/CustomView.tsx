import React, { useRef } from "react";
import {
  BarChart, Bar,
  LineChart, Line,
  CartesianGrid, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer,
  PieChart, Pie, Cell,
} from "recharts";
import html2canvas from "html2canvas";
import { toPieSlices, PIE_TOTAL_KEY } from "../../lib/chartDataset";

export type ChartType = "bar" | "line" | "pie";

/** Entradas de leyenda por defecto. Lo fija el dueño del estado, no esta vista. */
export const DEFAULT_LEGEND_MAX = 8;

export default function CustomView({
  title,
  data,
  xKey,
  yKeys,
  type = "bar",
  xCandidates = [],
  yCandidates = [],
  labelByKey,
  stacked,
  onChangeType,
  onChangeXKey,
  onToggleYKey,
  onChangeStacked,
  legendMax,
  onChangeLegendMax,
}: {
  /** Título del modal: da nombre al PNG exportado (`<título>_chart.png`). */
  title: string;
  data: Array<Record<string, any>>;
  xKey: string;
  yKeys: string[];
  type?: ChartType;
  xCandidates?: string[];
  yCandidates?: string[];
  labelByKey?: Map<string, string>;
  /** Stacked vs Grouped. Controlado por el dueño: esta vista se desmonta al
   *  cambiar de pestaña y el modo elegido no debe perderse por el camino. */
  stacked: boolean;
  onChangeType?: (v: ChartType) => void;
  onChangeXKey?: (v: string) => void;
  onToggleYKey?: (v: string) => void;
  onChangeStacked?: (v: boolean) => void;
  /** Máximo de entradas en la leyenda. Controlado por el dueño por lo mismo que
   *  `stacked`: esta vista se desmonta al cambiar de pestaña. */
  legendMax: number;
  onChangeLegendMax: (n: number) => void;
}) {
  const chartRef = useRef<HTMLDivElement>(null);

  const label = (k: string) => {
    if (k === '__count__') return 'Count (rows)';
    if (k === 'sf.Account.Name') return 'Account Name';
    if (k === 'sf.Account.Id') return 'Account Id';
    if (k === 'country') return 'Country';
    if (k === 'city') return 'City';
    return labelByKey?.get(k) ?? k.replace(/^sf\./, "");
  };

  // Color palette for charts
  const COLORS = ["#0072CE", "#00A99D", "#F37021", "#8E44AD", "#E74C3C", "#3498DB", "#2ECC71", "#F39C12"];

  // Pie: las porciones las monta `toPieSlices` (lib/chartDataset), que deja fuera
  // a quien no reporta ninguna serie en vez de pintarlo como porción de cero.
  // Con varias series Y la porción es el total, bajo la clave PIE_TOTAL_KEY.
  const y0 = yKeys.length > 1 ? PIE_TOTAL_KEY : (yKeys[0] ?? "value");
  const plotData = type === "pie" ? toPieSlices(data, xKey, yKeys) : [];
  const showLegend = plotData.length <= 25;

  // Un pie sin porciones (nadie reporta la métrica) se pintaría como un lienzo en
  // blanco sin explicación: hay que decirlo, no dejar el hueco mudo.
  const emptyMessage =
    data.length === 0 || yKeys.length === 0
      ? "Select at least one Y series."
      : type === "pie" && plotData.length === 0
        ? "Ningún centro reporta esta métrica: no hay porciones que pintar."
        : null;

  const Label = ({ children }: { children: React.ReactNode }) => (
    <span className="text-xs font-medium text-gray-700 mr-2">{children}</span>
  );

  // Legend payload builders (limit to legendMax)
  const legendPayloadForSeries = () =>
    (yKeys || []).slice(0, Math.max(0, legendMax)).map((k, idx) => ({
      id: k,
      value: label(k),
      color: COLORS[idx % COLORS.length],
      type: 'square' as const,
    }));

  const legendPayloadForPie = () =>
    (plotData || []).slice(0, Math.max(0, legendMax)).map((r: any, idx: number) => ({
      id: String(r?.[xKey] ?? idx),
      value: String(r?.[xKey] ?? ''),
      color: r?._color || COLORS[idx % COLORS.length],
      type: 'square' as const,
    }));

  const Pill = ({ active, children, onClick }: { active: boolean; children: React.ReactNode; onClick?: () => void }) => (
    <button
      onClick={onClick}
      className={`px-2 py-1 rounded-full border text-xs ${
        active ? "bg-blue-600 text-white border-blue-600" : "bg-white text-gray-700 hover:bg-gray-50"
      }`}
    >
      {children}
    </button>
  );

  const downloadChart = async (format: "png" | "svg") => {
    if (!chartRef.current) return;

    try {
      if (format === "png") {
        const canvas = await html2canvas(chartRef.current, {
          backgroundColor: "#ffffff",
          scale: 2, // Higher resolution
          logging: false,
        });
        const url = canvas.toDataURL("image/png");
        const link = document.createElement("a");
        link.download = `${title.replace(/\s+/g, "_")}_chart.png`;
        link.href = url;
        link.click();
      } else {
        // For SVG, we'd need to convert the Recharts component to SVG
        // This is a simplified version - for production, consider using a library
        alert("SVG export requires additional setup. PNG export is recommended.");
      }
    } catch (error) {
      console.error("Error downloading chart:", error);
      alert("Failed to download chart. Please try again.");
    }
  };

  return (
    <div className="flex flex-col" data-testid="chart-custom-builder">
      {/* Controls */}
      <div className="pb-3 border-b bg-white">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2">
            <Label>Type</Label>
            <Pill active={type === "bar"} onClick={() => onChangeType?.("bar")}>Bar</Pill>
            <Pill active={type === "line"} onClick={() => onChangeType?.("line")}>Line</Pill>
            <Pill active={type === "pie"} onClick={() => onChangeType?.("pie")}>Pie</Pill>
          </div>

          <div className="flex items-center gap-2">
            <Label>X</Label>
            <select
              className="border rounded-md text-sm px-2 py-1"
              value={xKey}
              onChange={(e) => onChangeXKey?.(e.target.value)}
            >
              {xCandidates.map((k) => (
                <option key={k} value={k}>{label(k)}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2 flex-wrap">
            <Label>Y series</Label>
            {yCandidates.length === 0 ? (
              <span className="text-xs text-gray-500">No numeric columns detected.</span>
            ) : (
              yCandidates.map((k) => (
                <label key={k} className="inline-flex items-center gap-1 text-xs border rounded-md px-2 py-1">
                  <input
                    type="checkbox"
                    className="rounded"
                    checked={yKeys.includes(k)}
                    onChange={() => onToggleYKey?.(k)}
                  />
                  {label(k)}
                </label>
              ))
            )}
          </div>

          {type === "bar" && yKeys.length > 1 && (
            <div className="flex items-center gap-2">
              <Label>Mode</Label>
              <Pill active={stacked} onClick={() => onChangeStacked?.(true)}>Stacked</Pill>
              <Pill active={!stacked} onClick={() => onChangeStacked?.(false)}>Grouped</Pill>
            </div>
          )}

          <div className="flex items-center gap-2">
            <Label>Legend max</Label>
            <select
              data-testid="chart-legend-max"
              className="border rounded-md text-sm px-2 py-1"
              value={legendMax}
              onChange={(e) => onChangeLegendMax(parseInt(e.target.value, 10) || 8)}
            >
              {[4,6,8,10,12,16,20].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </div>

          <button
            onClick={() => downloadChart("png")}
            className="ml-auto rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50 flex items-center gap-1.5"
            title="Download as PNG"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
            </svg>
            Download
          </button>
        </div>
      </div>

      {/* Chart */}
      <div ref={chartRef} className="p-4 bg-white">
        {emptyMessage ? (
          <div data-testid="chart-custom-empty" className="h-[420px] flex items-center justify-center text-gray-500 text-sm">
            {emptyMessage}
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={420}>
            {type === "bar" ? (
              // bottom margin just enough for angled tick labels; legend sits inside SVG
              <BarChart data={data} margin={{ top: 16, right: 30, left: 60, bottom: 16 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                <XAxis
                  dataKey={xKey}
                  tickFormatter={(v) => String(v)}
                  angle={-40}
                  textAnchor="end"
                  height={80}
                  tick={{ fontSize: 11 }}
                  interval={0}
                  label={{ value: label(xKey), position: "insideBottom", offset: -4, style: { fontSize: 13, fontWeight: "bold" } }}
                />
                <YAxis
                  tick={{ fontSize: 12 }}
                  width={60}
                  label={{ value: yKeys.length === 1 ? label(yKeys[0]) : "Values", angle: -90, position: "insideLeft", offset: 10, style: { fontSize: 13, fontWeight: "bold" } }}
                />
                <Tooltip
                  formatter={(val: any, name: string) => [val, label(name)]}
                  labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                  contentStyle={{ backgroundColor: "rgba(255, 255, 255, 0.95)", border: "1px solid #ccc", borderRadius: "4px" }}
                />
                {yKeys.length > 1 && (
                  <Legend
                    formatter={(value) => label(value)}
                    verticalAlign="bottom"
                    align="center"
                    wrapperStyle={{ paddingTop: 6 }}
                    payload={legendPayloadForSeries() as any}
                  />
                )}
                {yKeys.map((k, idx) => {
                  const isMulti = yKeys.length > 1;
                  const stackId = isMulti && stacked ? "stack" : undefined;
                  const radius: [number,number,number,number] = isMulti && stacked ? [0,0,0,0] : [3,3,0,0];
                  return (
                    <Bar key={k} dataKey={k} fill={COLORS[idx % COLORS.length]} radius={radius} stackId={stackId}>
                      {!isMulti && Array.isArray(data) && data.length > 0 && (
                        data.map((entry, i) => (
                          <Cell key={`cell-${k}-${i}`} fill={entry?._color || COLORS[idx % COLORS.length]} />
                        ))
                      )}
                    </Bar>
                  );
                })}
              </BarChart>
            ) : type === "line" ? (
              <LineChart data={data} margin={{ top: 16, right: 30, left: 60, bottom: 16 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                <XAxis
                  dataKey={xKey}
                  tickFormatter={(v) => String(v)}
                  angle={-40}
                  textAnchor="end"
                  height={80}
                  tick={{ fontSize: 11 }}
                  interval={0}
                  label={{ value: label(xKey), position: "insideBottom", offset: -4, style: { fontSize: 13, fontWeight: "bold" } }}
                />
                <YAxis
                  tick={{ fontSize: 12 }}
                  width={60}
                  label={{ value: yKeys.length === 1 ? label(yKeys[0]) : "Values", angle: -90, position: "insideLeft", offset: 10, style: { fontSize: 13, fontWeight: "bold" } }}
                />
                <Tooltip
                  formatter={(val: any, name: string) => [val, label(name)]}
                  labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                  contentStyle={{ backgroundColor: "rgba(255, 255, 255, 0.95)", border: "1px solid #ccc", borderRadius: "4px" }}
                />
                {yKeys.length > 1 && (
                  <Legend
                    formatter={(value) => label(value)}
                    verticalAlign="bottom"
                    align="center"
                    wrapperStyle={{ paddingTop: 6 }}
                    payload={legendPayloadForSeries() as any}
                  />
                )}
                {yKeys.map((k, idx) => (
                  <Line key={k} type="monotone" dataKey={k} stroke={COLORS[idx % COLORS.length]} strokeWidth={2} dot={{ r: 3 }} activeDot={{ r: 5 }} />
                ))}
              </LineChart>
            ) : (() => {
              // Pie: show inline labels only when few slices; scale radius responsively
              const pieTotal = plotData.reduce((s: number, r: any) => s + (Number(r[y0]) || 0), 0);
              const hasLabels = plotData.length <= 10;
              const pieCy  = hasLabels ? "38%" : "46%";
              const pieR   = hasLabels ? "30%" : "42%";
              const pieLabel = hasLabels
                ? (entry: any) => {
                    const pct = pieTotal > 0 ? (Number(entry[y0]) / pieTotal) * 100 : 0;
                    if (pct < 4) return "";
                    return `${String(entry[xKey] ?? "").slice(0, 22)}: ${entry[y0]}`;
                  }
                : false;
              return (
                <PieChart margin={{ top: 10, right: 20, left: 20, bottom: 10 }}>
                  <Pie
                    data={plotData}
                    dataKey={y0}
                    nameKey={xKey}
                    cx="50%"
                    cy={pieCy}
                    outerRadius={pieR}
                    label={pieLabel}
                    labelLine={hasLabels ? { stroke: "#999", strokeWidth: 1 } : false}
                  >
                    {plotData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry?._color || COLORS[index % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    formatter={(val: any, name: string) => [val, label(name)]}
                    contentStyle={{ backgroundColor: "rgba(255, 255, 255, 0.95)", border: "1px solid #ccc", borderRadius: "4px" }}
                  />
                  {showLegend && (
                    <Legend
                      formatter={(value) => String(value)}
                      verticalAlign="bottom"
                      align="center"
                      wrapperStyle={{ maxHeight: 100, overflowY: "auto", paddingTop: 8 }}
                      payload={legendPayloadForPie() as any}
                    />
                  )}
                </PieChart>
              );
            })()}
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
}
