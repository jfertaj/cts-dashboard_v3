// src/pages/MembersView.tsx
import React, { useEffect, useMemo, useState } from "react";
import {
  ColumnDef,
  SortingState,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  useReactTable,
  flexRender,
} from "@tanstack/react-table";
import { displayCountry, ISO2_COUNTRY } from "../lib/countryUtils";
import MemberDetailsModal from "../components/MemberDetailsModal";
import MemberMapView from "../components/MemberMapView";

// ── Types ────────────────────────────────────────────────────────────────────

type MemberRow = {
  account_id: string;
  account_name: string;
  country: string;  // ISO2
  city: string;
  lat?: number | null;
  lng?: number | null;
  data: Record<string, any>;
};

// ── Role field constants ─────────────────────────────────────────────────────

const PROPOSED_ROLES = [
  { key: "sf.Clinical_Site_CS__c",                        label: "CS" },
  { key: "sf.C_Deliver_Clinical_Grade_Services__c",       label: "DxLab" },
  { key: "sf.C_Perform_Cutting_Edge__c",                  label: "LAB" },
  { key: "sf.C_Contribute_as_a_Patient_Organization__c",  label: "Patient Org" },
] as const;

const VALIDATED_ROLES = [
  { key: "sf.Clinical_Site_CS_validated__c",              label: "Val. CS" },
  { key: "sf.Clinical_Trial_Site_CTS_validated__c",       label: "Val. CTS" },
  { key: "sf.Diagnostic_Lab_DxLab_validated__c",          label: "Val. DxLab" },
  { key: "sf.Research_Mechanistic_Lab_LAB_validated__c",  label: "Val. LAB" },
  { key: "sf.Patient_Organization_validated__c",          label: "Val. Patient Org" },
] as const;

// ── Helpers ──────────────────────────────────────────────────────────────────

function buildFilterGroup(
  filterName: string,
  filterCountry: string,
  filterLevel: string,
  filterProposed: string[],
  filterValidated: string[],
) {
  const rules: any[] = [];

  if (filterName.trim()) {
    rules.push({ field: "sf.Name", operator: "contains", value: filterName.trim() });
  }
  if (filterCountry) {
    rules.push({ field: "site.country", operator: "equals", value: filterCountry });
  }
  if (filterLevel) {
    // Match against whichever level field has data
    rules.push({
      logic: "OR",
      rules: [
        { field: "sf.C_Level_of_Membership__c", operator: "equals", value: filterLevel },
        { field: "sf.C_Membership__c", operator: "equals", value: filterLevel },
      ],
    });
  }
  for (const key of filterProposed) {
    rules.push({ field: key, operator: "equals", value: true });
  }
  for (const key of filterValidated) {
    rules.push({ field: key, operator: "equals", value: true });
  }

  return { logic: "AND", rules };
}

// ── RoleBadge ────────────────────────────────────────────────────────────────

