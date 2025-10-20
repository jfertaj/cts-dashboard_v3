// src/pages/UploadLinkView.tsx
import React, { useEffect, useRef, useState } from "react";
import FileDropzone from "../components/FileDropzone";
import {
  listQualifications,
  QualificationRow,
  uploadQualification,
  getQualificationPreviewBySite,
  deleteQualification,
  getExplorerFields,
  unlinkSite,
} from "../lib/api";
import SalesforceLinker from "../components/SalesforceLinker";
import UploadingOverlay from "../components/UploadingOverlay";

/* -------------------------- Confirm dialog -------------------------- */
function ConfirmDialog({
  open,
  title,
  message,
  confirmText = "Confirm",
  cancelText = "Cancel",
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string | React.ReactNode;
  confirmText?: string;
  cancelText?: string;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[11000] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/30" onClick={onCancel} />
      <div className="relative w-[min(520px,95vw)] rounded-xl border bg-white shadow-2xl">
        <div className="px-4 py-3 border-b">
          <h3 className="text-base font-semibold text-gray-900">{title}</h3>
        </div>
        <div className="px-4 py-4 text-sm text-gray-700">{message}</div>
        <div className="px-4 py-3 border-t flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
          >
            {cancelText}
          </button>
          <button
            onClick={onConfirm}
            className="rounded-md bg-red-600 text-white px-3 py-1.5 text-sm hover:bg-red-700"
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}

/* -------------------------- Simple modal (preview) -------------------------- */
function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[10000] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="relative max-h-[85vh] w-[min(1000px,95vw)] overflow-hidden rounded-xl border bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h3 className="text-base font-semibold">{title}</h3>
          <button
            onClick={onClose}
            className="rounded-md border px-2 py-1 text-sm hover:bg-gray-50"
            aria-label="Close preview"
          >
            Close
          </button>
        </div>
        <div className="p-3 overflow-auto">{children}</div>
      </div>
    </div>
  );
}

/* ------------------------------- Types -------------------------------------- */
type PreviewRow = {
  section?: string | null;
  subsection?: string | null;
  question_number?: string | null;
  question_text: string;
  answer?: string | null;
};

type ConfirmState =
  | null
  | {
      title: string;
      message: React.ReactNode;
      confirmText?: string;
      onConfirm: () => Promise<void> | void;
    };

