// src/components/MemberDetailsModal.tsx
import React from "react";

type Contact = {
  id: string;
  name: string;
  email?: string;
  phone?: string;
  mobile?: string;
  title?: string;
  department?: string;
  is_board_member?: boolean;
  is_country_lead?: boolean;
  voting_rights?: boolean;
};

type SubAccount = {
  id: string;
  name: string;
  country?: string;
  city?: string;
  is_cts?: boolean;
  is_cs?: boolean;
  is_dxlab?: boolean;
  is_rp?: boolean;
  is_accredited?: boolean;
  status?: string;
  contacts: Contact[];
};

type MemberDetail = {
  account_id: string;
  account_name: string;
  country?: string;
  city?: string;
  level?: string;
  status?: string;
  phone?: string;
  website?: string;
  proposed_roles: {
    cs: boolean;
    dxlab: boolean;
    lab: boolean;
    patient_org: boolean;
  };
  validated_roles: {
    cs: boolean;
    cts: boolean;
    dxlab: boolean;
    lab: boolean;
    patient_org: boolean;
  };
  member_contacts: Contact[];
  subaccounts: SubAccount[];
};

export default function MemberDetailsModal({
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
  const [loading, setLoading] = React.useState(false);
  const [detail, setDetail] = React.useState<MemberDetail | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let alive = true;
    const run = async () => {
      if (!open || !accountId) {
        setDetail(null);
        setError(null);
        return;
      }
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/members/${accountId}/detail`, { credentials: "include" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        if (alive) setDetail(json);
      } catch (e: any) {
        if (alive) setError(e?.message || "Failed to load details");
      } finally {
        if (alive) setLoading(false);
      }
    };
    run();
    return () => { alive = false; };
  }, [open, accountId]);

  if (!open || !accountId) return null;

  const RolePill = ({ label, active }: { label: string; active: boolean }) => (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
        active
          ? "bg-green-100 text-green-800 border border-green-200"
          : "bg-gray-100 text-gray-400 border border-gray-200"
      }`}
    >
      {active ? "✓ " : "— "}
      {label}
    </span>
  );

  const ContactCard = ({ c, showFlags }: { c: Contact; showFlags?: boolean }) => (
    <div className="rounded-lg border border-gray-100 bg-gray-50 p-3 text-sm space-y-0.5">
      <div className="font-medium text-gray-900">{c.name}</div>
      {c.title && <div className="text-gray-600">{c.title}{c.department ? ` · ${c.department}` : ""}</div>}
      {c.email && (
        <div className="text-gray-700">
          <a href={`mailto:${c.email}`} className="text-blue-600 hover:underline">{c.email}</a>
        </div>
      )}
      {(c.phone || c.mobile) && (
        <div className="text-gray-700">{c.phone || c.mobile}</div>
      )}
      {showFlags && (
        <div className="flex flex-wrap gap-1 pt-1">
          {c.is_board_member && (
            <span className="text-xs bg-blue-50 text-blue-700 border border-blue-200 rounded-full px-2 py-0.5">Board Member</span>
          )}
          {c.is_country_lead && (
            <span className="text-xs bg-purple-50 text-purple-700 border border-purple-200 rounded-full px-2 py-0.5">Country Lead</span>
          )}
          {c.voting_rights && (
            <span className="text-xs bg-amber-50 text-amber-700 border border-amber-200 rounded-full px-2 py-0.5">Voting Rights</span>
          )}
        </div>
      )}
    </div>
  );

  return (
    <div
      data-testid="member-details-modal"
      className="fixed inset-0 z-[10050] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="member-modal-title"
    >
      <div className="w-full max-w-2xl max-h-[90vh] flex flex-col rounded-2xl bg-white shadow-2xl ring-1 ring-black/5">
        {/* Sticky header */}
        <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-3 border-b bg-white/95 backdrop-blur supports-[backdrop-filter]:bg-white/80">
          <div className="min-w-0">
            <div className="text-xs text-gray-500 font-medium uppercase tracking-wide">Member Institution</div>
            <div id="member-modal-title" className="font-semibold text-gray-900 truncate" title={accountName || accountId}>
              {accountName || "—"}
            </div>
            {detail && (
              <div className="text-xs text-gray-500 mt-0.5">
                {detail.country} {detail.city ? `· ${detail.city}` : ""}
                {detail.level ? ` · ${detail.level}` : ""}
              </div>
            )}
          </div>
          <button
            data-testid="member-details-close"
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50 flex-shrink-0 ml-4"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        {/* Scrollable body */}
        <div className="p-5 space-y-5 overflow-y-auto flex-1 min-h-0">
          {loading && (
            <div className="text-sm text-gray-500 text-center py-8">Loading details…</div>
          )}
          {error && (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900 text-sm">
              ⚠️ {error}
            </div>
          )}

          {detail && !loading && (
            <>
              {/* Contact info */}
              {(detail.phone || detail.website) && (
                <div className="flex flex-wrap gap-4 text-sm">
                  {detail.phone && (
                    <span className="text-gray-700">📞 {detail.phone}</span>
                  )}
                  {detail.website && (
                    <a href={detail.website} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
                      🌐 {detail.website.replace(/^https?:\/\//, "")}
                    </a>
                  )}
                </div>
              )}

              {/* Proposed Roles */}
              <div>
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                  Proposed Roles in the Network
                </div>
                <div className="flex flex-wrap gap-2">
                  <RolePill label="Clinical Site (CS)" active={detail.proposed_roles.cs} />
                  <RolePill label="Diagnostic Lab (DxLab)" active={detail.proposed_roles.dxlab} />
                  <RolePill label="Research & Mech. Lab (LAB)" active={detail.proposed_roles.lab} />
                  <RolePill label="Patient Organization" active={detail.proposed_roles.patient_org} />
                </div>
              </div>

              {/* Validated Roles */}
              <div>
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                  Validated Roles in the Network
                </div>
                <div className="flex flex-wrap gap-2">
                  <RolePill label="Validated CS" active={detail.validated_roles.cs} />
                  <RolePill label="Validated CTS" active={detail.validated_roles.cts} />
                  <RolePill label="Validated DxLab" active={detail.validated_roles.dxlab} />
                  <RolePill label="Validated LAB" active={detail.validated_roles.lab} />
                  <RolePill label="Validated Patient Org" active={detail.validated_roles.patient_org} />
                </div>
              </div>

              {/* Member Contacts */}
              <div>
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                  Member Contacts ({detail.member_contacts.length})
                </div>
                {detail.member_contacts.length > 0 ? (
                  <div className="space-y-2">
                    {detail.member_contacts.map((c) => (
                      <ContactCard key={c.id} c={c} showFlags />
                    ))}
                  </div>
                ) : (
                  <div className="text-sm text-gray-400">No contacts found</div>
                )}
              </div>

              {/* Clinical Sites (SubAccounts) */}
              <div>
                <div className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                  Clinical Sites ({detail.subaccounts.length})
                </div>
                {detail.subaccounts.length > 0 ? (
                  <div className="space-y-3">
                    {detail.subaccounts.map((sa) => (
                      <div key={sa.id} className="rounded-lg border border-gray-200 overflow-hidden">
                        {/* Site header */}
                        <div className="bg-gray-50 px-3 py-2 flex items-start justify-between gap-2">
                          <div>
                            <div className="font-medium text-sm text-gray-900">{sa.name}</div>
                            <div className="text-xs text-gray-500">
                              {sa.country}{sa.city ? ` · ${sa.city}` : ""}
                              {sa.status ? ` · ${sa.status}` : ""}
                            </div>
                          </div>
                          <div className="flex flex-wrap gap-1 justify-end">
                            {sa.is_cts && <span className="text-xs bg-blue-100 text-blue-700 border border-blue-200 rounded px-1.5 py-0.5">CTS</span>}
                            {sa.is_cs && <span className="text-xs bg-green-100 text-green-700 border border-green-200 rounded px-1.5 py-0.5">CS</span>}
                            {sa.is_dxlab && <span className="text-xs bg-purple-100 text-purple-700 border border-purple-200 rounded px-1.5 py-0.5">DxLab</span>}
                            {sa.is_rp && <span className="text-xs bg-amber-100 text-amber-700 border border-amber-200 rounded px-1.5 py-0.5">RP</span>}
                            {sa.is_accredited && <span className="text-xs bg-teal-100 text-teal-700 border border-teal-200 rounded px-1.5 py-0.5">Accredited</span>}
                          </div>
                        </div>
                        {/* Site contacts */}
                        {sa.contacts.length > 0 && (
                          <div className="px-3 py-2 space-y-1.5">
                            {sa.contacts.map((c) => (
                              <ContactCard key={c.id} c={c} />
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-sm text-gray-400">No clinical sites found</div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