function RoleBadge({ label, active }: { label: string; active: boolean }) {
  if (!active) return <span className="text-gray-300 text-xs">—</span>;
  return (
    <span className="inline-block bg-green-100 text-green-800 border border-green-200 rounded-full px-1.5 py-0.5 text-xs font-medium">
      {label}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function MembersView() {
  // Data state
  const [allRows, setAllRows] = useState<MemberRow[]>([]);
  const [viewRows, setViewRows] = useState<MemberRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Filter state
  const [filterName, setFilterName] = useState("");
  const [filterCountry, setFilterCountry] = useState("");
  const [filterLevel, setFilterLevel] = useState("");
  const [filterProposed, setFilterProposed] = useState<string[]>([]);
  const [filterValidated, setFilterValidated] = useState<string[]>([]);

  // Table state
  const [sorting, setSorting] = useState<SortingState>([]);
  const [pageIndex, setPageIndex] = useState(0);
  const PAGE_SIZE = 25;

  // Map toggle
  const [showMap, setShowMap] = useState(true);

  // Detail modal state
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailName, setDetailName] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  // ── Bootstrap load ───────────────────────────────────────────────────────

  useEffect(() => {
    let alive = true;
    const run = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        const res = await fetch("/api/members/bootstrap", { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (alive) {
          setAllRows(json.rows ?? []);
          setViewRows(json.rows ?? []);
        }
      } catch (e: any) {
        if (alive) setLoadError(e?.message || "Failed to load members");
      } finally {
        if (alive) setLoading(false);
      }
    };
    run();
    return () => { alive = false; };
  }, []);

  // ── Country list derived from loaded data ────────────────────────────────

  const countryOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const r of allRows) if (r.country) seen.add(r.country);
    return Array.from(seen)
      .sort((a, b) => displayCountry(a).localeCompare(displayCountry(b)));
  }, [allRows]);

  const levelOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const r of allRows) {
      // Try both membership level fields — one of them may be populated
      const lvl = r.data["sf.C_Level_of_Membership__c"] || r.data["sf.C_Membership__c"];
      if (lvl) seen.add(String(lvl));
    }
    return Array.from(seen).sort();
  }, [allRows]);

  // ── Search / filter ──────────────────────────────────────────────────────

  const hasFilters =
    filterName.trim() !== "" ||
    filterCountry !== "" ||
    filterLevel !== "" ||
    filterProposed.length > 0 ||
    filterValidated.length > 0;

  const handleSearch = async () => {
    if (!hasFilters) {
      setViewRows(allRows);
      setPageIndex(0);
      return;
    }
    setSearching(true);
    try {
      const fg = buildFilterGroup(filterName, filterCountry, filterLevel, filterProposed, filterValidated);
      const res = await fetch("/api/members/search", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filters: fg }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      setViewRows(json.rows ?? []);
      setPageIndex(0);
    } catch (e: any) {
      // Fallback: client-side filter on cached rows
      setViewRows(allRows);
    } finally {
      setSearching(false);
    }
  };

  const handleClear = () => {
    setFilterName("");
    setFilterCountry("");
    setFilterLevel("");
    setFilterProposed([]);
    setFilterValidated([]);
    setViewRows(allRows);
    setPageIndex(0);
  };

  const toggleProposed = (key: string) => {
    setFilterProposed((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  };

  const toggleValidated = (key: string) => {
    setFilterValidated((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key]
    );
  };

  // ── Open detail modal ────────────────────────────────────────────────────

  const openDetail = (row: MemberRow) => {
    setDetailId(row.account_id);
    setDetailName(row.account_name);
    setDetailOpen(true);
  };

  // ── TanStack columns ─────────────────────────────────────────────────────

  const columns = useMemo<ColumnDef<MemberRow>[]>(() => [
    {
      id: "account_name",
      header: "Institution",
      accessorFn: (r) => r.account_name,
      cell: ({ row }) => (
        <button
          className="text-left text-blue-700 hover:underline font-medium text-sm"
          onClick={() => openDetail(row.original)}
        >
          {row.original.account_name}
        </button>
      ),
      size: 220,
    },
    {
      id: "country",
      header: "Country",
      accessorFn: (r) => r.country,
      cell: ({ getValue }) => {
        const iso2 = getValue() as string;
        return (
          <span title={iso2} className="text-sm">
            {displayCountry(iso2) || iso2 || "—"}
          </span>
        );
      },
      size: 120,
    },
    {
      id: "city",
      header: "City",
      accessorFn: (r) => r.city || "",
      cell: ({ getValue }) => <span className="text-sm">{(getValue() as string) || "—"}</span>,
      size: 120,
    },
    {
      id: "level",
      header: "Level",
      accessorFn: (r) => r.data["sf.C_Level_of_Membership__c"] || r.data["sf.C_Membership__c"] || "",
      cell: ({ getValue }) => <span className="text-sm text-gray-700">{(getValue() as string) || "—"}</span>,
      size: 130,
    },
    {
      id: "sites_count",
      header: "Sites",
      accessorFn: (r) => r.data["extra.SubAccountsCount"] ?? 0,
      cell: ({ getValue }) => {
        const n = getValue() as number;
        return (
          <span className={`text-sm font-medium ${n > 0 ? "text-blue-700" : "text-gray-400"}`}>
            {n}
          </span>
        );
      },
      size: 60,
    },
    {
      id: "contacts_count",
      header: "Contacts",
      accessorFn: (r) => r.data["extra.ContactsCount"] ?? 0,
      cell: ({ getValue }) => {
        const n = getValue() as number;
        return (
          <span className={`text-sm font-medium ${n > 0 ? "text-purple-700" : "text-gray-400"}`}>
            {n}
          </span>
        );
      },
      size: 70,
    },
    {
      id: "proposed_roles",
      header: "Proposed Roles",
      cell: ({ row }) => {
        const d = row.original.data;
        const active = PROPOSED_ROLES.filter((r) => d[r.key]).map((r) => r.label);
        if (active.length === 0) return <span className="text-gray-300 text-xs">—</span>;
        return (
          <div className="flex flex-wrap gap-1">
            {active.map((l) => (
              <span key={l} className="text-xs bg-blue-50 text-blue-700 border border-blue-100 rounded-full px-1.5 py-0.5">
                {l}
              </span>
            ))}
          </div>
        );
      },
      size: 160,
    },
    {
      id: "validated_roles",
      header: "Validated Roles",
      cell: ({ row }) => {
        const d = row.original.data;
        const active = VALIDATED_ROLES.filter((r) => d[r.key]).map((r) => r.label);
        if (active.length === 0) return <span className="text-gray-300 text-xs">—</span>;
        return (
          <div className="flex flex-wrap gap-1">
            {active.map((l) => (
              <span key={l} className="text-xs bg-green-50 text-green-700 border border-green-100 rounded-full px-1.5 py-0.5">
                {l}
              </span>
            ))}
          </div>
        );
      },
      size: 200,
    },
  ], [allRows]);

  // ── Table instance ───────────────────────────────────────────────────────

  const table = useReactTable({
    data: viewRows,
    columns,
    state: { sorting, pagination: { pageIndex, pageSize: PAGE_SIZE } },
    onSortingChange: setSorting,
    onPaginationChange: (updater) => {
      const next = typeof updater === "function" ? updater({ pageIndex, pageSize: PAGE_SIZE }) : updater;
      setPageIndex(next.pageIndex);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    manualPagination: false,
  });

  const pageCount = table.getPageCount();
  const rows = table.getRowModel().rows;

  // ── Render ───────────────────────────────────────────────────────────────

  return (
    <div data-testid="members-view" className="flex gap-4 items-start">
      {/* ── Filter panel ────────────────────────────────────────────────── */}
      <aside data-testid="members-filter-panel" className="w-64 flex-shrink-0 bg-white rounded-xl border shadow-sm p-4 space-y-4">
        <div className="text-sm font-semibold text-gray-700">Filters</div>

        {/* Institution name */}
        <div>
          <label className="text-xs text-gray-500 block mb-1">Institution name</label>
          <input
            data-testid="members-filter-name"
            type="text"
            value={filterName}
            onChange={(e) => setFilterName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="Search…"
            className="w-full text-sm border rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
          />
        </div>

        {/* Country */}
        <div>
          <label className="text-xs text-gray-500 block mb-1">Country</label>
          <select
            data-testid="members-filter-country"
            value={filterCountry}
            onChange={(e) => setFilterCountry(e.target.value)}
            className="w-full text-sm border rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400 bg-white"
          >
            <option value="">All countries</option>
            {countryOptions.map((iso) => (
              <option key={iso} value={iso}>{displayCountry(iso)}</option>
            ))}
          </select>
        </div>

        {/* Membership level */}
        <div>
          <label className="text-xs text-gray-500 block mb-1">Membership level</label>
          <select
            data-testid="members-filter-level"
            value={filterLevel}
            onChange={(e) => setFilterLevel(e.target.value)}
            className="w-full text-sm border rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400 bg-white"
          >
            <option value="">All levels</option>
            {levelOptions.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </div>

        {/* Proposed roles */}
        <div>
          <div className="text-xs text-gray-500 mb-1.5 font-medium">Proposed Roles</div>
          <div className="space-y-1.5">
            {PROPOSED_ROLES.map((r) => (
              <label key={r.key} className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                <input
                  data-testid={`members-filter-proposed-${r.label}`}
                  type="checkbox"
                  className="rounded text-blue-600"
                  checked={filterProposed.includes(r.key)}
                  onChange={() => toggleProposed(r.key)}
                />
                {r.label}
              </label>
            ))}
          </div>
        </div>

        {/* Validated roles */}
        <div>
          <div className="text-xs text-gray-500 mb-1.5 font-medium">Validated Roles</div>
          <div className="space-y-1.5">
            {VALIDATED_ROLES.map((r) => (
              <label key={r.key} className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                <input
                  data-testid={`members-filter-validated-${r.label}`}
                  type="checkbox"
                  className="rounded text-blue-600"
                  checked={filterValidated.includes(r.key)}
                  onChange={() => toggleValidated(r.key)}
                />
                {r.label}
              </label>
            ))}
          </div>
        </div>

        {/* Buttons */}
        <div className="flex gap-2 pt-1">
          <button
            data-testid="members-search-btn"
            onClick={handleSearch}
            disabled={searching || loading}
            className="flex-1 rounded-md bg-[#0072CE] text-white text-sm px-3 py-1.5 hover:bg-[#005fa3] disabled:opacity-50 transition"
          >
            {searching ? "Searching…" : "Search"}
          </button>
          <button
            data-testid="members-clear-btn"
            onClick={handleClear}
            disabled={searching || loading}
            className="rounded-md border text-sm px-3 py-1.5 hover:bg-gray-50 disabled:opacity-50 transition"
          >
            Clear
          </button>
        </div>
      </aside>

      {/* ── Main content ─────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0 space-y-3">
        {/* Header bar */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-lg font-semibold text-gray-900">INNODIA Member Institutions</h1>
            <p data-testid="members-results-count" className="text-xs text-gray-500 mt-0.5">
              {loading
                ? "Loading…"
                : `${viewRows.length.toLocaleString()} institution${viewRows.length !== 1 ? "s" : ""}${viewRows.length !== allRows.length ? ` (of ${allRows.length})` : ""}`}
            </p>
          </div>
          <button
            data-testid="members-map-toggle"
            onClick={() => setShowMap((v) => !v)}
            className={`px-3 py-1.5 rounded-md border text-sm transition ${
              showMap
                ? "bg-[#0072CE] text-white border-[#0072CE]"
                : "bg-white text-gray-700 hover:bg-gray-50"
            }`}
          >
            {showMap ? "Hide Map" : "Show Map"}
          </button>
        </div>

        {/* Map */}
        {showMap && (
          <div data-testid="members-map">
          <MemberMapView
            rows={viewRows}
            onOpenDetail={(id, name) => {
              setDetailId(id);
              setDetailName(name);
              setDetailOpen(true);
            }}
          />
          </div>
        )}

        {/* Error */}
        {loadError && (
          <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            ⚠️ {loadError}
          </div>
        )}

        {/* Table */}
        <div className="bg-white rounded-xl border shadow-sm overflow-hidden">
          {loading ? (
            <div className="py-16 text-center text-sm text-gray-500">Loading member data…</div>
          ) : viewRows.length === 0 ? (
            <div className="py-16 text-center text-sm text-gray-500">No members found matching the selected filters.</div>
          ) : (
            <div className="overflow-x-auto">
              <table data-testid="members-table" className="w-full text-sm">
                <thead>
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id} className="border-b bg-gray-50">
                      {hg.headers.map((h) => (
                        <th
                          key={h.id}
                          className={`px-3 py-2.5 text-left text-xs font-medium text-gray-600 uppercase tracking-wide whitespace-nowrap ${
                            h.column.getCanSort() ? "cursor-pointer select-none hover:text-gray-900" : ""
                          }`}
                          style={{ width: h.column.columnDef.size }}
                          onClick={h.column.getToggleSortingHandler()}
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {h.column.getIsSorted() === "asc" && " ↑"}
                          {h.column.getIsSorted() === "desc" && " ↓"}
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody>
                  {rows.map((row, i) => (
                    <tr
                      key={row.id}
                      data-testid="members-table-row"
                      className={`border-b last:border-0 hover:bg-blue-50/30 transition-colors ${i % 2 === 0 ? "" : "bg-gray-50/50"}`}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id} className="px-3 py-2.5 align-top">
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {!loading && viewRows.length > PAGE_SIZE && (
            <div className="flex items-center justify-between px-4 py-2.5 border-t bg-gray-50 text-xs text-gray-600">
              <span>
                Page {pageIndex + 1} of {pageCount} — showing {rows.length} of {viewRows.length}
              </span>
              <div className="flex gap-1">
                <button
                  className="px-2.5 py-1 rounded border hover:bg-white disabled:opacity-40"
                  disabled={!table.getCanPreviousPage()}
                  onClick={() => setPageIndex((p) => Math.max(0, p - 1))}
                >
                  ‹ Prev
                </button>
                <button
                  className="px-2.5 py-1 rounded border hover:bg-white disabled:opacity-40"
                  disabled={!table.getCanNextPage()}
                  onClick={() => setPageIndex((p) => Math.min(pageCount - 1, p + 1))}
                >
                  Next ›
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Detail modal */}
      <MemberDetailsModal
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        accountId={detailId}
        accountName={detailName}
      />
    </div>
  );
}
