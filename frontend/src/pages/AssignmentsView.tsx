import React, { useEffect, useState } from "react";
import { assignmentReport, assignmentReportOptions, ReportTable } from "../lib/api";

const STUDIES_DEFAULT = ["Baricade Delay (JAJJ)", "Baricade Preserve (JAJK)", "Safeguard", "Beta Preserve"];

function toCsv(t: ReportTable): string {
  const head = t.columns.map((c) => c.label || c.key).join(",");
  const esc = (v: any) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const body = t.rows.map((r) => t.columns.map((c) => esc(r[c.key])).join(",")).join("\n");
  return head + "\n" + body;
}

export default function AssignmentsView() {
  const [studies, setStudies] = useState<string[]>([]);
  const [stages, setStages] = useState<string[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [selStudies, setSelStudies] = useState<string[]>(STUDIES_DEFAULT);
  const [selStages, setSelStages] = useState<string[]>(["Activated"]);
  const [selRoles, setSelRoles] = useState<string[]>([]);  // empty = all roles
  const [referralOnly, setReferralOnly] = useState(true);
  const [excludeUK, setExcludeUK] = useState(true);
  const [table, setTable] = useState<ReportTable | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    assignmentReportOptions()
      .then((o) => { setStudies(o.studies); setStages(o.stages); setRoles(o.roles ?? []); })
      .catch(() => { /* options are best-effort; defaults still work */ });
  }, []);

  const run = async () => {
    setLoading(true); setError(null);
    try {
      const t = await assignmentReport({
        studies: selStudies,
        stages: selStages,
        referral_only: referralOnly,
        roles: selRoles,
        exclude_countries: excludeUK ? ["United Kingdom"] : [],
      });
      setTable(t);
    } catch (e: any) {
      setError(e?.message || "Failed to load report");
    } finally {
      setLoading(false);
    }
  };

  const exportCsv = () => {
    if (!table) return;
    const blob = new Blob(["﻿" + toCsv(table)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = "referral_contacts_with_role.csv"; a.click();
    URL.revokeObjectURL(url);
  };

  const toggle = (arr: string[], v: string, set: (x: string[]) => void) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  return (
    <div className="p-4" data-testid="assignments-view">
      <h2 className="text-lg font-semibold mb-3">Referral / Assignment Contacts (with Role)</h2>

      <div className="flex flex-wrap gap-4 items-start mb-4">
        <fieldset className="border rounded p-2">
          <legend className="text-xs px-1">Studies</legend>
          {(studies.length ? studies : STUDIES_DEFAULT).map((s) => (
            <label key={s} className="block text-sm">
              <input type="checkbox" checked={selStudies.includes(s)}
                     onChange={() => toggle(selStudies, s, setSelStudies)} /> {s}
            </label>
          ))}
        </fieldset>

        <fieldset className="border rounded p-2">
          <legend className="text-xs px-1">Stages</legend>
          {(stages.length ? stages : ["Activated"]).map((s) => (
            <label key={s} className="block text-sm">
              <input type="checkbox" checked={selStages.includes(s)}
                     onChange={() => toggle(selStages, s, setSelStages)} /> {s}
            </label>
          ))}
        </fieldset>

        {roles.length > 0 && (
          <fieldset className="border rounded p-2 max-h-40 overflow-auto">
            <legend className="text-xs px-1">Roles</legend>
            {roles.map((r) => (
              <label key={r} className="block text-sm">
                <input type="checkbox" checked={selRoles.includes(r)}
                       onChange={() => toggle(selRoles, r, setSelRoles)} /> {r}
              </label>
            ))}
          </fieldset>
        )}

        <div className="flex flex-col gap-1 text-sm">
          <label><input type="checkbox" checked={referralOnly}
                        onChange={(e) => setReferralOnly(e.target.checked)} /> Referral contacts only</label>
          <label><input type="checkbox" checked={excludeUK}
                        onChange={(e) => setExcludeUK(e.target.checked)} /> Exclude United Kingdom</label>
        </div>

        <div className="flex flex-col gap-2">
          <button data-testid="assignments-run" onClick={run} disabled={loading}
                  className="px-3 py-1.5 rounded bg-[#003f7d] text-white text-sm disabled:opacity-50">
            {loading ? "Loading…" : "Run report"}
          </button>
          <button data-testid="assignments-export" onClick={exportCsv} disabled={!table}
                  className="px-3 py-1.5 rounded border text-sm disabled:opacity-50">Export CSV</button>
        </div>
      </div>

      {error && <div className="text-red-600 text-sm mb-2">{error}</div>}

      {table && (
        <div className="overflow-auto border rounded">
          <table className="min-w-full text-sm" data-testid="assignments-table">
            <thead className="bg-gray-100">
              <tr>{table.columns.map((c) => <th key={c.key} className="text-left px-2 py-1">{c.label || c.key}</th>)}</tr>
            </thead>
            <tbody>
              {table.rows.map((r, i) => (
                <tr key={i} className="border-t">
                  {table.columns.map((c) => (
                    <td key={c.key} className="px-2 py-1">
                      {typeof r[c.key] === "boolean" ? (r[c.key] ? "✓" : "") : String(r[c.key] ?? "")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          <div className="text-xs text-gray-500 px-2 py-1">{table.rows.length} rows</div>
        </div>
      )}
    </div>
  );
}
