// src/components/AIResultTable.tsx
import React, { useState, useMemo } from "react";

type Props = {
  columns: { key: string; label?: string }[];
  rows: Array<Record<string, any>>;
  onPersist?: (columns: Props["columns"], rows: Props["rows"]) => void;
  onRowClick?: (row: Record<string, any>) => void;
};

const PAGE_SIZE = 20;
const INNODIA_BLUE = "#0072CE";

function smartCell(val: any): React.ReactNode {
  if (val === null || val === undefined || val === "") return <span className="text-gray-400">—</span>;
  const str = String(val);
  if (str === "true" || str.toLowerCase() === "yes") {
    return <span className="inline-block px-2 py-0.5 rounded-full text-xs font-semibold bg-green-100 text-green-700">Yes</span>;
  }
  if (str === "false" || str.toLowerCase() === "no") {
    return <span className="inline-block px-2 py-0.5 rounded-full text-xs font-semibold bg-red-100 text-red-600">No</span>;
  }
  // Number (but not a long id/code)
  if (str.length <= 12 && !isNaN(Number(str)) && str.trim() !== "") {
    return <span className="font-mono tabular-nums">{Number(str).toLocaleString()}</span>;
  }
  return str;
}

type SortDir = "asc" | "desc" | null;

export default function AIResultTable({ columns, rows, onRowClick }: Props) {
  const [page, setPage] = useState(0);
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);

  const sorted = useMemo(() => {
    if (!sortKey || !sortDir) return rows;
    return [...rows].sort((a, b) => {
      const av = a[sortKey] ?? "";
      const bv = b[sortKey] ?? "";
      const an = Number(av), bn = Number(bv);
      let cmp: number;
      if (!isNaN(an) && !isNaN(bn)) {
        cmp = an - bn;
      } else {
        cmp = String(av).localeCompare(String(bv));
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [rows, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages - 1);
  const pageRows = sorted.slice(safePage * PAGE_SIZE, (safePage + 1) * PAGE_SIZE);

  function handleSort(key: string) {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
    } else if (sortDir === "asc") {
      setSortDir("desc");
    } else {
      setSortKey(null);
      setSortDir(null);
    }
    setPage(0);
  }

  function sortIcon(key: string) {
    if (sortKey !== key) return <span className="ml-1 opacity-30 text-xs">⇅</span>;
    return <span className="ml-1 text-white text-xs">{sortDir === "asc" ? "↑" : "↓"}</span>;
  }

  // Pagination page numbers with ellipsis
  function pageNumbers(): (number | "...")[] {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i);
    const cur = safePage;
    const pages: (number | "...")[] = [0];
    if (cur > 2) pages.push("...");
    for (let p = Math.max(1, cur - 1); p <= Math.min(totalPages - 2, cur + 1); p++) pages.push(p);
    if (cur < totalPages - 3) pages.push("...");
    pages.push(totalPages - 1);
    return pages;
  }

  const from = sorted.length === 0 ? 0 : safePage * PAGE_SIZE + 1;
  const to = Math.min((safePage + 1) * PAGE_SIZE, sorted.length);

  if (!columns.length) return null;

  return (
    <div data-testid="ai-result-table" className="flex flex-col gap-2 text-sm">
      {/* Table */}
      <div className="overflow-auto rounded-lg border border-gray-200 shadow-sm">
        <table className="min-w-full border-collapse">
          <thead>
            <tr style={{ backgroundColor: INNODIA_BLUE }}>
              {columns.map((c) => (
                <th
                  key={c.key}
                  onClick={() => handleSort(c.key)}
                  className="px-3 py-2.5 text-left text-white font-semibold whitespace-nowrap cursor-pointer select-none hover:brightness-110 transition-all"
                >
                  {c.label || c.key}
                  {sortIcon(c.key)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-3 py-6 text-center text-gray-400">
                  No results
                </td>
              </tr>
            ) : (
              pageRows.map((r, i) => (
                <tr
                  key={i}
                  data-testid="ai-result-row"
                  className={`border-t border-gray-100 ${i % 2 === 0 ? "bg-white" : "bg-blue-50/30"} ${onRowClick ? "cursor-pointer hover:bg-blue-100/60" : "hover:bg-gray-50"} transition-colors`}
                  onClick={() => onRowClick?.(r)}
                >
                  {columns.map((c) => (
                    <td key={c.key} data-testid="ai-result-cell" className="px-3 py-2 whitespace-nowrap">
                      {smartCell(r[c.key])}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Footer: row count + pagination */}
      {sorted.length > 0 && (
        <div className="flex items-center justify-between px-1 text-xs text-gray-500">
          <span>
            {from}–{to} of <strong>{sorted.length}</strong> rows
          </span>
          {totalPages > 1 && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => setPage(p => Math.max(0, p - 1))}
                disabled={safePage === 0}
                className="px-2 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-100 transition-colors"
              >
                ←
              </button>
              {pageNumbers().map((p, idx) =>
                p === "..." ? (
                  <span key={`ellipsis-${idx}`} className="px-1">…</span>
                ) : (
                  <button
                    key={p}
                    onClick={() => setPage(p as number)}
                    className={`px-2 py-1 rounded border transition-colors ${
                      p === safePage
                        ? "text-white font-semibold border-transparent"
                        : "border-gray-200 hover:bg-gray-100"
                    }`}
                    style={p === safePage ? { backgroundColor: INNODIA_BLUE } : undefined}
                  >
                    {(p as number) + 1}
                  </button>
                )
              )}
              <button
                onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
                disabled={safePage === totalPages - 1}
                className="px-2 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-100 transition-colors"
              >
                →
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
