import React, { useEffect, useRef, useState } from "react";
import { searchAccounts, linkSite, unlinkSite } from "../lib/api";

type Row = { id: string; name: string; city?: string | null; country?: string | null };

const BTN_H = "h-10";
const BTN_W = "w-20"; // más estrecho (Search más pequeño)

/* ---------------- Toast muy simple ---------------- */
function Toast({
  open,
  message,
  kind = "error",
}: {
  open: boolean;
  message: string;
  kind?: "error" | "success" | "info";
}) {
  if (!open) return null;
  const color =
    kind === "success" ? "bg-emerald-600" : kind === "info" ? "bg-slate-700" : "bg-red-600";
  return (
    <div className="fixed bottom-4 right-4 z-[12000]">
      <div className={`text-white ${color} shadow-lg rounded-md px-3 py-2 text-sm`}>
        {message}
      </div>
    </div>
  );
}

export default function SalesforceLinker({
  siteId,
  currentAccountName,
  onLinked,
  onUnlinked,
}: {
  siteId: number;
  currentAccountName: string | null;
  onLinked?: () => void;
  onUnlinked?: () => void;
}) {
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);

  // toast state
  const [toastMsg, setToastMsg] = useState<string | null>(null);
  const hideTimer = useRef<number | null>(null);
  const showToast = (msg: string) => {
    setToastMsg(msg);
    if (hideTimer.current) window.clearTimeout(hideTimer.current);
    hideTimer.current = window.setTimeout(() => setToastMsg(null), 3500);
  };
  useEffect(() => () => hideTimer.current && window.clearTimeout(hideTimer.current), []);

  const clearSearch = () => {
    setQ("");
    setRows([]);
  };

  const doSearch = async () => {
    const term = q.trim();
    if (!term) {
      clearSearch();
      return;
    }
    setBusy(true);
    try {
      const res = await searchAccounts(term);
      setRows(Array.isArray(res?.rows) ? (res.rows as Row[]) : []);
      if (!Array.isArray(res?.rows) || res.rows.length === 0) {
        showToast("No Salesforce accounts found.");
      }
    } catch (e: any) {
      const status = e?.status || e?.response?.status;
      if (status === 401 || status === 403) {
        showToast("Not connected to Salesforce. Please log in.");
        try {
          window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: false } }));
        } catch {}
      } else {
        showToast(e?.message || "Search failed");
      }
      setRows([]);
    } finally {
      setBusy(false);
    }
  };

  const doLink = async (accountId: string) => {
    setBusy(true);
    try {
      await linkSite(siteId, accountId);
      setRows([]);
      setQ("");
      onLinked?.();
      showToast("Linked successfully.");
    } catch (e: any) {
      showToast(e?.message || "Link failed");
    } finally {
      setBusy(false);
    }
  };

  const doUnlink = async () => {
    const ok = window.confirm("Unlink this site from its Salesforce account?");
    if (!ok) return;
    setBusy(true);
    try {
      await unlinkSite(siteId);
      setRows([]);
      onUnlinked?.();
      showToast("Unlinked successfully.");
    } catch (e: any) {
      showToast(e?.message || "Unlink failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      {/* fila de controles */}
      <div className="flex items-center flex-nowrap gap-2">
        <input
          className={`border rounded-md px-3 text-sm w-72 ${BTN_H}`}
          placeholder="Search SF account..."
          value={q}
          onChange={(e) => {
            const v = e.target.value;
            setQ(v);
            if (v.trim() === "") setRows([]);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") doSearch();
            if (e.key === "Escape") clearSearch();
          }}
          disabled={busy}
          aria-label="Search SF account"
        />

        <button
          onClick={doSearch}
          disabled={busy || !q.trim()}
          className={`inline-flex items-center justify-center gap-2 ${BTN_H} ${BTN_W} rounded-md bg-blue-600 text-white text-sm hover:bg-blue-700 disabled:opacity-60`}
          title="Search Salesforce accounts"
        >
          {busy ? "…" : "Search"}
        </button>

        <button
          onClick={clearSearch}
          disabled={busy || (!q && rows.length === 0)}
          className={`inline-flex items-center justify-center gap-2 ${BTN_H} ${BTN_W} rounded-md border text-sm hover:bg-gray-50 disabled:opacity-60`}
          title="Clear search and hide results"
        >
          Clear
        </button>

        {currentAccountName ? (
          <button
            onClick={doUnlink}
            disabled={busy}
            className={`inline-flex items-center justify-center gap-2 ${BTN_H} ${BTN_W} rounded-md border border-red-600 text-red-700 text-sm hover:bg-red-50 disabled:opacity-60`}
            title="Unlink this site from its current Salesforce account"
          >
            Unlink
          </button>
        ) : null}
      </div>

      {/* estado actual */}
      {currentAccountName && (
        <div className="text-xs text-emerald-700">
          Linked to: <strong>{currentAccountName}</strong>
        </div>
      )}

      {/* resultados */}
      {rows.length > 0 && (
        <div className="rounded-md border overflow-hidden">
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left font-semibold text-gray-700">Account</th>
                <th className="px-3 py-2 text-left font-semibold text-gray-700">City / Country</th>
                <th className="px-3 py-2 text-left font-semibold text-gray-700 w-28">Action</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="border-t">
                  <td className="px-3 py-2">
                    <div className="font-medium text-gray-900">{r.name}</div>
                    <div className="text-xs text-gray-500">{r.id}</div>
                  </td>
                  <td className="px-3 py-2">{(r.city || "-") + " / " + (r.country || "-")}</td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => doLink(r.id)}
                      disabled={busy}
                      className={`inline-flex items-center justify-center gap-2 ${BTN_H} ${BTN_W} rounded-md bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-60`}
                      title="Link this site to the selected account"
                    >
                      Link
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* toast */}
      <Toast open={!!toastMsg} message={toastMsg || ""} />
    </div>
  );
}