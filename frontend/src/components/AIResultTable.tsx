// src/components/AIResultTable.tsx
import React, { useEffect } from "react";

type Props = {
  columns: { key: string; label?: string }[];
  rows: Array<Record<string, any>>;
  onPersist?: (columns: Props["columns"], rows: Props["rows"]) => void;
  onRowClick?: (row: Record<string, any>) => void;
};

export default function AIResultTable({ columns, rows, onRowClick }: Props) {
  useEffect(() => {
    // notifica al parent para que pueda persistir/derivar account_ids
    if (typeof window !== "undefined" && rows?.length && columns?.length) {
      // noop, parent engancha onPersist si quiere
    }
  }, [columns, rows]);

  return (
    <div data-testid="ai-result-table" className="overflow-auto border rounded-lg">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50">
          <tr>
            {columns.map((c) => (
              <th key={c.key} className="px-3 py-2 text-left font-semibold text-gray-700 whitespace-nowrap">
                {c.label || c.key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr
              key={i}
              data-testid="ai-result-row"
              className={`${i % 2 ? "bg-white" : "bg-gray-50/50"} ${onRowClick ? "cursor-pointer hover:bg-blue-50" : ""}`}
              onClick={() => onRowClick?.(r)}
            >
              {columns.map((c) => (
                <td key={c.key} data-testid="ai-result-cell" className="px-3 py-2 whitespace-nowrap">
                  {String(r[c.key] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