/* -------------------------------- Page -------------------------------------- */
export default function UploadLinkView() {
  const [rows, setRows] = useState<QualificationRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [overlayMsg, setOverlayMsg] = useState<string>("Uploading…");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Preview modal state
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewTitle, setPreviewTitle] = useState("Preview");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  const [previewRows, setPreviewRows] = useState<PreviewRow[]>([]);

  // Confirm dialog state
  const [confirm, setConfirm] = useState<ConfirmState>(null);

  // BroadcastChannel para sincronizar Explorer
  const bcRef = useRef<BroadcastChannel | null>(null);
  useEffect(() => {
    try {
      bcRef.current = new BroadcastChannel("cts-qual-sync");
    } catch {}
    return () => {
      try {
        bcRef.current?.close();
      } catch {}
    };
  }, []);

  const refresh = async () => {
    try {
      const resp = await listQualifications();
      setRows(Array.isArray(resp.rows) ? resp.rows : []);
    } catch (e: any) {
      setErr(e.message || "Could not load list");
      setRows([]);
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  // Espera a que los qual.* aparezcan/desaparezcan en /api/explorer/fields
  async function waitForQualPresence(expectPresent: boolean, timeoutMs = 25000, stepMs = 900) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeoutMs) {
      try {
        const res = await getExplorerFields();
        const hasQual = Array.isArray(res.fields) && res.fields.some((f) => String(f.key).startsWith("qual."));
        if (hasQual === expectPresent) return true;
      } catch {}
      await new Promise((r) => setTimeout(r, stepMs));
    }
    return false;
  }

  const onFiles = async (files: File[]) => {
    setBusy(true);
    setOverlayMsg("Uploading…");
    setMsg(null);
    setErr(null);
    try {
      for (const f of files) await uploadQualification(f);
      setOverlayMsg("Processing Excel (building fields)...");
      await waitForQualPresence(true);
      try {
        bcRef.current?.postMessage({ type: "qual:updated" });
      } catch {}
      setMsg(`✅ Uploaded ${files.length} file(s)`);
      await refresh();
    } catch (e: any) {
      setErr(e.message || "Upload failed");
    } finally {
      setBusy(false);
    }
  };

  const openPreview = async (siteId: number, siteName: string) => {
    setPreviewOpen(true);
    setPreviewTitle(`Preview · ${siteName}`);
    setPreviewLoading(true);
    setPreviewErr(null);
    setPreviewRows([]);
    try {
      const res = await getQualificationPreviewBySite(siteId, 1000);
      setPreviewRows(Array.isArray(res.rows) ? (res.rows as PreviewRow[]) : []);
      if (!res.rows || res.rows.length === 0) setPreviewErr("No parsed data for this site.");
    } catch (e: any) {
      setPreviewErr(e?.message || "Could not load preview");
    } finally {
      setPreviewLoading(false);
    }
  };

  // ----- Real delete (sin confirm UI) -----
  const doDelete = async (questionnaireId: number) => {
    setBusy(true);
    setOverlayMsg("Deleting…");
    setErr(null);
    try {
      await deleteQualification(questionnaireId);
      setRows((prev) => prev.filter((r) => r.id !== questionnaireId));
      setOverlayMsg("Cleaning fields…");
      await waitForQualPresence(false);
      try {
        bcRef.current?.postMessage({ type: "qual:updated" });
      } catch {}
      setMsg("🗑️ Deleted successfully");
    } catch (e: any) {
      setErr(e.message || "Could not delete");
    } finally {
      setBusy(false);
    }
  };

  // ----- Real unlink (sin confirm UI) -----
  const doUnlink = async (siteId: number) => {
    setBusy(true);
    setOverlayMsg("Unlinking…");
    setErr(null);
    try {
      await unlinkSite(siteId);
      await refresh();
      try {
        bcRef.current?.postMessage({ type: "qual:updated" });
      } catch {}
      setMsg("🔗 Unlinked successfully");
    } catch (e: any) {
      setErr(e.message || "Could not unlink");
    } finally {
      setBusy(false);
    }
  };

  // Helpers UI
  const isLinked = (r: QualificationRow) => !!r.salesforce_account_id;

  return (
    <div className="space-y-4">
      <FileDropzone onFiles={onFiles} />
      <UploadingOverlay open={busy} message={overlayMsg} />
      {msg && <div className="text-emerald-700">{msg}</div>}
      {err && <div className="text-red-600">{err}</div>}

      <div className="border rounded overflow-auto">
        <table className="w-full text-sm">
          <thead className="bg-gray-50">
            <tr>
              <th className="p-2 text-left">Filename</th>
              <th className="p-2 text-left">Site</th>
              <th className="p-2 text-left">Version</th>
              <th className="p-2 text-left">Country / City</th>
              <th className="p-2 text-left">Salesforce</th>
              <th className="p-2 text-left">Actions</th>
            </tr>
          </thead>
          <tbody>
            {Array.isArray(rows) && rows.length > 0 ? (
              rows.map((r) => (
                <tr key={r.id} className="border-t">
                  <td className="p-2">{r.file_name}</td>
                  <td className="p-2">{r.site_name}</td>
                  <td className="p-2">{r.version ?? "-"}</td>
                  <td className="p-2">
                    {r.country ?? "-"} / {r.city ?? "-"}
                  </td>
                  <td className="p-2">
                    {isLinked(r) ? (
                      <span className="inline-flex items-center gap-1 text-emerald-700">
                        <span className="w-2 h-2 rounded-full bg-emerald-600 inline-block" />
                        Linked
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-orange-600">
                        <span className="w-2 h-2 rounded-full bg-orange-500 inline-block" />
                        Not linked
                      </span>
                    )}
                  </td>

                  {/* 👇 Todas las acciones en una sola celda (sin wrap) */}
                  <td className="p-2 w-[700px]">
                    <div className="flex items-center flex-nowrap gap-2">
                      <SalesforceLinker
                        siteId={r.site_id}
                        currentAccountName={r.salesforce_account_name ?? null}
                        onLinked={async () => {
                          await refresh();
                          try {
                            bcRef.current?.postMessage({ type: "qual:updated" });
                          } catch {}
                        }}
                        onUnlinked={async () => {
                          await refresh();
                          try {
                            bcRef.current?.postMessage({ type: "qual:updated" });
                          } catch {}
                        }}
                      />

                      {isLinked(r) && (
                        <button
                          className="inline-flex items-center justify-center h-10 px-3 rounded-md border border-red-600 text-red-700 hover:bg-red-50"
                          title="Unlink this site from Salesforce"
                          onClick={() =>
                            setConfirm({
                              title: "Unlink site?",
                              message: (
                                <>
                                  This will remove the Salesforce Account link for{" "}
                                  <strong>{r.site_name}</strong>. You can link it again later.
                                </>
                              ),
                              confirmText: "Unlink",
                              onConfirm: () => {
                                setConfirm(null);
                                return doUnlink(r.site_id);
                              },
                            })
                          }
                        >
                          Unlink
                        </button>
                      )}

                      <button
                        onClick={() => openPreview(r.site_id, r.site_name)}
                        className="inline-flex items-center justify-center h-10 px-3 rounded-md border text-sm hover:bg-gray-50"
                        title="View last parsed questionnaire for this site"
                      >
                        Preview
                      </button>

                      <button
                        className="inline-flex items-center justify-center h-10 px-3 rounded-md bg-red-600 text-white text-sm hover:bg-red-700 disabled:opacity-60"
                        title="Delete this Excel and all its responses"
                        onClick={() =>
                          setConfirm({
                            title: "Delete questionnaire?",
                            message: (
                              <>
                                This will permanently delete the Excel{" "}
                                <strong>{r.file_name || r.id}</strong> and all its parsed
                                questions/answers. This action cannot be undone.
                              </>
                            ),
                            confirmText: "Delete",
                            onConfirm: () => {
                              setConfirm(null);
                              return doDelete(r.id);
                            },
                          })
                        }
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={6} className="p-4 text-center text-gray-500">
                  No qualifications uploaded
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Preview modal */}
      <Modal open={previewOpen} title={previewTitle} onClose={() => setPreviewOpen(false)}>
        {previewLoading ? (
          <div className="p-6 text-center text-gray-700">Loading…</div>
        ) : previewErr ? (
          <div className="p-4 text-red-600">{previewErr}</div>
        ) : (
          <div className="max-h-[70vh] overflow-auto rounded border">
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr>
                  <th className="px-3 py-2 text-left font-semibold text-gray-700">Section</th>
                  <th className="px-3 py-2 text-left font-semibold text-gray-700">Subsection</th>
                  <th className="px-3 py-2 text-left font-semibold text-gray-700">#</th>
                  <th className="px-3 py-2 text-left font-semibold text-gray-700">Question</th>
                  <th className="px-3 py-2 text-left font-semibold text-gray-700">Answer</th>
                </tr>
              </thead>
              <tbody>
                {previewRows.map((row, i) => (
                  <tr key={i} className="border-t">
                    <td className="px-3 py-2 whitespace-nowrap">{row.section ?? ""}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{row.subsection ?? ""}</td>
                    <td className="px-3 py-2 whitespace-nowrap">{row.question_number ?? ""}</td>
                    <td className="px-3 py-2">{row.question_text}</td>
                    <td className="px-3 py-2 text-gray-700">{row.answer ?? ""}</td>
                  </tr>
                ))}
                {previewRows.length === 0 && (
                  <tr>
                    <td className="px-3 py-6 text-center text-gray-500" colSpan={5}>
                      No data
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </Modal>

      {/* Confirm dialog (shared by delete/unlink) */}
      <ConfirmDialog
        open={!!confirm}
        title={confirm?.title || ""}
        message={confirm?.message || ""}
        confirmText={confirm?.confirmText || "Confirm"}
        onConfirm={() => confirm?.onConfirm?.()}
        onCancel={() => setConfirm(null)}
      />

      {/* overlay global */}
      <UploadingOverlay open={busy} message={overlayMsg} />
    </div>
  );
}