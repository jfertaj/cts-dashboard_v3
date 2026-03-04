// src/components/SiteDetailsModal.tsx
import React from "react";

export default function SiteDetailsModal({
  open,
  onClose,
  accountId,
  accountName,
}: {
  open: boolean;
  onClose: () => void;
  accountId: string | null;
  accountName?: string | null;
}) {
  type AccountExtras = {
    account_id: string;
    member?: { account_id: string; name: string } | null;
    pi?: { contact_id?: string; name?: string; email?: string; phone?: string; role__c?: string } | null;
    opportunity?: { id?: string; name?: string; new_dx_u18?: number | null; new_dx_o18?: number | null } | null;
    newDxUnder18?: number | null;
    newDxOver18?: number | null;
    csContribution?: {
      INNODIA_Clinical_Trial_Site__c?: boolean | null;
      Referral_Outreach_Site_Non_CTS__c?: boolean | null;
      Elegible_for_DETECT_Site__c?: boolean | null;
    } | null;
    assignments?: Array<{
      id: string;
      name: string;
      stage?: string | null;
      type?: string | null;
      opportunity_id?: string | null;
      opportunity_name?: string | null;
      created?: string | null;
    }> | null;
  };
  const [loading, setLoading] = React.useState(false);
  const [extras, setExtras] = React.useState<AccountExtras | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [raw, setRaw] = React.useState<any>(null); // debug: payload crudo

  React.useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!open || !accountId) { setExtras(null); setError(null); return; }
      setLoading(true); setError(null);
      try {
        const res = await fetch(`/api/salesforce/explorer/account-extras/${accountId}`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        // parseo defensivo: primero como texto, luego intenta JSON
        const txt = await res.text();
        let json: any = null;
        try {
          json = txt ? JSON.parse(txt) : {};
        } catch (e) {
          json = { _parse_error: String(e), _body: txt };
        }
        if (alive) {
          setRaw(json);
          // normaliza a objeto con las claves esperadas
          const normalized: AccountExtras = {
            account_id: json?.account_id ?? accountId,
            member: json?.member ?? null,
            pi: json?.pi ?? null,
            opportunity: json?.opportunity ?? null,
            newDxUnder18: json?.newDxUnder18 ?? json?.opportunity?.new_dx_u18 ?? null,
            newDxOver18: json?.newDxOver18 ?? json?.opportunity?.new_dx_o18 ?? null,
            csContribution: json?.csContribution ?? { INNODIA_Clinical_Trial_Site__c: null, Referral_Outreach_Site_Non_CTS__c: null, Elegible_for_DETECT_Site__c: null },
            assignments: Array.isArray(json?.assignments) ? json.assignments : [],
          };
          setExtras(normalized);
          console.debug("[SiteDetailsModal] extras payload:", normalized, "(raw:", json, ")");
          if (json?.error) {
            setError(String(json.error));
          }
        }
      } catch (e: any) {
        if (alive) {
          console.warn("[SiteDetailsModal] fetch error:", e);
          setError(e?.message || "Failed to load details");
          setRaw({ _fetch_error: String(e) });
        }
      } finally {
        if (alive) setLoading(false);
      }
    };
    run();
    return () => { alive = false; };
  }, [open, accountId]);

  if (!open || !accountId) return null;

  const line = (label: string, value?: string | number | null) => (
    <div className="flex items-start gap-2 text-sm">
      <span className="w-48 text-gray-600">{label}</span>
      <span className="font-medium text-gray-900 break-all">
        {value !== undefined && value !== null && String(value).trim() !== "" ? String(value) : "—"}
      </span>
    </div>
  );

  const bool = (v?: boolean | null) => (v ? "✓" : "—");

  const memberName = extras?.member?.name ?? "—";
  const piName = extras?.pi?.name ?? "—";
  const piEmail = extras?.pi?.email ?? "—";
  const piPhone = extras?.pi?.phone ?? "—";
  const oppName = extras?.opportunity?.name ?? "—";
  const newU18 = extras?.opportunity?.new_dx_u18 ?? extras?.newDxUnder18 ?? null;
  const newO18 = extras?.opportunity?.new_dx_o18 ?? extras?.newDxOver18 ?? null;
  const cs = extras?.csContribution;

  return (
    <div
      className="fixed inset-0 z-[10050] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="details-modal-title"
    >
      {/* Contenedor del modal con altura máxima de viewport y layout en columna */}
      <div className="w-full max-w-xl max-h-[90vh] flex flex-col rounded-2xl bg-white shadow-2xl ring-1 ring-black/5">
        {/* Header sticky para que el botón Close siempre sea accesible */}
        <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3 border-b bg-white/95 backdrop-blur supports-[backdrop-filter]:bg-white/80">
          <div className="min-w-0">
            <div id="details-modal-title" className="text-sm text-gray-600">Details</div>
            <div className="font-semibold truncate" title={accountName || accountId}>
              {accountName || "—"}
            </div>
          </div>
          <button className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50" onClick={onClose}>
            Close
          </button>
        </div>

        {/* Body scrollable: ocupa el resto del alto */}
        <div className="p-5 space-y-3 overflow-y-auto flex-1 min-h-0">
          {line("Account Id", accountId)}
          <hr className="my-2" />
          {loading && <div className="text-sm text-gray-600">Loading extra info…</div>}
          {error && (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-2 text-amber-900 text-sm">
              ⚠️ Could not load some details — {error}
            </div>
          )}

          {/* Member & PI */}
          {line("Member", memberName)}
          <hr className="my-2" />
          {line("PI Name", piName)}
          {line("PI Email", piEmail)}
          {line("PI Phone", piPhone)}

          {/* Opportunity */}
          <hr className="my-2" />
          <div className="text-sm font-semibold text-gray-800">Opportunity</div>
          {line("Opportunity Name", oppName)}
          {line("New T1D diagnosed U<18 (last year)", newU18)}
          {line("New T1D diagnosed O≥18 (last year)", newO18)}

          {/* CS Contribution to INNODIA */}
          <hr className="my-2" />
          <div className="text-sm font-semibold text-gray-800">CS Contribution to INNODIA</div>
          {line("INNODIA Clinical Trial Site", bool(cs?.INNODIA_Clinical_Trial_Site__c))}
          {line("Referral & Outreach Site (Non-CTS)", bool(cs?.Referral_Outreach_Site_Non_CTS__c))}
          {line("Eligible for DETECT Site", bool(cs?.Elegible_for_DETECT_Site__c))}
          {/* Assignments */}
          <hr className="my-2" />
          <div className="text-sm font-semibold text-gray-800">Assignments</div>
          {Array.isArray(extras?.assignments) && extras.assignments.length > 0 ? (
            <ul className="space-y-2">
              {extras.assignments.map((a) => (
                <li key={a.id} className="text-sm">
                  <div className="font-medium">
                    {a.name}
                    {a.stage ? <span className="text-gray-600"> · {a.stage}</span> : null}
                  </div>
                  <div className="text-gray-700">{a.opportunity_name ?? "—"}</div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="text-sm text-gray-600">—</div>
          )}
          {/* DEBUG: muestra el JSON crudo cuando haya algo raro */}
          {raw ? (
            <details className="mt-3">
              <summary className="text-xs text-gray-500 cursor-pointer">Debug payload</summary>
              <pre className="mt-2 text-[11px] text-gray-700 bg-gray-50 rounded-md p-2 overflow-auto max-h-64">
                {JSON.stringify(raw, null, 2)}
              </pre>
            </details>
          ) : null}
        </div>
      </div>
    </div>
  );
}
