import React from "react";
import {
  BarChart, Bar,
  LineChart, Line,
  CartesianGrid, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer,
} from "recharts";

type ChartType = "bar" | "line";

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
}) {
  if (!open) return null;

  const Empty = data.length === 0 || yKeys.length === 0;
  const label = (k: string) => labelByKey?.get(k) ?? k.replace(/^sf\./, "");

  const Label = ({ children }: { children: React.ReactNode }) => (
    <span className="text-xs font-medium text-gray-700 mr-2">{children}</span>
  );

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
          <button onClick={onClose} className="rounded-md border px-3 py-1.5 text-sm hover:bg-white">
            Close
          </button>
        </div>

        {/* Controls */}
        <div className="px-5 py-3 border-b bg-white">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <Label>Type</Label>
              <Pill active={type === "bar"} onClick={() => onChangeType?.("bar")}>Bar</Pill>
              <Pill active={type === "line"} onClick={() => onChangeType?.("line")}>Line</Pill>
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
          </div>
        </div>

        {/* Chart */}
        <div className="flex-1 p-4">
          {Empty ? (
            <div className="h-full flex items-center justify-center text-gray-500 text-sm">
              Select at least one Y series.
            </div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              {type === "bar" ? (
                <BarChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey={xKey} tickFormatter={(v) => String(v)} />
                  <YAxis />
                  <Tooltip
                    formatter={(val: any, name: string) => [val, label(name)]}
                    labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                  />
                  <Legend
                    formatter={(value) => label(String(value))}
                  />
                  {yKeys.map((k) => (
                    <Bar key={k} dataKey={k} /* no color explícito */ />
                  ))}
                </BarChart>
              ) : (
                <LineChart data={data}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey={xKey} tickFormatter={(v) => String(v)} />
                  <YAxis />
                  <Tooltip
                    formatter={(val: any, name: string) => [val, label(name)]}
                    labelFormatter={(lbl) => `${label(xKey)}: ${lbl}`}
                  />
                  <Legend formatter={(value) => label(String(value))} />
                  {yKeys.map((k) => (
                    <Line key={k} type="monotone" dataKey={k} dot={false} /* sin color explícito */ />
                  ))}
                </LineChart>
              )}
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}