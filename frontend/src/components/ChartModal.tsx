import React, { useEffect, useRef, useState } from "react";
import {
  BarChart, Bar,
  LineChart, Line,
  CartesianGrid, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer,
  PieChart, Pie, Cell,
} from "recharts";
import html2canvas from "html2canvas";

type ChartType = "bar" | "line" | "pie";

export default function ChartModal({
  open,
  onClose,
  title,
  data,
  xKey,
  yKeys,
  type = "bar",
  xCandidates = [],
  yCandidates = [],
  labelByKey,
  onChangeTitle,
  onChangeType,
  onChangeXKey,
  onToggleYKey,
  legendMax = 8,
  onChangeLegendMax,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  data: Array<Record<string, any>>;
  xKey: string;
  yKeys: string[];
  type?: ChartType;
  xCandidates?: string[];
  yCandidates?: string[];
  labelByKey?: Map<string, string>;
  onChangeTitle?: (v: string) => void;
  onChangeType?: (v: ChartType) => void;
  onChangeXKey?: (v: string) => void;
  onToggleYKey?: (v: string) => void;
  legendMax?: number; // Máximo de entradas en la leyenda (por defecto 8)
  onChangeLegendMax?: (n: number) => void;
}) {
  const chartRef = useRef<HTMLDivElement>(null);
  const [legendMaxUI, setLegendMaxUI] = useState<number>(legendMax ?? 8);
  useEffect(() => { setLegendMaxUI(legendMax ?? 8); }, [legendMax, open]);
  
  if (!open) return null;

  const Empty = data.length === 0 || yKeys.length === 0;
  const label = (k: string) => {
    if (k === 'sf.Account.Name') return 'Account Name';
    if (k === 'sf.Account.Id') return 'Account Id';
    if (k === 'country') return 'Country';
    if (k === 'city') return 'City';
    return labelByKey?.get(k) ?? k.replace(/^sf\./, "");
  };
  
  // Color palette for charts
  const COLORS = ["#0072CE", "#00A99D", "#F37021", "#8E44AD", "#E74C3C", "#3498DB", "#2ECC71", "#F39C12"];

  // Pie safety: normalize and cap slices
  // If multiple yKeys (stacked dataset), compute a total for pie use
  let y0 = (yKeys && yKeys.length > 0 ? yKeys[0] : "value");
  let plotData = Array.isArray(data) ? [...data] : [];
  let showLegend = true;
  let showSliceLabels = true;
  if (type === "pie") {
    if (Array.isArray(yKeys) && yKeys.length > 1) {
      const SUM = "__total__";
      plotData = (plotData || []).map((r: any) => {
        let s = 0;
        for (const k of yKeys) {
          const v = Number(String((r || {})[k] ?? 0).replace(/,/g, ""));
          s += Number.isFinite(v) ? v : 0;
        }
        return { ...r, [SUM]: s };
      });
      y0 = SUM; // use total for pie
    }
    // normalize numeric
    const norm = plotData.map((r) => {
      const raw = (r as any)[y0];
      let v = 0;
      try { v = Number(String(raw ?? 0).replace(/,/g, "")); if (!Number.isFinite(v)) v = 0; } catch { v = 0; }
      return { ...r, [y0]: v } as any;
    });
    norm.sort((a: any, b: any) => (b[y0] ?? 0) - (a[y0] ?? 0));
    const MAX_SLICES = 15;
    if (norm.length > MAX_SLICES) {
      const head = norm.slice(0, MAX_SLICES - 1);
      const tail = norm.slice(MAX_SLICES - 1);
      const others = tail.reduce((acc: number, r: any) => acc + (Number(r[y0]) || 0), 0);
      plotData = [...head, { ...(head[0] || {}), [xKey]: "Others", [y0]: others, _color: "#95A5A6" }];
    } else {
      plotData = norm;
    }
    showLegend = plotData.length <= 25;
    showSliceLabels = plotData.length <= 15;
  }

  const Label = ({ children }: { children: React.ReactNode }) => (
    <span className="text-xs font-medium text-gray-700 mr-2">{children}</span>
  );

  // Legend payload builders (limit to legendMax)
  const legendPayloadForSeries = () =>
    (yKeys || []).slice(0, Math.max(0, legendMaxUI)).map((k, idx) => ({
      id: k,
      value: label(k),
      color: COLORS[idx % COLORS.length],
      type: 'square' as const,
    }));

  const legendPayloadForPie = () =>
    (plotData || []).slice(0, Math.max(0, legendMaxUI)).map((r: any, idx: number) => ({
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
    <div className="fixed inset-0 z-[11000] flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-6xl h-[78vh] rounded-2xl bg-white shadow-2xl ring-1 ring-black/5 flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b bg-gray-50 rounded-t-2xl">
          <input
            className="font-semibold text-gray-900 truncate bg-transparent outline-none w-[60%]"
            value={title}
            onChange={(e) => onChangeTitle?.(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <button
              onClick={() => downloadChart("png")}
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-white flex items-center gap-1.5"
              title="Download as PNG"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              Download
            </button>
            <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-white">
              Close
            </button>
          </div>
        </div>

        {/* Controls */}
        <div className="px-5 py-3 border-b bg-white">
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

            <div className="flex items-center gap-2">
              <Label>Legend max</Label>
              <select
                className="border rounded-md text-sm px-2 py-1"
                value={legendMaxUI}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10) || 8;
                  setLegendMaxUI(n);
                  onChangeLegendMax?.(n);
                }}
              >
                {[4,6,8,10,12,16,20].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        {/* Chart */}
        <div ref={chartRef} className="flex-1 p-4 bg-white">
          {Empty ? (
            <div className="h-full flex items-center justify-center text-gray-500 text-sm">
              Select at least one Y series.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              {type === "bar" ? (
                <BarChart data={data} margin={{ top: 20, right: 24, left: 16, bottom: 180 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                  <XAxis 
                    dataKey={xKey} 
                    tickFormatter={(v) => String(v)} 
                    angle={-35}
                    textAnchor="end"
                    height={110}
                    tick={{ fontSize: 12 }}
                    label={{ value: label(xKey), position: "insideBottom", offset: -24, style: { fontSize: 14, fontWeight: "bold" } }}

                  />
                  <YAxis 
                    tick={{ fontSize: 12 }}
                    label={{ value: yKeys.length === 1 ? label(yKeys[0]) : "Values", angle: -90, position: "insideLeft", style: { fontSize: 14, fontWeight: "bold" } }}
                  />
                  <Tooltip
                    formatter={(val: any, name: string) => [val, label(name)]}
                    labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                    contentStyle={{ backgroundColor: "rgba(255, 255, 255, 0.95)", border: "1px solid #ccc", borderRadius: "4px" }}
                  />
                  <Legend
                    formatter={(value) => String(value)}
                    verticalAlign="bottom"
                    align="center"
                    wrapperStyle={{ maxHeight: 120, overflowY: "auto", paddingTop: 32 }}

                    payload={legendPayloadForSeries() as any}
                  />

                  {yKeys.map((k, idx) => (
                    <Bar key={k} dataKey={k} fill={COLORS[idx % COLORS.length]} radius={[4, 4, 0, 0]} stackId={yKeys.length > 1 ? "stack" : undefined}>
                      {/* Optional per-datum coloring via entry._color */}
                      {Array.isArray(data) && data.length > 0 && (
                        data.map((entry, i) => (
                          <Cell key={`cell-${k}-${i}`} fill={entry?._color || COLORS[idx % COLORS.length]} />
                        ))
                      )}
                    </Bar>
                  ))}
                </BarChart>
              ) : type === "line" ? (
                <LineChart data={data} margin={{ top: 20, right: 24, left: 16, bottom: 180 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e0e0e0" />
                  <XAxis 
                    dataKey={xKey} 
                    tickFormatter={(v) => String(v)}
                    angle={-35}
                    textAnchor="end"
                    height={70}
                    tick={{ fontSize: 12 }}
                    label={{ value: label(xKey), position: "insideBottom", offset: -12, style: { fontSize: 14, fontWeight: "bold" } }}
                  />
                  <YAxis 
                    tick={{ fontSize: 12 }}
                    label={{ value: yKeys.length === 1 ? label(yKeys[0]) : "Values", angle: -90, position: "insideLeft", style: { fontSize: 14, fontWeight: "bold" } }}
                  />
                  <Tooltip
                    formatter={(val: any, name: string) => [val, label(name)]}
                    labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                    contentStyle={{ backgroundColor: "rgba(255, 255, 255, 0.95)", border: "1px solid #ccc", borderRadius: "4px" }}
                  />
                  <Legend 
                    formatter={(value) => String(value)}
                    verticalAlign="bottom"
                    align="center"
                    wrapperStyle={{ maxHeight: 72, overflowY: "auto", paddingTop: 8 }}
                    payload={legendPayloadForSeries() as any}
                  />
                  {yKeys.map((k, idx) => (
                    <Line key={k} type="monotone" dataKey={k} stroke={COLORS[idx % COLORS.length]} strokeWidth={2} dot={{ r: 4 }} activeDot={{ r: 6 }} />
                  ))}
                </LineChart>
              ) : (
                <PieChart margin={{ top: 20, right: 24, left: 16, bottom: 160 }}>
                  <Pie
                    data={plotData}
                    dataKey={y0}
                    nameKey={xKey}
                    cx="50%"
                    cy="50%"
                    outerRadius={120}
                    label={showSliceLabels ? ((entry: any) => `${String(entry[xKey] ?? "").slice(0,28)}: ${entry[y0]}`) : false}
                    labelLine={showSliceLabels ? { stroke: "#999", strokeWidth: 1 } : false}
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
                      wrapperStyle={{ maxHeight: 120, overflowY: "auto", paddingTop: 32 }}
                      payload={legendPayloadForPie() as any}
                    />
                  )}
                </PieChart>
              )}
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}