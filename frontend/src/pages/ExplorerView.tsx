// src/pages/ExplorerView.tsx
import React, { useEffect, useMemo, useRef, useState, useCallback } from "react";
import {
  ExplorerPoint,
  ExplorerRow,
  explorerSearch,
  explorerBootstrap,
  salesforceMe,
  FieldDef,
  getExplorerFields,
  FilterGroup,
  explorerFillColumns,
} from "../lib/api";
import FilterBuilder from "../components/FilterBuilder";
import { filterGroupToFlat, removeRuleFromExpr, defaultExpr } from "../lib/filterLogic";
import MapView from "../components/MapView";
import ColumnPicker from "../components/ColumnPicker";
import ChartModal from "../components/charts/ChartModal";
import CustomView, { DEFAULT_LEGEND_MAX, type ChartType } from "../components/charts/CustomView";
import { displayCountry } from "../lib/countryUtils";
import SiteDetailsModal from "../components/SiteDetailsModal";

import {
  ColumnDef,
  SortingState,
  ColumnFiltersState,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  getFilteredRowModel,
  useReactTable,
  flexRender,
} from "@tanstack/react-table";

import useSWR, { useSWRConfig } from "swr";
import { EXPLORER_BOOT_KEY } from "../lib/cacheKeys";
import { listenExplorerChange } from "../lib/events";
import { sfLoginRedirect } from "../lib/salesforce";
import { askAI, ChatResponse } from "../lib/ai";
import { readDataCell } from "../lib/rowAccess";
import Moby from "../assets/Moby.png";

const OP_LABELS: Record<string, string> = {
  equals: "=", not_equals: "≠", contains: "contains", not_contains: "!contains",
  starts_with: "starts with", ends_with: "ends with",
  ">": ">", ">=": "≥", "<": "<", "<=": "≤",
  between: "between", in: "in", not_in: "not in",
  is_empty: "is empty", is_not_empty: "is not empty",
};

/** ================================================
 * Human‑friendly label dictionary (explicit overrides)
 * Keys can be with or without the "sf." prefix; matching is
 * case-insensitive and also supports a normalized form
 * (lowercase, no "__c", no dots/underscores/spaces).
 * ================================================ */
const FRIENDLY_LABEL_OVERRIDES = new Map<string, string>([
  // ---- New dx T1D (last year) ----
  ["sf.C_Number_of_new_T1D_diagnosed_U_18__c", "New diagnosed T1D <18"],
  ["C_Number_of_new_T1D_diagnosed_U_18__c", "New diagnosed T1D <18"],
  ["sf.C_Number_of_new_T1D_diagnosed_O_18__c", "New diagnosed T1D ≥18"],
  ["C_Number_of_new_T1D_diagnosed_O_18__c", "New diagnosed T1D ≥18"],

  // ---- Current T1D population ----
  ["sf.C_Number_of_T1D_Patients_currently_U_18__c", "T1D patients <18 (current)"],
  ["C_Number_of_T1D_Patients_currently_U_18__c", "T1D patients <18 (current)"],
  ["sf.C_Number_of_T1D_Patients_currently_O_18__c", "T1D patients ≥18 (current)"],
  ["C_Number_of_T1D_Patients_currently_O_18__c", "T1D patients ≥18 (current)"],

  // ---- Screening / stages ----
  ["sf.C_Number_of_Individuals_screened_intotal__c", "Individuals screened (total)"],
  ["C_Number_of_Individuals_screened_intotal__c", "Individuals screened (total)"],
  ["sf.C_Number_of_Stage1_Individuals_followed__c", "Stage 1 individuals followed"],
  ["C_Number_of_Stage1_Individuals_followed__c", "Stage 1 individuals followed"],
  ["sf.C_Number_of_Stage2_Individuals_followed__c", "Stage 2 individuals followed"],
  ["C_Number_of_Stage2_Individuals_followed__c", "Stage 2 individuals followed"],

  // ---- Common SF basics (examples) ----
  ["sf.Account.Name", "Account Name"],
  ["Account.Name", "Account Name"],
  ["sf.Account.ShippingCountry", "Country"],
  ["Account.ShippingCountry", "Country"],
  ["sf.Account.ShippingCity", "City"],
  ["Account.ShippingCity", "City"],
]);

// Contiene el nombre de la cuenta + los botones Nearby / Details, así que por
// defecto no se trunca: la tabla scrollea en horizontal en su lugar. Si el
// usuario le fija un ancho arrastrando la cabecera, ese ancho manda y recorta.
const ACCOUNT_NAME_COL_ID = "sf.Account.Name";

// Build a normalized lookup for resilient matching
function __normKeyForDict(k?: string): string {
  if (!k) return "";
  return String(k)
    .trim()
    .replace(/^sf\./i, "")
    .replace(/__c$/i, "")
    .replace(/[._\s]/g, "")
    .toLowerCase();
}
const __FRIENDLY_LABEL_OVERRIDES_NORM = (() => {
  const m = new Map<string, string>();
  for (const [k, v] of FRIENDLY_LABEL_OVERRIDES.entries()) {
    m.set(__normKeyForDict(k), v);
  }
  return m;
})();

// ---- Label prettifier (SF -> human) ----
function prettyLabelFromKeyLabel(key: string, incoming?: string | null): string {
  // 0) Dictionary overrides (highest priority)
  // Try exact key (with/without sf.), then normalized key
  const exact =
    FRIENDLY_LABEL_OVERRIDES.get(key) ||
    FRIENDLY_LABEL_OVERRIDES.get(key.replace(/^sf\./i, ""));
  if (exact) return exact;

  const normHit = __FRIENDLY_LABEL_OVERRIDES_NORM.get(__normKeyForDict(key));
  if (normHit) return normHit;

  // 1) Prefer the incoming label if present; otherwise derive from the key
  const raw0 = (incoming && String(incoming).trim().length ? String(incoming) : String(key)) || "";

  // 2) Strip SF adornments
  let s = raw0
    .replace(/^sf\./i, "")
    .replace(/__c$/i, "")
    .replace(/\./g, " ")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim();

  // 3) Remove leading technical prefixes like "C " or "CS "
  s = s.replace(/^(C|CS|Ct|Fld)\s+/i, "");

  // 4) Title-case with preserved acronyms
  const ACRONYMS = new Set(["T1D", "GCP", "PI", "CTS", "PAC", "CS"]);
  const words = s.split(" ").filter(Boolean);
  s = words
    .map((w) => {
      const up = w.toUpperCase();
      if (ACRONYMS.has(up)) return up;
      const small = ["of", "and", "or", "for", "to", "in", "on", "with", "by"];
      if (small.includes(w.toLowerCase())) return w.toLowerCase();
      return w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();
    })
    .join(" ");

  // 5) Domain tweaks
  s = s
    // U 18 / O 18 → <18 / ≥18 (allow variants with/without spaces/underscores)
    .replace(/\bU\s*[_]?\s*18\b/gi, "<18")
    .replace(/\bO\s*[_]?\s*18\b/gi, "≥18")
    // Remove boilerplate "Number of"
    .replace(/\bNumber\s+of\b/gi, "")
    // Normalize phrasing "T1D Diagnosed" → "Diagnosed T1D"
    .replace(/\bT1D\s+Diagnosed\b/gi, "Diagnosed T1D")
    // Collapse spaces
    .replace(/\s{2,}/g, " ")
    .trim();

  // 6) Specific phrasing polish
  s = s
    .replace(/^Number\s+/i, "")
    .replace(/\bDiagnosed\b/gi, "diagnosed")
    .replace(/\bNew\s+(?:T1D\s+)?diagnosed\b/gi, "New diagnosed")
    .replace(/\bdiagnosed\s+T1D\b/gi, "diagnosed T1D");

  // 7) Final touch: ensure "New T1D" segment (if present) is leading
  if (/New\s+T1D/i.test(s) && !/^New\s+T1D/i.test(s)) {
    s = s.replace(/.*?(New\s+T1D.*)$/i, "$1");
  }

  return s;
}


// import { explorerFillColumns } from "../lib/api";

// ========== Colores ==========
const COLOR_PROFILING = "#2563eb";
const COLOR_QUALIF = "#059669";
const COLOR_BOTH = "#7c3aed";
const COLOR_NEUTRAL = "#9ca3af";

// ========== Columnas visibles por defecto ==========
const DEFAULT_VISIBLE_COLUMNS: string[] = [
  "sf.Account.Name",
  "sf.Account.ShippingCountry",
  "sf.Account.ShippingCity",
];

// ========== PRESETS base (SF) ==========
const PRESETS = {
  Basic: [
    "sf.Account.Name",
    "sf.Account.ShippingCountry",
    "sf.Account.ShippingCity",
    "sf.Name",
    "sf.StageName",
    "sf.CloseDate",
  ],
  OpportunityCore: [
    "sf.Name",
    "sf.StageName",
    "sf.Amount",
    "sf.CloseDate",
    "sf.CreatedDate",
    "sf.LastModifiedDate",
    "sf.LastActivityDate",
    "sf.RecordTypeId",
  ],
  AccountBasics: [
    "sf.Account.Id",
    "sf.Account.Name",
    "sf.Account.ShippingCountry",
    "sf.Account.ShippingCity",
    // Removed lat/lng from default presets to avoid clutter
  ],
  QualificationDates: [
    "sf.C_Profiling_Complete__c",
    "sf.Qualification_Close_Date__c",
  ],
  PatientPopulation: [
    "sf.C_Number_of_T1D_Patients_currently_U_18__c",
    "sf.C_Number_of_T1D_Patients_currently_O_18__c",
    "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
    "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
    "sf.C_Number_of_Individuals_screened_intotal__c",
    "sf.C_Number_of_Stage1_Individuals_followed__c",
    "sf.C_Number_of_Stage2_Individuals_followed__c",
  ],
  StaffTraining: [
    "sf.C_All_Research_Staff_Needed_for_GCP__c",
    "sf.C_Lead_Study_Nurse_Dedicated_to_the_cent__c",
    "sf.C_Lead_Study_Nurse__c",
    "sf.C_Lead_Study_Coordinator_SC__c",
    "sf.C_Study_Nurse_Specialities__c",
    "sf.C_Study_Nurse_Situation_Explanation__c",
    "sf.C_SC_Situation_Explanation__c",
  ],
  ScreeningPrograms: [
    "sf.C_Aware_of_any_Screening_Program__c",
    "sf.C_Center_for_Running_Early_Diagnosis__c",
    "sf.C_The_Funding_for_the_screening_program__c",
    "sf.C_Under_Which_Program__c",
    "sf.C_Send_patients_to_other_CTS_nearby__c",
    "sf.C_List_of_nearby_CTS__c",
  ],
  ClinicalCapacity: [
    "sf.C_Has_facility_to_conduct__c",
    "sf.C_Centralized_Clinical_Trial_Facility__c",
    "sf.C_Centralized_Facility_Contact_Person__c",
    "sf.C_Services_Provided_by_Centralized_Unit__c",
    "sf.C_Primarily_Caring_for_all_study__c",
    "sf.C_Site_Linked_with_Patient_Org_or_PAC__c",
    "sf.C_List_of_Organization_or_PAC_names__c",
  ],
  TrialPhases: [
    "sf.C_Phase_I_Type1__c",
    "sf.C_Phase_I_NonType1__c",
    "sf.C_Phase_II_Type1__c",
    "sf.C_Phase_II_NonType1__c",
    "sf.C_Phase_III_Type1__c",
    "sf.C_Phase_III_NonType1__c",
    "sf.C_Immuno_Medi_trial_names_or_sponsors__c",
    "sf.C_List_of_trial_name_or_sponsors_Type1__c",
    "sf.C_List_of_trial_name_or_sponsors_NonTyp1__c",
  ],
  NotesAndVerification: [
    "sf.C_Comments__c",
    "sf.C_Additional_Comments__c",
    "sf.C_Certification_Output__c",
    "sf.C_Contact_Verified__c",
  ],
};

// ========== Mapa preset -> lista de keys SF ==========
const SF_GROUPS: Record<string, string[]> = {
  // OnBoarding
  "OnBoarding": [
    "sf.Name",
    "sf.StageName",
    "sf.C_Profiling_Complete__c",
    "sf.Qualification_Close_Date__c",
    "sf.Account.Id",
    "sf.Account.Name",
  ],
  // Profiling Basics
  "Profiling Basics": [
    "sf.C_Number_of_T1D_Patients_currently_U_18__c",
    "sf.C_Number_of_T1D_Patients_currently_O_18__c",
    "sf.C_Number_of_new_T1D_diagnosed_U_18__c",
    "sf.C_Number_of_new_T1D_diagnosed_O_18__c",
    "sf.C_PI_Experience_with_Immuno_Med__c",
    "sf.C_Site_Has_A_Study_Nurse__c",
    "sf.C_Interestedinconducting_clinical_trials__c",
    "sf.C_SC_Dedicate_To_The_Research_Center__c",
    "sf.C_Nbr_of_related_site_sub_Investigator__c",
    "sf.C_Nbr_of_studies_PI_is_involved_PI_Sub_I__c",
    "sf.C_Comments__c",
    "sf.C_All_Research_Staff_Needed_for_GCP__c",
    "sf.C_Aware_of_any_Screening_Program__c",
    "sf.C_Center_for_Running_Early_Diagnosis__c",
    "sf.C_Centralized_Clinical_Trial_Facility__c",
    "sf.C_Lead_Study_Nurse_Dedicated_to_the_cent__c",
    "sf.C_Primarily_Caring_for_all_study__c",
    "sf.C_Site_Linked_with_Patient_Org_or_PAC__c",
    "sf.C_Centralized_Facility_Contact_Person__c",
    "sf.C_Immuno_Medi_trial_names_or_sponsors__c",
    "sf.C_Lead_Study_Coordinator_SC__c",
    "sf.C_Lead_Study_Nurse__c",
    "sf.C_List_of_Organization_or_PAC_names__c",
    "sf.C_List_of_trial_name_or_sponsors_NonTyp1__c",
    "sf.C_List_of_trial_name_or_sponsors_Type1__c",
    "sf.C_Phase_III_NonType1__c",
    "sf.C_Phase_III_Type1__c",
    "sf.C_Phase_II_NonType1__c",
    "sf.C_Phase_II_Type1__c",
    "sf.C_Phase_I_NonType1__c",
    "sf.C_Phase_I_Type1__c",
    "sf.C_Principal_Investigator__c",
    "sf.C_SC_Experience_in_T1D_Clinicla_Research__c",
    "sf.C_SC_Situation_Explanation__c",
    "sf.C_Services_Provided_by_Centralized_Unit__c",
    "sf.C_Study_Nurse_Situation_Explanation__c",
    "sf.C_Study_Nurse_Specialities__c",
    "sf.C_Send_patients_to_other_CTS_nearby__c",
    "sf.C_List_of_nearby_CTS__c",
  ],
  // Screening
  "Screening": [
    "sf.C_Number_of_Individuals_screened_intotal__c",
    "sf.C_Number_of_Stage1_Individuals_followed__c",
    "sf.C_Number_of_Stage2_Individuals_followed__c",
    "sf.C_Is_HLA_typing_performed__c",
    "sf.C_Population_Origin__c",
    "sf.C_Additional_Comments__c",
    "sf.C_Interest_about_setting_up_a_program__c",
    "sf.C_Contact_Provided__c",
    "sf.C_The_Funding_for_the_screening_program__c",
    "sf.C_Under_Which_Program__c",
  ],
  // Account basics always available
  "Account Basics": [
    "sf.Account.Id",
    "sf.Account.Name",
    "sf.Account.ShippingCountry",
    "sf.Account.ShippingCity",
  ],
};

type LocalFieldDef = FieldDef & { group?: string };

const STORAGE_NS = "explorer";
const LS_KEYS = {
  visibleColumns: `${STORAGE_NS}:visibleColumns`,
  sorting: `${STORAGE_NS}:sorting`,
  columnFilters: `${STORAGE_NS}:columnFilters`,
  globalFilter: `${STORAGE_NS}:globalFilter`,
  pageSize: `${STORAGE_NS}:pageSize`,
  columnOrder: `${STORAGE_NS}:columnOrder`,
  columnWidths: `${STORAGE_NS}:columnWidths`,
  nearbyLayout: `${STORAGE_NS}:nearbyLayout`,
  nearbySideWidth: `${STORAGE_NS}:nearbySideWidth`,
};

// Límites al arrastrar el borde de una columna.
const MIN_COLUMN_WIDTH = 80;
const MAX_COLUMN_WIDTH = 1200;

// localStorage es editable por el usuario y puede venir de una versión anterior:
// nunca dejamos que un valor basura acabe en un style width.
function sanitizeColumnWidths(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  const out: Record<string, number> = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value !== "number" || !Number.isFinite(value)) continue;
    out[key] = Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, Math.round(value)));
  }
  return out;
}

// Debug flag (puedes apagarlo luego)
const DEBUG = true; // o lee de localStorage si prefieres
const dbg = (...args: any[]) => { if (DEBUG) console.log(...args); };
const dbgwarn = (...args: any[]) => { if (DEBUG) console.warn(...args); };
// helper para detectar timeouts/abort
const isTimeoutErr = (e: any) => {
  const name = String(e?.name || "");
  const msg  = String(e?.message || "");
  return name === "AbortError" || /timeout/i.test(msg);
};


function safeParse<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try { return JSON.parse(raw) as T; } catch { return fallback; }
}

// --- Columnas que NUNCA deben pedirse a /columns/fill (vienen de otro sitio) ---
// En concreto, Account Name lo tienes en row.account_name / points.account_name
const UNFILLABLE_COLUMNS = new Set<string>([
  "sf.Account.Name",
]);

// --- Columnas que NO queremos en "visibleColumns" porque ya están como fijas ---
// Evita duplicados con las columnas fijas "country"/"city" de la tabla
const EXCLUDED_VISIBLE_COLUMNS = new Set<string>([
  "sf.Account.ShippingCountry",
  "sf.Account.ShippingCity",
  "sf.Account.ShippingLatitude",
  "sf.Account.ShippingLongitude",
]);

// --- Campos que NO mostramos en el FilterBuilder ---
// - ShippingCountry/City → redundantes con site.country / site.city (que sí funcionan)
// - Lat/Lng → no tienen sentido como filtro de texto
// - ID-type lookup fields → show raw Salesforce IDs, not useful for filtering
const EXCLUDED_FILTER_FIELDS = new Set<string>([
  "sf.Account.ShippingCountry",
  "sf.Account.ShippingCity",
  "sf.Account.ShippingLatitude",
  "sf.Account.ShippingLongitude",
  // Raw Salesforce ID fields — impossible to filter without knowing internal IDs
  "sf.Id",
  "sf.Account.Id",
  "sf.RecordTypeId",
  "sf.C_Centralized_Facility_Contact_Person__c",
  "sf.C_Lead_Study_Coordinator_SC__c",
  "sf.C_Lead_Study_Nurse__c",
  "sf.C_Principal_Investigator__c",
  "sf.C_Assignment__c",
  "sf.Assignment.C_Account__c",
  "sf.Assignment.C_Contact_Name__c",
  "sf.Assignment.C_Opportunity_Name__c",
]);

function useDebouncedEffect(fn: () => void, deps: any[], delay = 250) {
  const t = useRef<number | null>(null);
  useEffect(() => {
    if (t.current) window.clearTimeout(t.current);
    t.current = window.setTimeout(() => fn(), delay);
    return () => { if (t.current) window.clearTimeout(t.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

function LoadingOverlay({ text }: { text: string }) {
  return (
    <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-white/70 backdrop-blur-sm">
      <div className="flex items-center gap-3 rounded-xl border bg-white shadow-lg px-4 py-3">
        <svg className="animate-spin h-5 w-5" viewBox="0 0 24 24" fill="none" aria-hidden>
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"/>
        </svg>
        <span className="text-sm font-medium">{text}</span>
      </div>
    </div>
  );
}

function getAccountNameByIdSafe(
  id: string,
  rowsList: ExplorerRow[],
  pointsList: ExplorerPoint[]
): string | "" {
  const r = rowsList.find(r => r.account_id === id);
  if (r?.account_name) return r.account_name;
  const p = pointsList.find(p => p.account_id === id);
  return p?.account_name || "";
}

/* ================= Modal Nearby (reworked) ================= */
type NearbyLayout = "side" | "bottom";

function NearbyModal({
  open,
  baseAccountId,
  baseAccountName,
  defaultKm = 120,
  onClose,
  onConfirm,
}: {
  open: boolean;
  baseAccountId: string;
  baseAccountName?: string | null;
  defaultKm?: number;
  onClose: () => void;
  onConfirm: (opts: { baseAccountId: string; maxKm: number }) => void;
}) {
  const [km, setKm] = useState<number>(defaultKm);
  useEffect(() => { if (open) setKm(defaultKm); }, [open, defaultKm]);
  if (!open) return null;

  const apply = () =>
    onConfirm({ baseAccountId, maxKm: Math.max(1, Number.isFinite(km) ? km : defaultKm) });

  return (
    <div className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl bg-white shadow-2xl ring-1 ring-black/5">
        <div className="px-6 pt-5 pb-2 border-b">
          <div className="flex items-center gap-2 text-gray-800">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Zm0 9.5A2.5 2.5 0 1 1 12 6a2.5 2.5 0 0 1 0 5.5Z" stroke="currentColor" strokeWidth="1.5"/>
            </svg>
            <h3 className="text-lg font-semibold tracking-tight">Find nearby sites</h3>
          </div>
          <div className="mt-1 text-xs text-gray-600">
            Base:{" "}
            <span className="font-semibold text-gray-900" title={baseAccountName || baseAccountId}>
              {baseAccountName || baseAccountId}
            </span>{" "}
            <code className="text-gray-500 ml-1">({baseAccountId})</code>
          </div>
        </div>

        <div className="px-6 py-5 space-y-4">
          <label className="block text-sm font-medium text-gray-900">
            Max driving distance (km)
          </label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1}
              max={1000}
              step={1}
              value={km}
              onChange={(e) => setKm(Number(e.target.value))}
              className="flex-1 accent-[#0072CE]"
            />
            <input
              type="number"
              min={1}
              step={1}
              value={km}
              onChange={(e) => setKm(Math.max(1, Number(e.target.value || 1)))}
              onKeyDown={(e) => { if (e.key === "Enter") apply(); }}
              className="w-24 rounded-lg border px-3 py-2 text-sm focus:ring-2 focus:ring-[#0072CE]"
            />
          </div>
          <p className="text-xs text-gray-500">
            Uses Google Distance Matrix (driving distance).
          </p>
        </div>

        <div className="sticky bottom-0 flex items-center justify-end gap-2 px-6 py-3 bg-gray-50 rounded-b-2xl border-t">
          <button
            onClick={onClose}
            className="rounded-lg px-4 py-2 text-sm text-gray-700 hover:bg-white border"
          >
            Cancel
          </button>
          <button
            onClick={apply}
            className="rounded-lg px-4 py-2 text-sm font-medium text-white bg-[#0072CE] hover:opacity-90"
          >
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}

/* ================= Modal Nearby Multi ================= */
function NearbyMultiModal({
  open,
  count,
  defaultKm = 120,
  onClose,
  onConfirm,
}: {
  open: boolean;
  count: number;
  defaultKm?: number;
  onClose: () => void;
  onConfirm: (opts: { maxKm: number }) => void;
}) {
  const [km, setKm] = useState<number>(defaultKm);
  useEffect(() => { if (open) setKm(defaultKm); }, [open, defaultKm]);
  if (!open) return null;

  const apply = () => onConfirm({ maxKm: Math.max(1, Number.isFinite(km) ? km : defaultKm) });

  return (
    <div className="fixed inset-0 z-[10000] flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl bg-white shadow-2xl ring-1 ring-black/5">
        <div className="px-6 pt-5 pb-2 border-b">
          <div className="flex items-center gap-2 text-gray-800">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Zm0 9.5A2.5 2.5 0 1 1 12 6a2.5 2.5 0 0 1 0 5.5Z" stroke="currentColor" strokeWidth="1.5"/>
            </svg>
            <h3 className="text-lg font-semibold tracking-tight">Find nearby sites</h3>
          </div>
          <div className="mt-1 text-xs text-gray-600">
            Base: <span className="font-semibold text-gray-900">{count} selected site{count !== 1 ? "s" : ""}</span>
          </div>
        </div>
        <div className="px-6 py-5 space-y-4">
          <label className="block text-sm font-medium text-gray-900">
            Max driving distance (km)
          </label>
          <div className="flex items-center gap-3">
            <input
              type="range"
              min={1} max={1000} step={1}
              value={km}
              onChange={(e) => setKm(Number(e.target.value))}
              className="flex-1 accent-[#0072CE]"
            />
            <input
              type="number"
              min={1} step={1}
              value={km}
              onChange={(e) => setKm(Math.max(1, Number(e.target.value || 1)))}
              onKeyDown={(e) => { if (e.key === "Enter") apply(); }}
              className="w-24 rounded-lg border px-3 py-2 text-sm focus:ring-2 focus:ring-[#0072CE]"
            />
          </div>
          <p className="text-xs text-gray-500">
            Uses Google Distance Matrix (driving distance). Each INNODIA site within range of any selected site will be included.
          </p>
        </div>
        <div className="sticky bottom-0 flex items-center justify-end gap-2 px-6 py-3 bg-gray-50 rounded-b-2xl border-t">
          <button onClick={onClose} className="rounded-lg px-4 py-2 text-sm text-gray-700 hover:bg-white border">
            Cancel
          </button>
          <button onClick={apply} className="rounded-lg px-4 py-2 text-sm font-medium text-white bg-violet-600 hover:bg-violet-700">
            Apply
          </button>
        </div>
      </div>
    </div>
  );
}

/* ================= Drawer Nearby (reworked) ================= */
function NearbyDrawer({
  open,
  baseAccountId,
  baseAccountName,
  maxKm,
  useSeparateNearbyFilters,
  filtersNearby,
  fields,
  layout,
  sideWidth,
  onSetLayout,
  onSetSideWidth,
  onHidePanel,
  onExitNearby,
  onToggleSeparate,
  onChangeFiltersNearby,
  onApplyFilters,
  onClearFilters,
  onApplyKm,
  onClear,
  closeOnBackdrop = false,
}: {
  open: boolean;
  baseAccountId?: string | null;
  baseAccountName?: string | null;
  maxKm?: number | null;
  useSeparateNearbyFilters: boolean;
  filtersNearby: FilterGroup;
  fields: FieldDef[];
  layout: NearbyLayout;
  sideWidth: number;
  onSetLayout: (l: NearbyLayout) => void;
  onSetSideWidth: (w: number) => void;
  onHidePanel: () => void;
  onExitNearby: () => void;
  onToggleSeparate: (v: boolean) => void;
  onChangeFiltersNearby: (fg: FilterGroup) => void;
  onApplyFilters: () => void;
  onClearFilters: () => void;
  onApplyKm: (nextKm: number) => void;
  onClear: () => void;
  closeOnBackdrop?: boolean;
}) {
  const [localKm, setLocalKm] = useState<number>(Math.max(1, Number(maxKm || 120)));
  useEffect(() => setLocalKm(Math.max(1, Number(maxKm || 120))), [maxKm]);

  const clampedWidth = Math.max(480, Math.min(980, Math.round(sideWidth)));

  const handleBackdropClick = useCallback(() => { if (closeOnBackdrop) onHidePanel(); }, [closeOnBackdrop, onHidePanel]);

  const applyKm = () => onApplyKm(localKm);

  return (
    <>
      {/* Backdrop */}
      <div
        className={`fixed inset-0 z-[9000] bg-black/30 backdrop-blur-[1px] transition-opacity ${open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"}`}
        onClick={handleBackdropClick}
        aria-hidden={!open}
      />

      {/* Drawer */}
      <aside
        className={
          layout === "side"
            ? `fixed right-0 top-0 z-[9010] h-full transform bg-white/95 backdrop-saturate-150 shadow-2xl transition-transform border-l ${open ? "translate-x-0" : "translate-x-full"}`
            : `fixed left-0 right-0 bottom-0 z-[9010] w-full transform bg-white/95 backdrop-saturate-150 shadow-2xl transition-transform border-t ${open ? "translate-y-0" : "translate-y-full"}`
        }
        style={layout === "side" ? { width: clampedWidth } : { height: 420 }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="nearby-drawer-title"
      >
        {/* Header */}
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3 bg-white/70">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-gray-800">
              <span className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-semibold tracking-wide bg-blue-50 text-blue-700 border-blue-200">
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden>
                  <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Z" stroke="currentColor" strokeWidth="1.5"/>
                </svg>
                NEARBY
              </span>
              <div id="nearby-drawer-title" className="truncate text-sm" title={baseAccountName || baseAccountId || "—"}>
                Base:&nbsp;
                <span className="font-semibold text-gray-900 truncate align-middle">
                  {baseAccountName || "—"}
                </span>
                {baseAccountId ? (
                  <code className="text-gray-500 ml-2 whitespace-nowrap">({baseAccountId})</code>
                ) : null}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Layout switch */}
            <div className="hidden sm:flex items-center border rounded-md overflow-hidden">
              <button
                className={`px-2 py-1 text-xs ${layout==="side"?"bg-gray-100 font-medium":""}`}
                onClick={() => onSetLayout("side")}
                title="Dock right"
              >
                Side
              </button>
              <button
                className={`px-2 py-1 text-xs ${layout==="bottom"?"bg-gray-100 font-medium":""}`}
                onClick={() => onSetLayout("bottom")}
                title="Dock bottom"
              >
                Bottom
              </button>
            </div>

            {/* Width control (solo side) */}
            {layout === "side" && (
              <div className="hidden md:flex items-center gap-1">
                <button
                  className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                  onClick={() => onSetSideWidth(Math.max(480, clampedWidth - 60))}
                  title="Narrow"
                >−</button>
                <span className="text-xs text-gray-600 w-16 text-center">{clampedWidth}px</span>
                <button
                  className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                  onClick={() => onSetSideWidth(Math.min(980, clampedWidth + 60))}
                  title="Widen"
                >+</button>
              </div>
            )}

            <button onClick={onHidePanel} className="rounded-md border px-2 py-1 text-sm hover:bg-gray-50" title="Hide panel">Hide</button>
            <button onClick={onExitNearby} className="rounded-md border px-2 py-1 text-sm hover:bg-gray-50" title="Close nearby (exit)">Close</button>
          </div>
        </div>

        {/* Body */}
        <div className="h-[calc(100%-56px-56px)] overflow-auto p-4 space-y-4">
          {/* Card: Distance */}
          <div className="rounded-xl border bg-white shadow-sm">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <span className="text-sm font-medium text-gray-800">Max driving distance (km)</span>
              <span className="inline-flex items-center text-xs rounded-full bg-gray-100 text-gray-700 px-2 py-0.5 border">
                Current: <b className="ml-1">{localKm}</b>
              </span>
            </div>
            <div className="p-3 space-y-3">
              <div className="flex items-center gap-3">
                <input
                  type="range"
                  min={1}
                  max={1000}
                  step={1}
                  value={localKm}
                  onChange={(e) => setLocalKm(Math.max(1, Number(e.target.value)))}
                  className="flex-1 accent-[#0072CE]"
                />
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={localKm}
                  onChange={(e) => setLocalKm(Math.max(1, Number(e.target.value || 1)))}
                  onKeyDown={(e) => { if (e.key === "Enter") applyKm(); }}
                  className="w-24 rounded-md border px-2 py-1.5 text-sm focus:ring-2 focus:ring-[#0072CE]"
                />
                <button
                  className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
                  onClick={applyKm}
                  title="Re-run nearby with this distance"
                >
                  Apply km
                </button>
              </div>
              <p className="text-xs text-gray-500">Re-applies the nearby search using Google Distance Matrix.</p>
            </div>
          </div>

          {/* Card: Filters */}
          <div className="rounded-xl border bg-white shadow-sm overflow-hidden">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <span className="text-sm font-medium text-gray-800">Nearby filters</span>
              <label className="inline-flex items-center gap-2 text-sm text-gray-700 select-none">
                <input
                  type="checkbox"
                  className="rounded border-gray-300"
                  checked={useSeparateNearbyFilters}
                  onChange={(e) => onToggleSeparate(e.target.checked)}
                />
                Use different filters
              </label>
            </div>

            {useSeparateNearbyFilters ? (
              <div className="p-3">
                <FilterBuilder value={filtersNearby} onChange={onChangeFiltersNearby} fields={fields.filter((f: any) => !EXCLUDED_FILTER_FIELDS.has(f.key))} />
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button
                    className="rounded-md bg-emerald-600 px-3 py-1.5 text-sm text-white hover:bg-emerald-700"
                    onClick={onApplyFilters}
                  >
                    Apply to nearby
                  </button>
                  <button
                    className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
                    onClick={onClearFilters}
                    title="Clear and re-run nearby with no extra filters"
                  >
                    Clear nearby filters
                  </button>
                </div>
              </div>
            ) : (
              <div className="px-3 py-2 text-xs text-gray-600">
                Using the same filter set as the global Filters panel.
              </div>
            )}
          </div>
        </div>

        {/* Sticky action bar */}
        <div className="h-14 flex items-center justify-between gap-3 px-4 border-t bg-white/80 backdrop-blur-sm">
          <div className="text-xs text-gray-600">
            <span className="inline-block mr-2">Layout:</span>
            <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 border text-gray-700">
              {layout === "side" ? "Side" : "Bottom"}
            </span>
            {typeof maxKm === "number" && (
              <span className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 border border-blue-200 text-blue-700 ml-2">
                ≤ {maxKm} km
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
              onClick={onClear}
              title="Exit nearby mode and clear results"
            >
              Clear nearby
            </button>
            <button
              className="rounded-md bg-[#0072CE] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
              onClick={applyKm}
              title="Re-run nearby now"
            >
              Re-run
            </button>
          </div>
        </div>
      </aside>
    </>
  );
}

// Badge (si lo necesitas)
function TypeBadge({ type }: { type?: string | null }) {
  const t = (type || "").toLowerCase();
  let label = "—";
  let dot = COLOR_NEUTRAL;
  let pill = "bg-gray-100 text-gray-700 border-gray-200";

  if (t === "qualification") { label = "Qualification"; dot = COLOR_QUALIF; pill = "bg-emerald-100 text-emerald-700 border-emerald-200"; }
  else if (t === "profiling"){ label = "Profiling";     dot = COLOR_PROFILING; pill = "bg-blue-100 text-blue-700 border-blue-200"; }
  else if (t === "both")     { label = "Both";          dot = COLOR_BOTH; pill = "bg-violet-100 text-violet-700 border-violet-200"; }

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${pill}`}>
      <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ backgroundColor: dot }} />
      {label}
    </span>
  );
}

function shallowEqual(a: any, b: any) {
  // Para strings/números/bools/null/undefined vale con ===
  if (a === b) return true;
  // Caso raro de object/array (p.ej. si backend manda algo compuesto)
  if (typeof a === "object" && typeof b === "object" && a && b) {
    try {
      return JSON.stringify(a) === JSON.stringify(b);
    } catch {
      return false;
    }
  }
  return false;
}

function mergeFilledCells(
  baseRows: ExplorerRow[],
  filled: Array<{ account_id: string; data: Record<string, any> }>
): { rows: ExplorerRow[]; changed: boolean } {
  if (!Array.isArray(baseRows) || !Array.isArray(filled) || filled.length === 0) {
    dbg("[mergeFilledCells] no-op", { baseRows: baseRows?.length ?? 0, filled: filled?.length ?? 0 });
    return { rows: baseRows, changed: false };
  }

  // Agrupa patches por account_id
  const patches = new Map<string, Record<string, any>>();
  for (const fr of filled) {
    if (!fr?.account_id || !fr?.data) continue;
    const prev = patches.get(fr.account_id) || {};
    patches.set(fr.account_id, { ...prev, ...fr.data });
  }

  let anyChanged = false;

  const nextRows = baseRows.map((row) => {
    const patch = patches.get(row.account_id);
    if (!patch) return row;

    const prevData = row.data || {};
    let rowChanged = false;
    // Construye newData solo si algo cambia
    let newData: Record<string, any> | null = null;

    for (const [k, v] of Object.entries(patch)) {
      const prev = prevData[k];
      if (!shallowEqual(prev, v)) {
        if (!newData) newData = { ...prevData };
        newData[k] = v;
        rowChanged = true;
      }
      // Extra: si rellenamos sf.Account.Id y falta account_id en la fila, sincronízalo
      if ((k === "sf.Account.Id" || k === "Account.Id") && (!row.account_id || String(row.account_id).trim() === "")) {
        (row as any).account_id = String(v || "");
      }
    }

    if (!rowChanged) return row;

    anyChanged = true;
    return { ...row, data: newData! };
  });

  dbg("[mergeFilledCells] done", { changed: anyChanged });
  return { rows: nextRows, changed: anyChanged };
}

const containsCI = (row: any, columnId: string, filterValue: string) => {
  try {
    // TanStack passes a Row object with getValue, but some callers (or older bundles) might
    // pass a plain object. Be defensive: try row.getValue first, otherwise fall back to
    // reading from original via our readDataCell helper or direct property access.
    let v: any = undefined;
    if (row && typeof row.getValue === "function") {
      v = row.getValue<any>(columnId);
    } else if (row && row.original) {
      // columnId might be the key like "sf.Account.Name" or a simple id
      v = readDataCell(row.original as any, columnId);
    } else if (row && typeof row === "object") {
      v = (row as any)[columnId];
    }

    if (v === null || v === undefined) return false;
    const s = String(v).toLowerCase();
    return s.includes(String(filterValue ?? "").toLowerCase());
  } catch (e) {
    // Any unexpected error should not break typing in the filter box — treat as no-match
    // and surface a console.debug for debugging.
    // eslint-disable-next-line no-console
    console.debug("containsCI fallback due to error:", e);
    return false;
  }
};

type Preset = { id: string; label: string; keys: string[] };
function orderKeysByPresets(allKeys: string[], presets: Preset[]): string[] {
  const rank = new Map<string, number>();
  const HUGE = 1e9;
  presets.forEach((p, pi) => {
    p.keys.forEach((k, ki) => {
      if (!rank.has(k)) rank.set(k, pi * 10000 + ki);
    });
  });
  return [...allKeys].sort((a, b) => (rank.get(a) ?? HUGE) - (rank.get(b) ?? HUGE));
}

function enrichFieldsWithGroups(fields: FieldDef[], sfKeyToGroupMap: Map<string,string>): LocalFieldDef[] {
  return fields.map((f) => {
    const out: LocalFieldDef = { ...f };
    const src = (f.source || "").toLowerCase();
    if (src === "qual") {
      if (!out.group || !out.group.trim()) {
        out.group = f.qual_section || "Qualification forms";
      }
      return out;
    }
    if (out.group && out.group.trim()) return out;
    if (src === "site") out.group = "Site";
    else if (src === "sf") out.group = sfKeyToGroupMap.get(f.key) || "Salesforce (other)";
    else out.group = "Other";
    return out;
  });
}

function hasCell(d: Record<string, any> | undefined, key: string) {
  if (!d) return false;
  // valor directo
  const v0 = d[key];
  if (v0 !== undefined && v0 !== null && v0 !== "") return true;

  // variantes típicas (con/sin 'sf.' y con underscores)
  const base = key.replace(/^sf\./, "");
  const variants = [key, `sf.${base}`, base, key.replace(/\./g, "_"), base.replace(/\./g, "_"), `sf.${base.replace(/\./g, "_")}`];
  for (const k of variants) {
    const v = (d as any)[k];
    if (v !== undefined && v !== null && v !== "") return true;
  }
  return false;
}

// ¿Hace falta pedir fill para estas filas/columnas?
function needsFill(rows: ExplorerRow[], cols: string[]): boolean {
  if (!rows?.length || !cols?.length) return false;
  // ignora columnas no rellenables
  const colsEff = cols.filter(c => !UNFILLABLE_COLUMNS.has(c));
  if (!colsEff.length) return false;
  for (const r of rows) {
    for (const c of colsEff) {
      const v = readDataCell(r as any, c);
      if (v === null || v === undefined || String(v).trim() === "") {
        return true;
      }
    }
  }
  return false;
}


function getAccountNameFromRow(r: ExplorerRow): string {
  try {
    const v = readDataCell(r as any, "sf.Account.Name");
    if (v !== undefined && v !== null && String(v).trim() !== "") return String(v);
    return (r && (r.account_name || "")) || "";
  } catch (e) {
    // Defensive: any unexpected shape should not break sorting/filtering
    // eslint-disable-next-line no-console
    console.debug("getAccountNameFromRow fallback due to error:", e);
    try { return String((r && r.account_name) || ""); } catch { return ""; }
  }
}

// ===== Helpers to filter by a list of Account IDs (from ChatView or URL) =====
function getAccountIdFilterKey(fields: FieldDef[]): string | null {
  // Prefer a true SF Account.Id if present, otherwise fall back to site/account id keys if exposed
  const keys = new Set((fields || []).map(f => f.key));
  if (keys.has("sf.Account.Id")) return "sf.Account.Id";
  if (keys.has("site.account_id")) return "site.account_id";
  if (keys.has("account_id")) return "account_id";
  return null;
}

function buildAccountIdsFilter(fieldKey: string, ids: string[]): FilterGroup {
  // FilterBuilder no representa bien el operador "in" y termina dejando un único
  // campo con valores separados por comas. Para que funcione y se vea como en la
  // UI de la captura, generamos un grupo OR con una regla de igualdad por id.
  const unique = Array.from(new Set(ids.map(String).filter(Boolean)));
  return {
    logic: "OR",
    rules: unique.map((id) => ({
      field: fieldKey,
      operator: "=",
      value: id,
    })) as any[],
  };
}

// Convierte filtros donde el value trae IDs separados por comas
// en un grupo OR con reglas = por cada id.
function normalizeAccountIdCsvToOr(f: FilterGroup): FilterGroup {
  // Deep-walk: if a rule has a string value with commas, expand it into an OR group
  // preserving existing nested AND/OR groups.
  const clone = (obj: any) => JSON.parse(JSON.stringify(obj || { logic: "AND", rules: [] }));

  const expandNode = (node: any): any => {
    // If it's a group
    if (node && Array.isArray(node.rules)) {
      const children = node.rules.map(expandNode);

      // If any direct child expansion returned a "csv-expanded" marker, we should OR them
      const hasCsv = children.some((c: any) => c && c.__csvExpanded === true);

      // Preserve expression-mode logic strings like "1 AND (2 OR 3)" verbatim
      // (only collapse to plain AND/OR when a CSV expansion forced new sub-rules).
      const isExprLogic = typeof node.logic === "string" && /\d/.test(node.logic);
      const nextLogic = hasCsv
        ? "OR"
        : (isExprLogic ? node.logic : (node.logic === "OR" ? "OR" : "AND"));

      return {
        logic: nextLogic,
        rules: children.map((c: any) => (c && c.__csvExpanded === true ? c.rules : c)).flat(),
      };
    }

    // Leaf rule
    const val = node?.value;
    if (typeof val === "string" && val.includes(",")) {
      const parts = val.split(",").map((s) => s.trim()).filter(Boolean);
      if (parts.length > 1) {
        return {
          __csvExpanded: true,
          logic: "OR",
          rules: parts.map((p) => ({
            field: node.field,
            operator: node.operator ?? "=",
            value: p,
          })),
        };
      }
    }
    return node;
  };

  try {
    const root = clone(f);
    const expanded = expandNode(root);
    // expandNode can return a leaf; enforce group shape at root
    if (expanded && !Array.isArray(expanded.rules)) {
      return { logic: "AND", rules: [expanded as any] } as FilterGroup;
    }
    // Strip internal marker if present
    const strip = (n: any): any => {
      if (!n) return n;
      if (Array.isArray(n.rules)) {
        return { logic: n.logic, rules: n.rules.map(strip) };
      }
      const { __csvExpanded, ...rest } = n;
      return rest;
    };
    return strip(expanded);
  } catch {
    return f;
  }
}

/* ================= Nearby API ================= */
type NearbyResult = {
  base: { account_id: string; lat: number; lng: number };
  neighbors_all: ExplorerPoint[];
  points: ExplorerPoint[];
  rows: ExplorerRow[];
  meta: { mode: "drive_km"; max_km: number };
};

async function fetchNearbyDriveKm(opts: {
  baseAccountId: string;
  maxKm: number;
  filters: FilterGroup;
  columns: string[];
}): Promise<NearbyResult> {
  const res = await fetch("/api/explorer/search/within-drive-km", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({
      base_account_id: opts.baseAccountId,
      max_km: opts.maxKm,
      filters: opts.filters,
      columns: opts.columns,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}


/* ================= Página principal ================= */
export default function ExplorerView() {
  const [bootDone, setBootDone] = useState(false);
  const [bootLoading, setBootLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // NUEVO: estado de sesión Salesforce
  const [sfAuthOk, setSfAuthOk] = useState<boolean | null>(null);

  const [fieldDefs, setFieldDefs] = useState<FieldDef[] | any>([]);
  const [allColumnKeys, setAllColumnKeys] = useState<string[]>([]);

  const [visibleColumns, setVisibleColumns] = useState<string[]>(
    safeParse<string[]>(localStorage.getItem(LS_KEYS.visibleColumns), DEFAULT_VISIBLE_COLUMNS)
  );
  const [sorting, setSorting] = useState<SortingState>(
    safeParse<SortingState>(localStorage.getItem(LS_KEYS.sorting), [{ id: "country", desc: false }])
  );
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>(
    safeParse<ColumnFiltersState>(localStorage.getItem(LS_KEYS.columnFilters), [])
  );
  const [globalFilter, setGlobalFilter] = useState<string>(
    safeParse<string>(localStorage.getItem(LS_KEYS.globalFilter), "")
  );
  const [pageSize, setPageSize] = useState<number>(
    safeParse<number>(localStorage.getItem(LS_KEYS.pageSize), 10)
  );
  const [pageIndex, setPageIndex] = useState<number>(0);
  const [columnOrder, setColumnOrder] = useState<string[]>(
    safeParse<string[]>(localStorage.getItem(LS_KEYS.columnOrder), [])
  );
  // Anchos que el usuario ha fijado arrastrando el borde de la cabecera.
  // Una columna ausente de este mapa se auto-dimensiona según su contenido.
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>(
    () => sanitizeColumnWidths(safeParse<unknown>(localStorage.getItem(LS_KEYS.columnWidths), {}))
  );

  const [points, setPoints] = useState<ExplorerPoint[]>([]);
  const [rows, setRows] = useState<ExplorerRow[]>([]);
  const [filters, setFilters] = useState<FilterGroup>({ logic: "AND", rules: [] });
  // NUEVO: caché con TODAS las columnas
  const [fullPoints, setFullPoints] = useState<ExplorerPoint[]>([]);
  const [fullRows, setFullRows] = useState<ExplorerRow[]>([]);
  // NUEVO: caché nearby con TODAS las columnas
  const [fullNearbyPoints, setFullNearbyPoints] = useState<ExplorerPoint[]>([]);
  const [fullNearbyRows, setFullNearbyRows] = useState<ExplorerRow[]>([]);
  
  // Refs para controlar autofill (evita loops y reentradas)
  const fillBusyRef = useRef(false);
  const lastFillSigRef = useRef<string>("");
  // Cooldown si el último fill no trajo cambios
  const lastNoChangeAtRef = useRef<number>(0);

  // ===== Nearby =====
  const [nearbyBaseName, setNearbyBaseName] = useState<string | null>(null);
  const [lastNearbyBaseName, setLastNearbyBaseName] = useState<string | null>(null);
  const [nearbyActive, setNearbyActive] = useState(false);
  const [nearbyPanelOpen, setNearbyPanelOpen] = useState(false);
  const [filterCollapsed, setFilterCollapsed] = useState(false);
  const [nearbyLayout, setNearbyLayout] = useState<NearbyLayout>(
    safeParse<NearbyLayout>(localStorage.getItem(LS_KEYS.nearbyLayout), "side")
  );
  
  // === NEW: highlight desde Chat (ids de cuentas resaltadas) ===
  const [highlightIds, setHighlightIds] = useState<string[]>([]);
  // Para autoscroll al primer resaltado
  const firstHighlightRef = useRef<string | null>(null);

  const [nearbySideWidth, setNearbySideWidth] = useState<number>(
    safeParse<number>(localStorage.getItem(LS_KEYS.nearbySideWidth), 520)
  );

  const [nearbyPoints, setNearbyPoints] = useState<ExplorerPoint[]>([]);
  const [nearbyRows, setNearbyRows] = useState<ExplorerRow[]>([]);
  const [neighborsAll, setNeighborsAll] = useState<ExplorerPoint[]>([]);
  const [nearbyMeta, setNearbyMeta] = useState<{ max_km: number } | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<string | null>(null);

  const [useSeparateNearbyFilters, setUseSeparateNearbyFilters] = useState(false);
  const [filtersNearby, setFiltersNearby] = useState<FilterGroup>({ logic: "AND", rules: [] });
  const [autoSearchTrigger, setAutoSearchTrigger] = useState(0);
  const [lastNearbyParams, setLastNearbyParams] = useState<{ baseAccountId?: string; baseAccountIds?: string[]; maxKm: number } | null>(null);

  // ===== Details state (NEW) =====
  const [detailsOpen, setDetailsOpen] = useState(false);

  // ====== AI output (texto + tabla opcional) ======
  type AITable = { columns: Array<{ key: string; label?: string }>; rows: Array<Record<string, any>> };
  const [aiAnswer, setAiAnswer] = useState<string | null>(null);
  const [aiTable, setAiTable] = useState<AITable | null>(null);

  // Persistencia
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.visibleColumns, JSON.stringify(visibleColumns)); }, [visibleColumns]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.sorting, JSON.stringify(sorting)); }, [sorting]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.columnFilters, JSON.stringify(columnFilters)); }, [columnFilters]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.globalFilter, JSON.stringify(globalFilter)); }, [globalFilter]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.pageSize, JSON.stringify(pageSize)); }, [pageSize]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.columnOrder, JSON.stringify(columnOrder)); }, [columnOrder]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.columnWidths, JSON.stringify(columnWidths)); }, [columnWidths]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.nearbyLayout, JSON.stringify(nearbyLayout)); }, [nearbyLayout]);
  useDebouncedEffect(() => { localStorage.setItem(LS_KEYS.nearbySideWidth, JSON.stringify(nearbySideWidth)); }, [nearbySideWidth]);

  // --- Redimensionado de columnas (arrastrar el borde derecho de la cabecera) ---
  // Guarda el "cierre" del drag en curso para poder abortarlo si desmontamos.
  const endColumnResizeRef = useRef<(() => void) | null>(null);
  // Mientras se arrastra el tirador, el <th> no debe iniciar su drag-to-reorder.
  const isResizingRef = useRef(false);
  useEffect(() => () => { endColumnResizeRef.current?.(); }, []);

  const startColumnResize = useCallback((columnId: string, e: React.MouseEvent<HTMLElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const th = e.currentTarget.closest("th");
    if (!th) return;

    // Si un arrastre anterior se quedó vivo (mouseup perdido), ciérralo antes de
    // registrar otro: si no, sus listeners quedarían huérfanos y seguirían escribiendo.
    endColumnResizeRef.current?.();

    // Arrancamos del ancho REAL renderizado, no de un tamaño por defecto,
    // para que la columna no pegue un salto en el primer píxel de arrastre.
    const startX = e.clientX;
    const startWidth = th.getBoundingClientRect().width;

    const onMove = (ev: MouseEvent) => {
      // Si soltaste el botón fuera de la ventana no llega el mouseup: el drag
      // se quedaría pegado. ev.buttons === 0 significa "ya no hay botón pulsado".
      if (ev.buttons === 0) { onEnd(); return; }
      const raw = Math.round(startWidth + ev.clientX - startX);
      const next = Math.min(MAX_COLUMN_WIDTH, Math.max(MIN_COLUMN_WIDTH, raw));
      setColumnWidths((prev) => (prev[columnId] === next ? prev : { ...prev, [columnId]: next }));
    };
    const onEnd = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onEnd);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      isResizingRef.current = false;
      // Solo suéltate del ref si sigues siendo el drag activo.
      if (endColumnResizeRef.current === onEnd) endColumnResizeRef.current = null;
    };

    isResizingRef.current = true;
    endColumnResizeRef.current = onEnd;
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onEnd);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  // Doble clic en el tirador: la columna vuelve a auto-dimensionarse al contenido.
  const resetColumnWidth = useCallback((columnId: string) => {
    setColumnWidths((prev) => {
      if (!(columnId in prev)) return prev;
      const { [columnId]: _dropped, ...rest } = prev;
      return rest;
    });
  }, []);

  // Auto-search when all filter rules are removed so the map restores full results
  const prevFiltersRulesLenRef = useRef(0);
  useEffect(() => {
    const prev = prevFiltersRulesLenRef.current;
    prevFiltersRulesLenRef.current = filters.rules.length;
    if (prev > 0 && filters.rules.length === 0 && !busy && bootDone) {
      onSearch();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.rules.length]);

  const idsSig = useMemo(() => {
    const currentRows = nearbyActive ? fullNearbyRows : fullRows;
    const ids = Array.from(new Set((currentRows || []).map(r => r.account_id).filter(Boolean)));
    ids.sort();
    return ids.join(",");
  }, [nearbyActive, fullNearbyRows, fullRows]);

  const colsSig = useMemo(() => {
    const cols = Array.from(new Set(visibleColumns || []));
    cols.sort();
    return cols.join("|");
  }, [visibleColumns]);

  // Preguntar al agente y abrir Chart si manda visualization
  const askAIAndMaybeShowChart = async (prompt: string) => {
    try {
      setBusy(true);
      const resp: ChatResponse = await askAI(prompt);
      // Mostrar respuesta textual
      setAiAnswer(resp.answer?.trim() ? resp.answer.trim() : null);
      // Tabla opcional del agente
      if (resp.table?.rows && resp.table?.columns) {
        setAiTable({
          columns: resp.table.columns.map((c: any) => ({
            key: String(c.key ?? c.id ?? c.name ?? "").trim(),
            label: String(c.label ?? c.title ?? c.key ?? "").trim() || undefined,
          })),
          rows: resp.table.rows as any[],
        });
      } else {
        setAiTable(null);
      }
      if (resp.visualization) {
        const v = resp.visualization;
        setChartType(v.type === "line" ? "line" : "bar");
        setChartXKey(v.xKey);
        setChartYKeys(Array.isArray(v.yKeys) ? v.yKeys : []);
        setChartTitle(v.meta?.title || "Explorer Chart");
        setChartOpen(true);
      }
      // Si además quieres inyectar la tabla devuelta en la tabla de Explorer:
      // if (resp.table?.rows?.length) { ... }  // opcional
    } catch (e: any) {
      console.error(e);
      alert(e?.message || "AI error");
    } finally {
      setBusy(false);
    }
  };
 

  // === NEW: autoscroll a primera fila resaltada cuando cambien datos o resaltados ===
  const tableContainerRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const targetId = firstHighlightRef.current;
    if (!targetId) return;
    // ¿existe en filas actuales?
    const vr = (nearbyActive ? fullNearbyRows : fullRows) || [];
    const idx = vr.findIndex(r => r.account_id === targetId);
    if (idx === -1) return;
    // hace scroll suave a esa posición (aprox.)
    const rowHeight = 40; // aprox. altura de fila
    const top = Math.max(0, idx * rowHeight - 120);
    try {
      tableContainerRef.current?.scrollTo({ top, behavior: "smooth" });
    } catch {}
    // sólo la primera vez
    firstHighlightRef.current = null;
  }, [highlightIds, nearbyActive, fullNearbyRows, fullRows]);

  // Utilidad: limpiar highlight
  const clearHighlight = useCallback(() => {
    setHighlightIds([]);
  }, []);

  // AUTO-FILL
  useDebouncedEffect(() => {
    if (!bootDone || bootLoading) {
      dbg("fill skipped: boot not ready", { bootDone, bootLoading });
      return;
    }

    const currentRows = nearbyActive ? fullNearbyRows : fullRows;

    // Columnas rellenables (excluye UNFILLABLE_COLUMNS)
    const reqCols = Array.from(new Set((visibleColumns || []).filter(c => !UNFILLABLE_COLUMNS.has(c))));
    if (!reqCols.length || !needsFill(currentRows, reqCols)) {
      dbg("fill skipped: no missing visible cells");
      return;
    }

    const colsReqSig = reqCols.slice().sort().join("|");
    const sig = `${nearbyActive ? "nearby" : "global"}::${idsSig}::${colsReqSig}`;
    if (fillBusyRef.current) { dbg("fill skipped: busy"); return; }
    if (lastFillSigRef.current === sig) { dbg("fill skipped: same signature", sig); return; }

    // Cooldown si la última vez no hubo cambios
    const now = Date.now();
    if (now - lastNoChangeAtRef.current < 5000) {
      dbg("fill skipped: cooldown (no-change last run)");
      return;
    }

    fillBusyRef.current = true;
    lastFillSigRef.current = sig;
    dbg("fill start", sig);

    (async () => {
      try {
        const ids = idsSig ? idsSig.split(",") : [];
        if (!ids.length) return;

        const resp = await explorerFillColumns({ account_ids: ids, columns: reqCols });
        const filled = Array.isArray(resp?.rows) ? resp.rows : [];

        const { rows: merged, changed } = mergeFilledCells(currentRows, filled);
        dbg("fill done", { changed, filledRows: filled.length });

        if (changed) {
          if (nearbyActive) {
            setFullNearbyRows(merged);
            setNearbyRows(prev => mergeFilledCells(prev, filled).rows);   // opcional, si lo usas en otros sitios
          } else {
            setFullRows(merged);
            setRows(prev => mergeFilledCells(prev, filled).rows);         // opcional
          }
          lastNoChangeAtRef.current = 0;          // hubo cambios → sin cooldown
        } else {
          lastNoChangeAtRef.current = Date.now(); // marca cooldown
        }
      } catch (e) {
        dbgwarn("columns/fill failed:", e);
      } finally {
        fillBusyRef.current = false;
      }
    })();
  }, [
    bootDone,
    bootLoading,
    nearbyActive,
    idsSig,
    colsSig,
  ], 250);

  // Bootstrap
  const { mutate } = useSWRConfig();
  const { data: bootData, isLoading: bootIsLoading } = useSWR(
    EXPLORER_BOOT_KEY,
    async () => {
      // 0) Comprobar sesión SF antes de bootstrap
      try {
        const me = await salesforceMe();
        if (!me || me.authenticated === false) {
          // ❌ NO redirigir automáticamente
          setSfAuthOk(false);
          window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: false } }));
          // Forzamos un error controlado para que entre al onError con 401
          const err: any = new Error("HTTP 401");
          err.status = 401;
          throw err;
        }
      } catch (e) {
        // si salesforceMe() lanza error → tratamos igual
        const err: any = new Error("HTTP 401");
        err.status = 401;
        throw err;
      }

      const fldsResp = await getExplorerFields();
      const flds: FieldDef[] = Array.isArray((fldsResp as any)?.fields)
        ? (fldsResp as any).fields
        : Array.isArray(fldsResp)
        ? (fldsResp as any)
        : [];
      const allKeys = flds.map((f) => f.key);

      await explorerBootstrap();

      // SIEMPRE pedimos TODAS las columnas
      const resp = await explorerSearch({
        filters: { logic: "AND", rules: [] },
        columns: allKeys,
      } as any);

      // sesión válida
      setSfAuthOk(true);
      window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: true } }));

      return { flds, allKeys, resp };
    },
    {
      revalidateOnFocus: false,
      revalidateIfStale: false,
      revalidateOnReconnect: false,
      revalidateOnMount: true,
      shouldRetryOnError: false,
      onError: (e: any) => {
        // Si simplemente fue un timeout/abort del fetch, no ensuciamos la UI con "Timeout"
        if (isTimeoutErr(e)) {
          console.warn("Bootstrap/init timeout — ignoring UI error", e);
          setBootLoading(false);
          return;
        }
        const status =
          e?.status ??
          e?.response?.status ??
          Number(
            String(e?.message || "").match(/HTTP\s+(\d{3})/)?.[1] || NaN
          );

        if (status === 401 || status === 403) {
          // ✅ No auto-redirect: solo estado y banner
          setSfAuthOk(false);
          window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: false } }));
          setErr(null);
          setBootLoading(false);
        } else {
          console.error("Bootstrap/init error:", e);
          setErr(e?.message ?? "Bootstrap error");
          setBootLoading(false);
        }

        setPoints([]);
        setRows([]);
      },
    }
  );

  useEffect(() => {
    const off = listenExplorerChange(() => mutate(EXPLORER_BOOT_KEY));
    return off;
  }, [mutate]);

  // Mapas y fields
  const sfKeyToGroupMap = useMemo(() => {
    const m = new Map<string, string>();
    Object.entries(SF_GROUPS).forEach(([groupName, keys]) => {
      keys.forEach((k) => { if (!m.has(k)) m.set(k, groupName); });
    });
    return m;
  }, []);

  useEffect(() => {
    setBootLoading(bootIsLoading);
    if (!bootData) return;

    const { flds, allKeys, resp } = bootData;
    const fldsWithGroup = enrichFieldsWithGroups(flds, sfKeyToGroupMap);

    const pts = Array.isArray(resp?.points) ? resp.points : [];
    const rws = Array.isArray(resp?.rows) ? resp.rows : [];
    const hasQualificationData =
      pts.some((p: any) => p?.badges?.qualification) ||
      rws.some((row: any) => {
        const data = row?.data || {};
        return Object.entries(data).some(
          ([k, v]) => k.startsWith("qual.") && v != null && v !== "" && v !== false
        );
      });

    const fieldDefsForUi = hasQualificationData
      ? fldsWithGroup
      // Si no hay ningún dato de Qualification, ocultamos automáticamente todos los campos qual.*
      // para evitar que aparezca una sección vacía en el selector de columnas.
      : fldsWithGroup.filter((f) => !(f.key || "").startsWith("qual."));

    setFieldDefs(fieldDefsForUi as any);
    setAllColumnKeys(allKeys);

    setVisibleColumns((prev) => {
      const allowed = new Set(fieldDefsForUi.map((f) => f.key));
      const base = prev && Array.isArray(prev) ? prev : DEFAULT_VISIBLE_COLUMNS;
      // No excluimos a la fuerza ShippingCity/ShippingCountry aquí;
      // si no deseas mostrarlas, ocúltalas en la lista de `fields` del ColumnPicker.
      const cleaned = base.filter((k) => allowed.has(k));
      if (!cleaned.includes("sf.Account.Name") && allowed.has("sf.Account.Name")) {
        cleaned.unshift("sf.Account.Name");
      }
      return cleaned.length ? cleaned : DEFAULT_VISIBLE_COLUMNS.filter((k) => allowed.has(k));
    });

    // Rellena caché completa y la vista actual
    // Instrumentation: trace whether `cs` is present on points coming from the server
    try {
      const withCs = pts.filter((p: any) => p && p.cs !== undefined).length;
      const sample = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
      console.info("[CTS][DEBUG] bootstrap -> setting fullPoints:", { total: pts.length, withCs, sampleId: sample?.account_id ?? null, sampleHasCs: sample ? (sample.cs !== undefined) : null, sample });
    } catch (e) {
      console.info("[CTS][DEBUG] bootstrap -> setting fullPoints: (instrumentation failed)", e);
    }
    setFullPoints(pts); 
    setFullRows(rws);
    // ⬇️ DEBUG
    ;(window as any).__rows_search = rws;
    console.log("[CTS][DEBUG] onSearch rows sample:", rws?.[0]);
    try {
      const withCs2 = pts.filter((p: any) => p && p.cs !== undefined).length;
      const sample2 = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
      console.info("[CTS][DEBUG] bootstrap -> setting points:", { total: pts.length, withCs: withCs2, sampleId: sample2?.account_id ?? null, sampleHasCs: sample2 ? (sample2.cs !== undefined) : null, sample: sample2 });
    } catch (e) {
      console.info("[CTS][DEBUG] bootstrap -> setting points: (instrumentation failed)", e);
    }
    setPoints(pts); 
    setRows(rws);
    setBootDone(true);
    setBootLoading(false);
  }, [bootData, bootIsLoading, sfKeyToGroupMap]);


  // // ⬇️ AQUI pega el useEffect opcional (justo después del anterior)
  // useEffect(() => {
  //   if (!bootDone || bootLoading) return;
  //   const currentRows = nearbyActive ? nearbyRows : rows;
  //   const ids = (currentRows || []).map(r => r.account_id).filter(Boolean);
  //   const cols = (visibleColumns || []);
  //   if (!ids.length || !cols.length) return;

  //   explorerFillColumns({ account_ids: ids, columns: cols })
  //     .then(resp => {
  //       const filled = Array.isArray(resp?.rows) ? resp.rows : [];
  //       const { rows: merged, changed } = mergeFilledCells(currentRows, filled);
  //       if (!changed) return;
  //       if (nearbyActive) setNearbyRows(merged); else setRows(merged);
  //     })
  //     .catch(() => {});
  //   // eslint-disable-next-line react-hooks/exhaustive-deps
  // }, [bootDone]);


  // Etiquetas
  const labelByKey = useMemo(() => {
    const m = new Map<string, string>();
    const arr = Array.isArray(fieldDefs) ? (fieldDefs as any[]) : [];
    for (const f of arr) {
      if (!f || typeof f.key !== "string") continue;
      const pretty = prettyLabelFromKeyLabel(f.key, (f as any).label);
      m.set(f.key, pretty);
    }
    return m;
  }, [fieldDefs]);

  const activeRuleCount = useMemo(() => {
    const count = (g: { rules?: any[] }): number => {
      let n = 0;
      for (const r of (g.rules ?? [])) {
        if (r && typeof (r as any).field === "string") n++;
        else n += count(r as any);
      }
      return n;
    };
    return count(filters);
  }, [filters]);

  const qualPresetKeys = useMemo(
    () => (Array.isArray(fieldDefs) ? (fieldDefs as FieldDef[]).filter(f => String(f.key).startsWith("qual.")).map(f => f.key) : []),
    [fieldDefs]
  );

  // const presetsForUI = useMemo(() => {
  //   const base: { id: string; label: string; keys: string[] }[] = [
  //     { id: "basic",   label: "Basic",                    keys: PRESETS.Basic },
  //     { id: "opp",     label: "Opportunity Core",         keys: PRESETS.OpportunityCore },
  //     { id: "acc",     label: "Account Basics",           keys: PRESETS.AccountBasics },
  //     { id: "qual_sf", label: "Qualification (SF dates)", keys: PRESETS.QualificationDates },
  //     { id: "clin",    label: "Clinical Details",         keys: PRESETS.ClinicalCapacity },
  //     { id: "audit",   label: "Audit/Timeline",           keys: PRESETS.NotesAndVerification },
  //   ];
  //   if (qualPresetKeys.length) base.push({ id: "qual_dyn", label: "Qualification (qual.* from Excel)", keys: qualPresetKeys });
  //   return base;
  // }, [qualPresetKeys]);


  // Re-busca usando las columnas visibles actuales (optimiza payload)
  const refreshForVisibleColumns = async () => {
    // No molestes si no está el bootstrap listo
    if (!bootDone) return;
      // ⟵ Ya tenemos fullRows/fullNearbyRows con TODAS las columnas.
      // Cambiar columnas visibles NO requiere pedir nada: solo re-render.
      return;
  };

  // Buscar con filtros (no cerramos nearby)
  const onSearch = async () => {
    setBusy(true);
    setErr(null);
    fillBusyRef.current = true;
    try {
      console.groupCollapsed("[CTS] Search triggered");
      console.log("Current filters:", filters);
      console.log("All columns:", allColumnKeys.length);

      // Normalizamos filtros
      const normalizedFilters = normalizeAccountIdCsvToOr(filters);
      const payload = { filters: normalizedFilters, columns: allColumnKeys };

      // Ejecuta fetch directo sin interferencia de caches previas
      const res = await fetch("/api/explorer/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}: ${text || res.statusText}`);
      }

      // Clonamos el body antes de parsear JSON
      const textBody = await res.text();
      let json: any;
      try {
        json = JSON.parse(textBody);
      } catch (e) {
        console.error("❌ JSON parse failed:", e, textBody.slice(0, 300));
        throw new Error("Invalid JSON in response");
      }

      // Exit nearby mode and clear last fill signature before updating points/rows
      setNearbyActive(false);
      setNearbyPanelOpen(false);

      const pts = Array.isArray(json?.points) ? json.points : [];
      const rws = Array.isArray(json?.rows) ? json.rows : [];

      setSfAuthOk(true);
      window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: true } }));

      // Instrumentation: check `cs` presence on search results
      try {
        const withCs = pts.filter((p: any) => p && p.cs !== undefined).length;
        const sample = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
        console.info("[CTS][DEBUG] search -> setting fullPoints:", { total: pts.length, withCs, sampleId: sample?.account_id ?? null, sampleHasCs: sample ? (sample.cs !== undefined) : null, sample });
      } catch (e) {
        console.info("[CTS][DEBUG] search -> setting fullPoints: (instrumentation failed)", e);
      }

      setFullPoints(pts);
      setFullRows(rws);

      try {
        const withCs2 = pts.filter((p: any) => p && p.cs !== undefined).length;
        const sample2 = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
        console.info("[CTS][DEBUG] search -> setting points:", { total: pts.length, withCs: withCs2, sampleId: sample2?.account_id ?? null, sampleHasCs: sample2 ? (sample2.cs !== undefined) : null, sample: sample2 });
      } catch (e) {
        console.info("[CTS][DEBUG] search -> setting points: (instrumentation failed)", e);
      }

      setPoints(pts);
      setRows(rws);
      lastFillSigRef.current = "";

      console.table(rws.slice(0, 5).map(r => ({
        id: r.account_id,
        name: r.account_name,
        country: r.country,
        city: r.city
      })));
      console.groupEnd();
    } catch (e: any) {
      console.error("[CTS] Search error:", e);
      if (isTimeoutErr(e)) {
        console.warn("⏱ Timeout ignored");
        return;
      }
      const status = e?.status ?? e?.response?.status ?? Number(String(e?.message || "").match(/HTTP\s+(\d{3})/)?.[1] || NaN);
      if (status === 401 || status === 403) {
        setSfAuthOk(false);
        window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: false } }));
        setErr(null);
      } else {
        setErr(e?.message || "Search failed");
      }
      setPoints([]);
      setRows([]);
    } finally {
      fillBusyRef.current = false;
      setBusy(false);
    }
  };

  // Remove a single top-level filter rule by index and re-trigger search
  const removeRuleAtIndex = useCallback((idx: number) => {
    setFilters(prev => {
      const removedId = idx + 1;  // rules are 1-indexed in the new system
      const newRules = prev.rules.filter((_, i) => i !== idx);
      const newExpr = removeRuleFromExpr(prev.logic, removedId) || defaultExpr(newRules.length);
      return { ...prev, logic: newExpr, rules: newRules };
    });
    setAutoSearchTrigger(t => t + 1);
  }, []);

  // Auto-search when a chip removal triggers it (state is already updated by then)
  useEffect(() => {
    if (autoSearchTrigger > 0) onSearch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSearchTrigger]);

  // Reset (sí cerramos nearby)
  const onReset = async () => {
    setFilters({ logic: "AND", rules: [] });
    setVisibleColumns(DEFAULT_VISIBLE_COLUMNS);
    setSorting([{ id: "country", desc: false }]);
    setColumnFilters([]);
    setGlobalFilter("");
    setPageIndex(0);
    setPageSize(10);
    setBusy(true);
    try {
      // Traemos TODO otra vez
      const resp = await explorerSearch({
        filters: { logic: "AND", rules: [] },
        columns: allColumnKeys,
      } as any);
      setSfAuthOk(true);
      window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: true } }));
      const pts = Array.isArray(resp?.points) ? resp.points : [];
      const rws = Array.isArray(resp?.rows) ? resp.rows : [];
      try {
        const withCs = pts.filter((p: any) => p && p.cs !== undefined).length;
        const sample = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
        console.info("[CTS][DEBUG] reset -> setting fullPoints:", { total: pts.length, withCs, sampleId: sample?.account_id ?? null, sampleHasCs: sample ? (sample.cs !== undefined) : null, sample });
      } catch (e) {
        console.info("[CTS][DEBUG] reset -> setting fullPoints: (instrumentation failed)", e);
      }
      setFullPoints(pts); 
      setFullRows(rws);
      ;(window as any).__rows_search = rws;
      console.log("[CTS][DEBUG] onSearch rows sample:", rws?.[0]);
      try {
        const withCs2 = pts.filter((p: any) => p && p.cs !== undefined).length;
        const sample2 = pts.find((p: any) => p?.account_id === "001Vg00000UGORhIAP");
        console.info("[CTS][DEBUG] reset -> setting points:", { total: pts.length, withCs: withCs2, sampleId: sample2?.account_id ?? null, sampleHasCs: sample2 ? (sample2.cs !== undefined) : null, sample: sample2 });
      } catch (e) {
        console.info("[CTS][DEBUG] reset -> setting points: (instrumentation failed)", e);
      }
      setPoints(pts); setRows(rws);
      setNearbyActive(false);
      setNearbyPanelOpen(false);
      setNearbyPoints([]); setNearbyRows([]); setNeighborsAll([]); setNearbyMeta(null);
      setSelectedAccountId(null);
      setLastNearbyParams(null);
    } catch (e: any) {
      if (isTimeoutErr(e)) {
        console.warn("Reset timeout — ignoring UI error", e);
        setBusy(false);
        return;
      }
      const status = e?.status ?? e?.response?.status ?? Number(String(e?.message || "").match(/HTTP\s+(\d{3})/)?.[1] || NaN);
      if (status === 401 || status === 403) {
        setSfAuthOk(false);
        window.dispatchEvent(new CustomEvent("sf-auth", { detail: { ok: false } }));
        setErr(null);
      } else {
        console.error(e); setErr(e?.message || "Reset error");
      }
      setPoints([]); setRows([]);
    } finally { setBusy(false); }
  };

  // ===== Row selection (for multi-nearby) =====
  const [selectedRowIds, setSelectedRowIds] = useState<Set<string>>(new Set());

  // ===== Nearby actions =====
  const [nearbyModalOpen, setNearbyModalOpen] = useState(false);
  const [nearbyMultiModalOpen, setNearbyMultiModalOpen] = useState(false);
  const [nearbyBaseAccount, setNearbyBaseAccount] = useState<string | null>(null);

  const openNearbyFor = (accountId: string) => {
    const name = getAccountNameByIdSafe(accountId, rows, points);
    setNearbyBaseAccount(accountId);
    setNearbyBaseName(name || null);
    setNearbyModalOpen(true);
  };

  const applyNearby = async ({ baseAccountId, maxKm }: { baseAccountId: string; maxKm: number }) => {
    setNearbyModalOpen(false);
    if (!baseAccountId) return;
    setBusy(true); setErr(null);
    try {
      const r = await fetchNearbyDriveKm({
        baseAccountId,
        maxKm,
        filters: useSeparateNearbyFilters ? filtersNearby : filters,
        // SIEMPRE todas las columnas
        columns: allColumnKeys,
      });
      setNearbyActive(true);
      setNearbyPanelOpen(true);
      const npts = Array.isArray(r.points) ? r.points : [];
      const nrws = Array.isArray(r.rows) ? r.rows : [];
      setFullNearbyPoints(npts);
      setFullNearbyRows(nrws);
      ;(window as any).__rows_nearby = nrws;
      console.log("[CTS][DEBUG] nearby rows sample:", nrws?.[0]);
      // Las vistas usan directamente los "full" (o puedes dejar copias en los no-full)
      setNearbyPoints(npts);
      setNearbyRows(nrws);
      setNeighborsAll(Array.isArray(r.neighbors_all) ? r.neighbors_all : []);
      setNearbyMeta({ max_km: r?.meta?.max_km ?? maxKm });
      setSelectedAccountId(null);
      setLastNearbyParams({ baseAccountId, maxKm });
      setLastNearbyBaseName(nearbyBaseName || getAccountNameByIdSafe(baseAccountId, rows, points));
    } catch (e: any) {
      console.error(e);
      setErr(e?.message || "Nearby search error");
    } finally {
      setBusy(false);
    }
  };

  const applyNearbyMulti = async ({ maxKm }: { maxKm: number }) => {
    setNearbyMultiModalOpen(false);
    const accountIds = Array.from(selectedRowIds);
    if (accountIds.length === 0) return;
    setBusy(true); setErr(null);
    try {
      const res = await fetch("/api/explorer/search/nearby-multi", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_account_ids: accountIds,
          max_km: maxKm,
          filters: filtersNearby,  // always use separate nearby filters for multi (not the main Explorer filter used to select the bases)
          columns: allColumnKeys,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const r = await res.json();
      setNearbyActive(true);
      setNearbyPanelOpen(true);
      const npts = Array.isArray(r.points) ? r.points : [];
      const nrws = Array.isArray(r.rows) ? r.rows : [];
      setFullNearbyPoints(npts);
      setFullNearbyRows(nrws);
      setNearbyPoints(npts);
      setNearbyRows(nrws);
      setNeighborsAll(Array.isArray(r.neighbors_all) ? r.neighbors_all : []);
      setNearbyMeta({ max_km: r?.meta?.max_km ?? maxKm });
      const label = `${accountIds.length} selected site${accountIds.length !== 1 ? "s" : ""}`;
      setNearbyBaseName(label);
      setLastNearbyBaseName(label);
      setLastNearbyParams({ baseAccountIds: accountIds, maxKm });
      setSelectedAccountId(null);
    } catch (e: any) {
      console.error(e);
      setErr(e?.message || "Nearby multi search error");
    } finally {
      setBusy(false);
    }
  };

  // Helper: fetch nearby results for either single-site or multi-site mode
  const _fetchNearbyResult = async (maxKm: number, nearbyFilters: FilterGroup) => {
    if (lastNearbyParams?.baseAccountIds) {
      // Multi-site mode: call /nearby-multi with all base IDs
      const res = await fetch("/api/explorer/search/nearby-multi", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_account_ids: lastNearbyParams.baseAccountIds,
          max_km: maxKm,
          filters: nearbyFilters,
          columns: allColumnKeys,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.json();
    } else {
      // Single-site mode: call /within-drive-km
      return fetchNearbyDriveKm({
        baseAccountId: lastNearbyParams!.baseAccountId!,
        maxKm,
        filters: useSeparateNearbyFilters ? nearbyFilters : filters,
        columns: allColumnKeys,
      });
    }
  };

  // clearNearbyFilters: clears filtersNearby and re-runs nearby with empty filters.
  // Uses a local emptyFilters variable (not the state) to avoid stale closure issues
  // when called synchronously after setFiltersNearby.
  const clearNearbyFilters = async () => {
    if (!nearbyActive || !lastNearbyParams) return;
    const emptyFilters: FilterGroup = { logic: "AND", rules: [] };
    setFiltersNearby(emptyFilters);
    setBusy(true); setErr(null);
    try {
      const r = await _fetchNearbyResult(lastNearbyParams.maxKm, emptyFilters);
      const npts = Array.isArray(r.points) ? r.points : [];
      const nrws = Array.isArray(r.rows) ? r.rows : [];
      setFullNearbyPoints(npts);
      setFullNearbyRows(nrws);
      setNearbyPoints(npts);
      setNearbyRows(nrws);
      setNeighborsAll(Array.isArray(r.neighbors_all) ? r.neighbors_all : []);
      setNearbyMeta({ max_km: r?.meta?.max_km ?? lastNearbyParams.maxKm });
    } catch (e: any) {
      console.error(e);
      setErr(e?.message || "Nearby (clear filters) error");
    } finally {
      setBusy(false);
    }
  };

  const reapplyNearbyWithFilters = async () => {
    if (!nearbyActive || !lastNearbyParams) return;
    setBusy(true); setErr(null);
    try {
      const r = await _fetchNearbyResult(lastNearbyParams.maxKm, filtersNearby);
      const npts = Array.isArray(r.points) ? r.points : [];
      const nrws = Array.isArray(r.rows) ? r.rows : [];
      setFullNearbyPoints(npts);
      setFullNearbyRows(nrws);
      setNearbyPoints(npts);
      setNearbyRows(nrws);
      setNeighborsAll(Array.isArray(r.neighbors_all) ? r.neighbors_all : []);
      setNearbyMeta({ max_km: r?.meta?.max_km ?? lastNearbyParams.maxKm });
    } catch (e: any) {
      console.error(e);
      setErr(e?.message || "Nearby (apply filters) error");
    } finally {
      setBusy(false);
    }
  };

  const reapplyNearbyWithKm = async (nextKm: number) => {
    if (!nearbyActive || !lastNearbyParams) return;
    setBusy(true); setErr(null);
    try {
      const r = await _fetchNearbyResult(nextKm, filtersNearby);
      const npts = Array.isArray(r.points) ? r.points : [];
      const nrws = Array.isArray(r.rows) ? r.rows : [];
      setFullNearbyPoints(npts);
      setFullNearbyRows(nrws);
      setNearbyPoints(npts);
      setNearbyRows(nrws);
      setNeighborsAll(Array.isArray(r.neighbors_all) ? r.neighbors_all : []);
      setNearbyMeta({ max_km: r?.meta?.max_km ?? nextKm });
      setLastNearbyParams({ ...lastNearbyParams, maxKm: nextKm });
    } catch (e: any) {
      console.error(e);
      setErr(e?.message || "Nearby (apply km) error");
    } finally {
      setBusy(false);
    }
  };

  const clearNearby = useCallback(() => {
    setNearbyActive(false);
    setNearbyPanelOpen(false);
    setNearbyPoints([]); setNearbyRows([]); setNeighborsAll([]); setNearbyMeta(null);
    setSelectedAccountId(null);
    setLastNearbyParams(null);
    setSelectedRowIds(new Set());
  }, []);

  const showNearbyPanel = () => setNearbyPanelOpen(true);
  const hideNearbyPanel = () => setNearbyPanelOpen(false);

  // ---------- Column defs ----------
  const fixedDefs = useMemo<ColumnDef<ExplorerRow>[]>(() => ([
    { id: "country", header: "Country",
      accessorFn: (r) => (r.country === null || r.country === undefined || String(r.country).trim()==="" ? undefined : r.country),
      enableSorting: true, enableColumnFilter: true,
      filterFn: (row, columnId, filterValue) => {
        const raw = String(row.getValue(columnId) ?? "");
        const name = displayCountry(raw);
        const needle = String(filterValue).toLowerCase();
        return raw.toLowerCase().includes(needle) || name.toLowerCase().includes(needle);
      },
      sortUndefined: 'last',
      sortingFn: 'alphanumeric',
      cell: ({ getValue }) => {
        const raw = String(getValue() ?? "");
        if (!raw) return "—";
        const name = displayCountry(raw);
        return name !== raw ? <span title={raw}>{name}</span> : <>{name}</>;
      },
    },
    { id: "city", header: "City",
      accessorFn: (r) => (r.city === null || r.city === undefined || String(r.city).trim()==="" ? undefined : r.city),
      enableSorting: true, enableColumnFilter: true, filterFn: containsCI,
      sortUndefined: 'last',
      sortingFn: 'alphanumeric',
    },
    // NUEVO: Opportunity type (concatenado del backend)
    // { id: "opportunity_types", header: "Opportunity type",
    //   accessorFn: (r) => (r as any)?.opportunity_types ?? "",
    //   enableSorting: true, enableColumnFilter: true, filterFn: containsCI,
    //   cell: ({ getValue }) => {
    //     const v = String(getValue() ?? "");
    //     if (!v) return "—";
    //     // Pequeña “píldora” visual con múltiples tipos separados por coma
    //     return (
    //       <div className="flex flex-wrap gap-1">
    //         {v.split(",").map(s => s.trim()).filter(Boolean).map((t) => {
    //           const tl = t.toLowerCase();
    //           const pill =
    //             tl === "profiling" ? "bg-blue-100 text-blue-700 border-blue-200" :
    //             tl === "qualification" ? "bg-emerald-100 text-emerald-700 border-emerald-200" :
    //             "bg-gray-100 text-gray-700 border-gray-200";
    //           return (
    //             <span key={t} className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${pill}`}>
    //               {t}
    //             </span>
    //           );
    //         })}
    //       </div>
    //     );
    //   },
    // },
  ]), []);

  const dynamicDefs = useMemo<ColumnDef<ExplorerRow>[]>(() =>
    (Array.isArray(visibleColumns) ? visibleColumns : []).map((k) => {
      // Dentro de dynamicDefs, en el caso especial de "sf.Account.Name"
      if (k === ACCOUNT_NAME_COL_ID) {
        return {
          id: k,
          header: labelByKey.get(k) ?? prettyLabelFromKeyLabel(k, "Account Name"),
          accessorFn: (r: any) => readDataCell(r, k),
          enableSorting: true,
          enableColumnFilter: true,
          filterFn: containsCI,
          // También aquí, por coherencia
          sortUndefined: 'last',
          cell: ({ row }) => {
            const name  = getAccountNameFromRow(row.original as any);
            const accId = (row.original as ExplorerRow).account_id;

            return (
              <div className="flex items-center gap-2">
                <span className="font-medium">{name}</span>

                {/* Nearby (como ya tenías) */}
                <button
                  className="inline-flex items-center gap-1 rounded-full border border-blue-300 text-blue-700 px-2 py-0.5 text-xs hover:bg-blue-50 hover:border-blue-400"
                  title="Find nearby (driving km)"
                  onClick={(e) => { e.stopPropagation(); openNearbyFor(accId); }}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Zm0 9.5A2.5 2.5 0 1 1 12 6a2.5 2.5 0 0 1 0 5.5Z"
                          stroke="currentColor" strokeWidth="1.5"/>
                  </svg>
                  Nearby
                </button>

                {/* NUEVO: Details — abre el mismo modal de detalles */}
                <button
                  className="inline-flex items-center gap-1 rounded-full border border-gray-300 text-gray-700 px-2 py-0.5 text-xs hover:bg-gray-50 hover:border-gray-400"
                  title="Show details"
                  onClick={(e) => {
                    e.stopPropagation();
                    // Igual que en el mapa: abrimos el DetailsModal
                    setSelectedAccountId(accId);
                    setDetailsOpen(true);
                    // (equivale a onShowDetails(accId), pero evitamos problema de orden/hoisting)
                  }}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <path d="M12 8.5a1 1 0 1 0 0-2 1 1 0 0 0 0 2Zm1 7h-2v-6h2v6Z"
                          fill="currentColor"/>
                    <path d="M12 3a9 9 0 1 0 .001 18.001A9 9 0 0 0 12 3Z" stroke="currentColor" strokeWidth="1.4"/>
                  </svg>
                  Details
                </button>
              </div>
            );
          },
          sortingFn: (rowA, rowB) => {
            const a = getAccountNameFromRow(rowA.original as any).toLowerCase();
            const b = getAccountNameFromRow(rowB.original as any).toLowerCase();
            return a.localeCompare(b);
          },
        } as ColumnDef<ExplorerRow>;
      }
      return {
        id: k,
        header: labelByKey.get(k) ?? prettyLabelFromKeyLabel(k),
        accessorFn: (r: any) => readDataCell(r, k),
        enableSorting: true,
        enableColumnFilter: true,
        filterFn: containsCI,
        // Render booleans and empties nicely
        cell: ({ getValue }) => {
          const v = getValue<any>();
          if (typeof v === 'boolean') return v ? '✓' : '—';
          if (v === null || v === undefined) return '';
          const s = String(v).trim();
          if (s === '' || s === '__' || s === 'null' || s === 'undefined') return '';
          return s;
        },
        // Delega el orden a TanStack: alphanumeric + sortUndefined:'last' asegura que los vacíos
        // se queden SIEMPRE al final tanto en asc como en desc.
        sortingFn: 'alphanumeric',
        sortUndefined: 'last'
      } as ColumnDef<ExplorerRow>;
    })
  , [visibleColumns, labelByKey]);
  // Consume pending account-ids left by ChatView (sessionStorage) and apply filter immediately
  useEffect(() => {
    if (!bootDone || bootLoading) return;
    try {
      const raw = sessionStorage.getItem("cts:explorer:pending-account-ids");
      if (!raw) return;
      sessionStorage.removeItem("cts:explorer:pending-account-ids");
      const ids: string[] = Array.from(new Set(JSON.parse(raw))).map(String).filter(Boolean);
      if (!ids.length) return;
      const fk = getAccountIdFilterKey(fieldDefs as FieldDef[]);
      if (!fk) return;
      const fg = buildAccountIdsFilter(fk, ids);
      setFilters(fg);
      // run the search (with all columns)
      setTimeout(() => { void onSearch(); }, 0);
      // and highlight for convenience
      setHighlightIds(ids);
      firstHighlightRef.current = ids[0] || null;
    } catch {}
  }, [bootDone, bootLoading, fieldDefs]);

  const columns = useMemo<ColumnDef<ExplorerRow>[]>(() => [...fixedDefs, ...dynamicDefs], [fixedDefs, dynamicDefs]);

  // ColumnPicker groupBy
  const groupByField = (f: FieldDef): string | undefined => {
    const gf = f as LocalFieldDef;
    return gf.group || undefined;
  };

  // Filter fieldDefs to only show sf.* / extra.* columns that have at least one non-empty value
  // across the full result set. qual.* and site.* columns are always kept (they may be sparse
  // by design — qual data is only uploaded for some sites).
  const nonEmptyFieldDefs = useMemo(() => {
    const fds = Array.isArray(fieldDefs) ? (fieldDefs as LocalFieldDef[]) : [];
    const activeRows = nearbyActive ? fullNearbyRows : fullRows;
    if (!activeRows.length) return fds;
    const visibleSet = new Set(visibleColumns);
    return fds.filter((f) => {
      // Always keep currently-selected columns so they don't vanish from the picker
      if (visibleSet.has(f.key)) return true;
      // Always keep qual.* and site.* — sparsely populated by design
      if (f.key.startsWith("qual.") || f.key.startsWith("site.")) return true;
      // For sf.* and extra.* columns: hide if no row has a non-empty value
      return activeRows.some((r) => {
        const v = r.data?.[f.key];
        return v !== null && v !== undefined && !(typeof v === "string" && v.trim() === "");
      });
    });
  }, [fieldDefs, nearbyActive, fullNearbyRows, fullRows, visibleColumns]);

  // Tabla
  // 🔑 FUENTE ÚNICA DE VERDAD: los datasets "full*" de la primera carga
  const viewRows = useMemo(
    () => (nearbyActive ? fullNearbyRows : fullRows),
    [nearbyActive, fullNearbyRows, fullRows]
  );

  const table = useReactTable({
    data: Array.isArray(viewRows) ? viewRows : [],
    columns,
    state: {
      sorting,
      columnFilters,
      globalFilter,
      pagination: { pageIndex, pageSize },
      columnOrder,
    },
    onSortingChange: setSorting,
    onColumnFiltersChange: setColumnFilters,
    onGlobalFilterChange: setGlobalFilter,
    onPaginationChange: (updater) => {
      const next = typeof updater === "function" ? updater({ pageIndex, pageSize }) : updater;
      if (typeof next?.pageIndex === "number") setPageIndex(next.pageIndex);
      if (typeof next?.pageSize === "number") setPageSize(next.pageSize);
    },
    onColumnOrderChange: setColumnOrder,
    globalFilterFn: containsCI,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  // ==== Chart State (mover hooks DENTRO del componente) ====
  const [chartOpen, setChartOpen] = useState(false);
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [chartTitle, setChartTitle] = useState<string>("Chart");
  const [chartXKey, setChartXKey] = useState<string>("sf.Account.Name");
  const [chartYKeys, setChartYKeys] = useState<string[]>([]);
  // Stacked y legend max viven aquí, no en CustomView: la pestaña Personalizado
  // se desmonta al cambiar de pestaña y lo elegido se perdería en cada ida y vuelta.
  const [chartStacked, setChartStacked] = useState<boolean>(true);
  const [chartLegendMax, setChartLegendMax] = useState<number>(DEFAULT_LEGEND_MAX);
  const [chartYCandidates, setChartYCandidates] = useState<string[]>([]);
  const [chartXCandidates, setChartXCandidates] = useState<string[]>([]);

  // Detecta si una columna parece numérica (mirando filas visibles)
  const isNumericColumn = useCallback((key: string, sampleRows: ExplorerRow[]) => {
    let seen = 0, numeric = 0;
    for (const r of sampleRows.slice(0, 50)) {
      const v = readDataCell(r as any, key);
      if (v === null || v === undefined || v === "") continue;
      seen++;
      const n = Number(v);
      if (Number.isFinite(n)) numeric++;
    }
    return seen > 0 && numeric / seen >= 0.8;
  }, []);

  // Construye el dataset plano para Recharts
  const buildChartDataset = useCallback((
    rowsIn: ExplorerRow[],
    xKey: string,
    yKeys: string[]
  ) => {
    // If X is 'country' or 'city', aggregate values by that dimension (sum per group)
    const wantGroup = (xKey === 'country' || xKey === 'city');
    if (wantGroup) {
      const buckets = new Map<string, Record<string, any>>();
      for (const r of rowsIn) {
        const key = String(readDataCell(r as any, xKey) ?? '').trim() || '(empty)';
        const bucket = buckets.get(key) || { [xKey]: key, __count__: 0 };
        bucket.__count__ = (bucket.__count__ || 0) + 1;
        for (const y of yKeys) {
          if (y === '__count__') continue;
          const raw = readDataCell(r as any, y);
          const n = Number(String(raw ?? '').replace(/,/g, ''));
          bucket[y] = (Number.isFinite(n) ? (bucket[y] || 0) + n : (bucket[y] || 0));
        }
        buckets.set(key, bucket);
      }
      return Array.from(buckets.values()).sort((a, b) => (b.__count__ ?? 0) - (a.__count__ ?? 0));
    }
    // Default: row-wise dataset
    const arr: Array<Record<string, any>> = [];
    for (const r of rowsIn) {
      const row: Record<string, any> = {};
      row[xKey] = readDataCell(r as any, xKey) ?? "";
      row.__count__ = 1;
      for (const y of yKeys) {
        if (y === '__count__') continue;
        const raw = readDataCell(r as any, y);
        const n = Number(String(raw ?? '').replace(/,/g, ''));
        row[y] = Number.isFinite(n) ? n : null;
      }
      arr.push(row);
    }
    return arr;
  }, []);

  // chartData is derived (not stored) — auto-updates whenever rows or axis config changes
  const chartData = useMemo(() => {
    if (!chartOpen || !chartYKeys.length) return [];
    const currentRows = nearbyActive ? fullNearbyRows : fullRows;
    if (!currentRows?.length) return [];
    return buildChartDataset(currentRows, chartXKey, chartYKeys);
  }, [chartOpen, nearbyActive, fullNearbyRows, fullRows, chartXKey, chartYKeys, buildChartDataset]);

  // Abre el modal con defaults razonables
  const openChartWizard = useCallback(() => {
    const currentRows = nearbyActive ? fullNearbyRows : fullRows;
    if (!currentRows?.length) {
      alert("No hay datos para graficar.");
      return;
    }

    // X por defecto: Account Name si existe, si no country/city
    const defaultX =
      (visibleColumns.includes("sf.Account.Name") && "sf.Account.Name") ||
      (table.getAllLeafColumns().some(c => c.id === "country") && "country") ||
      (table.getAllLeafColumns().some(c => c.id === "city") && "city") ||
      visibleColumns[0];

    // Sugerir Y numéricas a partir de columnas visibles
    const numericKeys = visibleColumns.filter(k => isNumericColumn(k, currentRows));
    // Always offer a "Count" virtual Y key (counts rows per X group)
    const yCands = ["__count__", ...numericKeys];
    const suggestedY = numericKeys.slice(0, 3); // hasta 3 series por defecto

    // candidatos
    const xCands = [
      ...(visibleColumns.includes("sf.Account.Name") ? ["sf.Account.Name"] : []),
      ...["country","city"].filter(k => table.getAllLeafColumns().some(c => c.id === k)),
      ...visibleColumns.filter(k => k !== "sf.Account.Name")
    ].filter((v, i, a) => a.indexOf(v) === i);

    setChartXCandidates(xCands);
    setChartYCandidates(yCands);

    setChartXKey(defaultX);
    // Default to Count if no numeric columns, otherwise use the numeric ones
    setChartYKeys(suggestedY.length ? suggestedY : ["__count__"]);
    setChartType("bar");
    setChartStacked(true);
    setChartTitle("Explorer Chart");
    setChartOpen(true); // chartData recomputes automatically via useMemo
  }, [nearbyActive, fullNearbyRows, fullRows, visibleColumns, table, isNumericColumn, buildChartDataset]);

  // ====== Export TSV (toda la tabla filtrada) ======
  const sanitizeTSV = (val: any): string => {
    if (val === null || val === undefined) return "";
    if (typeof val === "object") {
      try {
        return JSON.stringify(val).replace(/\t/g, " ").replace(/\r?\n/g, " ");
      } catch {
        return String(val).replace(/\t/g, " ").replace(/\r?\n/g, " ");
      }
    }
    return String(val).replace(/\t/g, " ").replace(/\r?\n/g, " ");
  };

  const downloadFilteredTSV = () => {
    // Columnas visibles y en el orden actual de la tabla
    const visibleCols = table.getAllLeafColumns().filter(c => c.getIsVisible());

    const headerCells = visibleCols.map((c) => {
      const rawHeader =
        typeof c.columnDef.header === "string"
          ? c.columnDef.header
          : (labelByKey.get(c.id) ??
             (c.id === "country" ? "Country" :
              c.id === "city"    ? "City"    :
              c.id.replace(/^sf\./, "")));
      return sanitizeTSV(rawHeader);
    });
    const headerLine = headerCells.join("\t");

    // Filas: todas las filtradas + ordenadas (sin paginación)
    const allRows = table.getSortedRowModel().rows; // ya respeta filtros y orden
    const dataLines = allRows.map(r => {
      const cells = visibleCols.map(c => sanitizeTSV(r.getValue<any>(c.id)));
      return cells.join("\t");
    });

    const txt = [headerLine, ...dataLines].join("\n");
    const blob = new Blob([txt], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);

    const a = document.createElement("a");
    a.href = url;
    a.download = `explorer_filtered_${new Date().toISOString().slice(0,19).replace(/[:T]/g,"-")}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // Orden inicial: Account.Name primero
  const ensureInitialOrder = () => {
    const base = ["sf.Account.Name", "country", "city", ...visibleColumns.filter(k => k !== "sf.Account.Name")];
    const present = new Set(table.getAllLeafColumns().map(c => c.id));
    const next: string[] = [];
    base.forEach(id => { if (present.has(id) && !next.includes(id)) next.push(id); });
    table.getAllLeafColumns().forEach(c => { if (!next.includes(c.id)) next.push(c.id); });
    return next;
  };

  useEffect(() => {
    const next = ensureInitialOrder();
    if (JSON.stringify(next) !== JSON.stringify(columnOrder)) setColumnOrder(next);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nearbyActive ? nearbyRows : rows, visibleColumns]);

  const LOCKED_COLUMNS = ["sf.Account.Name"];

  const applyVisibleColumns = (nextKeys: string[]) => {
    // Evita meter columnas que duplican fijas
    const filtered = nextKeys.filter(k => !EXCLUDED_VISIBLE_COLUMNS.has(k));
    // Account Name is always on — add it back if cleared
    const withLocked = LOCKED_COLUMNS.reduce(
      (acc, k) => (acc.includes(k) ? acc : [k, ...acc]),
      filtered
    );
    setVisibleColumns(withLocked);
    setColumnOrder((prev) => {
      const start = prev.length ? [...prev] : ensureInitialOrder();
      const keepFixed = new Set(["sf.Account.Name","country","city"]);
      const out = start.filter(id => keepFixed.has(id) || filtered.includes(id));
      filtered.forEach(k => { if (!out.includes(k)) out.push(k); });
      const i = out.indexOf("sf.Account.Name");
      if (i > 0) { out.splice(i, 1); out.unshift("sf.Account.Name"); }
      return out;
    });
  };

  // === NEW: listeners para eventos del Chat y legacy ===
  useEffect(() => {
    function onFilterByIds(e: Event) {
      const ev = e as CustomEvent<{ account_ids?: string[] }>;
      const ids = (ev.detail?.account_ids || []).map(String).filter(Boolean);
      if (!ids.length) return;
      const fk = getAccountIdFilterKey(fieldDefs as FieldDef[]);
      if (!fk) {
        console.warn("[Explorer] No account-id filter key available in fieldDefs");
        return;
      }
      const fg = buildAccountIdsFilter(fk, ids);
      setFilters(fg);
      // highlight for convenience
      setHighlightIds(ids);
      firstHighlightRef.current = ids[0] || null;
      setSelectedAccountId(ids[0] || null);
      // run search with all columns
      setTimeout(() => { void onSearch(); }, 0);
    }
    function onHighlight(e: Event) {
      const ev = e as CustomEvent<{ account_ids?: string[] }>;
      const ids = (ev.detail?.account_ids || []).map(String).filter(Boolean);
      if (!ids.length) return;
      setHighlightIds(ids);
      firstHighlightRef.current = ids[0];
      setSelectedAccountId(ids[0] || null);
      // Broadcast a generic event for MapView or other listeners
      window.dispatchEvent(new CustomEvent("cts:explorer:highlight", { detail: { account_ids: ids } }));
    }
    function onSetFilters(e: Event) {
      const ev = e as CustomEvent<{ filters?: FilterGroup }>;
      if (ev.detail?.filters) {
        const flat = filterGroupToFlat(ev.detail.filters);
        setFilters({
          logic: flat.logicExpr || defaultExpr(flat.rules.length),
          rules: flat.rules.map(r => ({ field: r.field, operator: r.operator, value: r.value })),
        });
        setTimeout(() => { void onSearch(); }, 0);
      }
    }
    function onColumnsAdd(e: Event) {
      const ev = e as CustomEvent<{ columns?: string[] }>;
      const cols = (ev.detail?.columns || []).map(String).filter(Boolean);
      if (!cols.length) return;
      const allowed = new Set(allColumnKeys);
      const next = [...visibleColumns];
      for (const k of cols) {
        if (allowed.has(k) && !next.includes(k)) next.push(k);
      }
      if (next.length !== visibleColumns.length) {
        applyVisibleColumns(next);
      }
    }
    function onExplorerSet(e: Event) {
      const ev = e as CustomEvent<{ filters?: FilterGroup }>;
      if (!ev.detail?.filters) return;
      const incoming = normalizeAccountIdCsvToOr(ev.detail.filters);
      const flat = filterGroupToFlat(incoming);
      setFilters({
        logic: flat.logicExpr || defaultExpr(flat.rules.length),
        rules: flat.rules.map(r => ({ field: r.field, operator: r.operator, value: r.value })),
      });
      // ejecuta la búsqueda con todas las columnas
      setTimeout(() => { void onSearch(); }, 0);
    }
    // Listen to both legacy ("cts:explorer:*") and chat-prefixed ("cts:chat:explorer:*") events
    window.addEventListener("cts:explorer:filter-ids", onFilterByIds as EventListener);
    window.addEventListener("cts:chat:explorer:filter-ids", onFilterByIds as EventListener);
    window.addEventListener("cts:chat:explorer:highlight", onHighlight as EventListener);
    window.addEventListener("cts:explorer:highlight", onHighlight as EventListener);
    window.addEventListener("cts:chat:explorer:set", onSetFilters as EventListener);
    window.addEventListener("cts:explorer:set", onSetFilters as EventListener);
    window.addEventListener("cts:chat:explorer:columns:add", onColumnsAdd as EventListener);
    window.addEventListener("cts:explorer:columns:add", onColumnsAdd as EventListener);
    window.addEventListener("cts:chat:explorer:set", onExplorerSet);
    return () => {
      window.removeEventListener("cts:explorer:filter-ids", onFilterByIds as EventListener);
      window.removeEventListener("cts:chat:explorer:filter-ids", onFilterByIds as EventListener);
      window.removeEventListener("cts:chat:explorer:highlight", onHighlight as EventListener);
      window.removeEventListener("cts:explorer:highlight", onHighlight as EventListener);
      window.removeEventListener("cts:chat:explorer:set", onSetFilters as EventListener);
      window.removeEventListener("cts:explorer:set", onSetFilters as EventListener);
      window.removeEventListener("cts:chat:explorer:columns:add", onColumnsAdd as EventListener);
      window.removeEventListener("cts:explorer:columns:add", onColumnsAdd as EventListener);
      window.removeEventListener("cts:chat:explorer:set", onExplorerSet);
    };
  }, [allColumnKeys, visibleColumns, applyVisibleColumns, onSearch, setFilters, fieldDefs]);


  // Apply account_id filter from URL or sessionStorage on initial load
  useEffect(() => {
    if (!bootDone || bootLoading) return;

    // 1) URL params (support several aliases)
    const url = new URL(window.location.href);
    const raw =
      url.searchParams.get("account_ids") ||
      url.searchParams.get("accountIds") ||
      url.searchParams.get("ids") ||
      url.searchParams.get("acc_ids") ||
      "";

    let ids: string[] = [];
    if (raw) {
      ids = raw.split(",").map(s => s.trim()).filter(Boolean);
    } else {
      // 2) Fallback: sessionStorage (ChatView can write here before navigating)
      try {
        const ss = sessionStorage.getItem("cts:explorer:pending-account-ids");
        if (ss) {
          const parsed = JSON.parse(ss);
          if (Array.isArray(parsed)) ids = parsed.map(String).filter(Boolean);
          sessionStorage.removeItem("cts:explorer:pending-account-ids");
        }
      } catch { /* noop */ }
    }

    if (!ids.length) return;

    const fk = getAccountIdFilterKey(fieldDefs as FieldDef[]);
    if (!fk) {
      console.warn("[Explorer] account_ids provided but no filter key available in fieldDefs");
      return;
    }
    const fg = buildAccountIdsFilter(fk, ids);
    setFilters(fg);
    setHighlightIds(ids);
    firstHighlightRef.current = ids[0] || null;

    // Ensure we're on page 0 and run the search (with all columns)
    setPageIndex(0);
    setTimeout(() => { void onSearch(); }, 0);
  }, [bootDone, bootLoading, fieldDefs]);

  const visiblePoints = nearbyActive ? fullNearbyPoints : fullPoints;

  // ⟵ NUEVO: acción para re-login preservando la ruta / tab actual
  const loginAgain = () => {
    const url = new URL(window.location.href);
    if (!url.searchParams.get("tab")) url.searchParams.set("tab", "explorer");
    const next = url.pathname + url.search;
    sfLoginRedirect(next || "/");
  };

  // ===== onShowDetails handler (NEW) =====
  const onShowDetails = (accountId: string) => {
    setSelectedAccountId(accountId);
    setDetailsOpen(true);
  };

  const selectedAccountName = selectedAccountId
    ? getAccountNameByIdSafe(
        selectedAccountId,
        nearbyActive ? fullNearbyRows : fullRows,
        nearbyActive ? fullNearbyPoints : fullPoints
      )
    : null;

  return (
    <div className="space-y-4">
      {bootLoading && <LoadingOverlay text="Loading initial data…" />}
      {!bootLoading && busy && <LoadingOverlay text="Searching…" />}

      {/* AVISO DE SESIÓN SALESFORCE */}
      {sfAuthOk === false && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
          <div className="flex items-center justify-between gap-3">
            <div>
              <strong>Salesforce session expired.</strong> Please login again to load data.
            </div>
            <button
              onClick={loginAgain}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-white hover:bg-blue-700"
            >
              Login Salesforce
            </button>
          </div>
        </div>
      )}

      {/* Header filtros */}
      <div data-testid="explorer-filter-panel" className="rounded-xl border bg-white shadow-sm">
        <div className="px-4 py-2 bg-gray-100 border-b flex items-center justify-between">
          <button
            data-testid="filter-panel-collapse-btn"
            className="font-semibold text-gray-800 flex items-center gap-2 hover:text-blue-700 focus:outline-none"
            onClick={() => setFilterCollapsed(v => !v)}
            title={filterCollapsed ? "Expand filters" : "Collapse filters"}
          >
            <svg
              className={`h-4 w-4 text-gray-500 transition-transform ${filterCollapsed ? "-rotate-90" : ""}`}
              viewBox="0 0 16 16" fill="none" aria-hidden
            >
              <path d="M4 6l4 4 4-4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            Filters
            {activeRuleCount > 0 && (
              <span className="inline-flex items-center justify-center rounded-full bg-blue-100 text-blue-700 text-xs font-bold px-2 py-0.5 min-w-[20px]">
                {activeRuleCount}
              </span>
            )}
          </button>
          <div className="flex gap-2 items-center">
            {nearbyActive && (
              <span className="hidden md:inline-flex items-center gap-2 mr-2 text-sm rounded-full border px-3 py-1 bg-emerald-50 text-emerald-700">
                Nearby ON · ≤ <b>{nearbyMeta?.max_km ?? "—"}</b> km
                <button className="rounded border px-2 py-0.5 text-xs hover:bg-white" onClick={() => setNearbyPanelOpen(v => !v)}>
                  Toggle panel
                </button>
                <button className="rounded border px-2 py-0.5 text-xs hover:bg-white" onClick={clearNearby}>
                  Close nearby
                </button>
              </span>
            )}
            {highlightIds.length > 0 && (
              <span className="hidden md:inline-flex items-center gap-2 mr-2 text-sm rounded-full border px-3 py-1 bg-amber-50 text-amber-800">
                Highlight: <b>{highlightIds.length}</b> site{highlightIds.length===1?"":"s"}
                <button
                  className="rounded border px-2 py-0.5 text-xs hover:bg-white"
                  onClick={clearHighlight}
                  title="Clear highlighted rows"
                >
                  Clear highlight
                </button>
              </span>
            )}
            <button
              onClick={() => {
                localStorage.removeItem(LS_KEYS.visibleColumns);
                localStorage.removeItem(LS_KEYS.sorting);
                localStorage.removeItem(LS_KEYS.columnFilters);
                localStorage.removeItem(LS_KEYS.globalFilter);
                localStorage.removeItem(LS_KEYS.pageSize);
                localStorage.removeItem(LS_KEYS.columnOrder);
                localStorage.removeItem(LS_KEYS.columnWidths);
                setVisibleColumns(DEFAULT_VISIBLE_COLUMNS);
                setSorting([{ id: "country", desc: false }]);
                setColumnFilters([]);
                setGlobalFilter("");
                setPageSize(10);
                setPageIndex(0);
                setColumnOrder([]);
                setColumnWidths({});
              }}
              className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50"
              disabled={busy || bootLoading}
              title="Reset table layout (columns, sorting, filters, page size)"
            >
              Reset layout
            </button>
            <button
              onClick={onReset}
              className="text-sm rounded-md border px-3 py-1.5 hover:bg-gray-50"
              disabled={busy || bootLoading}
              title="Clear filters and show all"
            >
              Reset data
            </button>
          </div>
        </div>

        <div className="p-4">
          {!filterCollapsed && (
            <FilterBuilder
              value={filters}
              onChange={setFilters}
              fields={Array.isArray(fieldDefs) ? (fieldDefs as LocalFieldDef[]).filter(f => !EXCLUDED_FILTER_FIELDS.has(f.key)) : []}
            />
          )}
          {/* Active filter chips — always visible, also serve as summary when collapsed */}
          {filters.rules.length > 0 && (
            <div className={`flex flex-wrap gap-1.5 mb-1 ${filterCollapsed ? "" : "mt-2"}`}>
              {/* Logic expression summary (shown when collapsed and expression is non-trivial) */}
              {filterCollapsed && filters.logic && filters.logic !== "AND" && filters.logic !== "OR" && (
                <span className="self-center text-xs text-gray-400 font-mono mr-1">
                  Logic: <code className="bg-blue-50 text-blue-700 border border-blue-100 rounded px-1">{filters.logic}</code>
                </span>
              )}
              {filters.rules.map((r, i) => {
                const isRule = r && typeof (r as any).field === "string";
                const rule = r as any;
                let chipLabel: string;
                if (isRule) {
                  const fieldLabel = labelByKey.get(rule.field) ?? (rule.field as string).split(".").pop() ?? rule.field;
                  const opLabel = OP_LABELS[rule.operator] ?? rule.operator;
                  const noValue = rule.operator === "is_empty" || rule.operator === "is_not_empty";
                  let valStr = "";
                  if (!noValue) {
                    if (Array.isArray(rule.value)) valStr = (rule.value as any[]).filter(Boolean).join(" – ");
                    else valStr = String(rule.value ?? "");
                  }
                  chipLabel = noValue
                    ? `${fieldLabel} ${opLabel}`
                    : `${fieldLabel} ${opLabel} ${valStr}`.trim();
                } else {
                  const sub = r as any;
                  const n = (sub.rules ?? []).length;
                  chipLabel = `Group (${n} rule${n === 1 ? "" : "s"})`;
                }
                return (
                  <span
                    key={i}
                    className="inline-flex items-center gap-1 rounded-full bg-blue-50 border border-blue-200 text-blue-800 text-xs px-2.5 py-0.5"
                  >
                    <span className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-blue-200 text-blue-700 text-[10px] font-bold flex-shrink-0">{i + 1}</span>
                    <span className="max-w-[240px] truncate" title={chipLabel}>{chipLabel}</span>
                    <button
                      className="ml-0.5 rounded-full hover:bg-blue-200 p-0.5 text-blue-600 flex-shrink-0"
                      title="Remove filter"
                      onClick={() => removeRuleAtIndex(i)}
                    >
                      <svg className="h-3 w-3" viewBox="0 0 12 12" fill="none" aria-hidden>
                        <path d="M2 2l8 8M10 2L2 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                      </svg>
                    </button>
                  </span>
                );
              })}
            </div>
          )}
          <div className="flex flex-wrap gap-2 items-center">
            <button
              data-testid="explorer-search-btn"
              onClick={onSearch}
              className="mt-3 inline-flex items-center gap-2 rounded-md bg-blue-600 text-white px-4 py-2 hover:bg-blue-700 disabled:opacity-60"
              disabled={busy || bootLoading}
            >
              {busy ? (
                <>
                  <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none" aria-hidden>
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  Searching…
                </>
              ) : ("Search")}
            </button>
            {nearbyActive && (
              <div className="md:hidden inline-flex items-center gap-2 text-sm mt-3">
                <span>Nearby ≤ <b>{nearbyMeta?.max_km ?? "—"}</b> km</span>
                <button className="rounded-md border px-3 py-1 hover:bg-gray-50" onClick={() => setNearbyPanelOpen(v => !v)}>
                  Panel
                </button>
                <button className="rounded-md border px-3 py-1 hover:bg-gray-50" onClick={clearNearby}>
                  Close
                </button>
              </div>
            )}
            {err ? <div className="text-red-600 mt-3 text-sm">{err}</div> : null}
          </div>
        </div>
      </div>

      
      {/* ===== AI Answer panel (texto + tabla opcional) ===== */}
      {aiAnswer && (
        <div className="rounded-xl border bg-white shadow-sm relative">
          <div className="px-4 py-2 border-b flex items-center justify-between">
            <div className="font-medium text-gray-800">AI Answer</div>
            <div className="flex items-center gap-2">
              <button
                className="rounded-md border px-2 py-1 text-xs hover:bg-gray-50"
                onClick={() => { setAiAnswer(null); setAiTable(null); }}
                title="Clear answer"
              >
                Clear
              </button>
            </div>
          </div>
          <div className="p-4">
            {/* Renderiza el HTML ya saneado que devuelve el backend (evita ver tags crudos como <table>) */}
            <div
              className="prose prose-sm max-w-none text-gray-900"
              dangerouslySetInnerHTML={{ __html: aiAnswer }}
            />
            {/* Tabla opcional que envía el agente */}
            {aiTable && aiTable.rows?.length > 0 && (
              <div className="mt-4 overflow-x-auto">
                <table className="min-w-[640px] w-full text-sm border rounded-md overflow-hidden">
                  <thead className="bg-gray-50">
                    <tr>
                      {aiTable.columns.map((c) => (
                        <th key={c.key} className="px-3 py-2 text-left font-semibold text-gray-700 border-b">
                          {c.label || c.key}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {aiTable.rows.map((r, idx) => (
                      <tr key={idx} className="border-b last:border-0">
                        {aiTable.columns.map((c) => (
                          <td key={c.key} className="px-3 py-2 whitespace-nowrap">
                            {r[c.key] ?? ""}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}


      {/* Column Picker */}
      <div className="rounded-xl border bg-white shadow-sm">
        <div className="px-4 py-2 bg-blue-50 border-b">
          <span className="text-sm font-medium text-blue-700">Columns</span>
        </div>
        <ColumnPicker
          fields={nonEmptyFieldDefs}
          value={visibleColumns}
          onChange={(next) => {
            const allowed = new Set((fieldDefs as any[]).map((f: any) => f.key));
            // Limpia: solo claves válidas y sin las que duplican columnas fijas (Country/City)
            const cleaned = next
              .filter((k) => allowed.has(k))
              .filter((k) => !EXCLUDED_VISIBLE_COLUMNS.has(k));
            applyVisibleColumns(cleaned);
          }}
          presets={useMemo(() => {
            const base: { id: string; label: string; keys: string[] }[] = [
              { id: "basic",   label: "Basic",                    keys: PRESETS.Basic },
              { id: "opp",     label: "Opportunity Core",         keys: PRESETS.OpportunityCore },
              { id: "acc",     label: "Account Basics",           keys: PRESETS.AccountBasics },
              { id: "qual_sf", label: "Qualification (SF dates)", keys: PRESETS.QualificationDates },
              { id: "clin",    label: "Clinical Details",         keys: PRESETS.ClinicalCapacity },
              { id: "audit",   label: "Audit/Timeline",           keys: PRESETS.NotesAndVerification },
            ];
            const qualKeys = (Array.isArray(fieldDefs) ? (fieldDefs as FieldDef[]) : [])
              .filter(f => String(f.key).startsWith("qual."))
              .map(f => f.key);
            if (qualKeys.length) base.push({ id: "qual_dyn", label: "Qualification (qual.* from Excel)", keys: qualKeys });
            return base;
          }, [fieldDefs])}
          defaultOpen={false}
showPresets={false}
          sortKeys={(allKeys, presets) => orderKeysByPresets(allKeys, presets as any)}
          renderPresetHeader={(preset) => {
            if (preset.id === "qual_dyn") {
              return (
                <div className="px-3 py-1.5 bg-emerald-50 text-emerald-700 font-medium text-sm rounded-t">
                  {preset.label}
                </div>
              );
            }
            return (
              <div className="px-3 py-1.5 bg-gray-50 text-gray-700 font-medium text-sm rounded-t">
                {preset.label}
              </div>
            );
          }}
          groupBy={groupByField}
          lockedKeys={LOCKED_COLUMNS}
        />
      </div>

      {/* Mapa */}
      {bootDone ? (
        <MapView
          {...({} as any)}
          points={Array.isArray(visiblePoints) ? visiblePoints : []}
          neighborsAll={neighborsAll as any}
          selectedAccountId={selectedAccountId as any}
          onSelectAccount={(id: string | null) => setSelectedAccountId(id)}
          onNearbyRequest={(id: string) => openNearbyFor(id)}
          onShowDetails={(id: string) => onShowDetails(id)}  // sigue abriendo modal
          highlightedAccountIds={highlightIds}
        />
      ) : (
        <div className="p-3 text-sm text-gray-600">Map not ready.</div>
      )}

      {/* Barra superior tabla */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2 mt-2">
        <div data-testid="explorer-results-count" className="text-sm text-gray-600">
          {table.getFilteredRowModel().rows.length.toLocaleString()} result
          {table.getFilteredRowModel().rows.length === 1 ? "" : "s"}
          {sorting[0] ? (
            <> · sorted by <strong>{labelByKey.get(sorting[0].id) ?? (sorting[0].id === 'country' ? 'Country' : sorting[0].id === 'city' ? 'City' : prettyLabelFromKeyLabel(String(sorting[0].id)))}</strong> ({sorting[0].desc ? "desc" : "asc"})</>
          ) : null}
          {nearbyActive ? <> · Nearby ≤ <strong>{nearbyMeta?.max_km ?? "—"}</strong> km</> : null}
        </div>
        <div className="flex items-center gap-2">
          <input
            data-testid="explorer-global-search"
            className="border rounded-md px-3 py-1.5 text-sm w-64"
            placeholder="Search in all columns…"
            value={globalFilter ?? ""}
            onChange={(e) => setGlobalFilter(e.target.value)}
          />
          <label className="text-sm text-gray-600">Rows per page</label>
          <select
            data-testid="explorer-page-size"
            className="border rounded-md px-2 py-1 text-sm"
            value={pageSize}
            onChange={(e) => { setPageSize(Number(e.target.value)); setPageIndex(0); }}
          >
            {[10, 20, 50, 100].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>

          {selectedRowIds.size > 0 && (
            <button
              data-testid="explorer-nearby-multi-btn"
              onClick={() => setNearbyMultiModalOpen(true)}
              className="inline-flex items-center gap-1 text-sm bg-violet-600 text-white rounded-md px-3 py-1.5 hover:bg-violet-700"
              title={`Find INNODIA sites within driving distance of the ${selectedRowIds.size} selected row${selectedRowIds.size !== 1 ? "s" : ""}`}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Z" stroke="currentColor" strokeWidth="1.8"/>
                <circle cx="12" cy="9" r="2.5" fill="currentColor"/>
              </svg>
              Nearby ({selectedRowIds.size} selected)
            </button>
          )}
          <button
            data-testid="explorer-btn-export-tsv"
            className="ml-2 rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
            onClick={downloadFilteredTSV}
            disabled={table.getFilteredRowModel().rows.length === 0}
            title="Download all filtered rows as tab-delimited .txt"
          >
            Export filtered (TSV)
          </button>
          {/* Chart button */}
          <button
            data-testid="explorer-btn-chart"
            className="rounded-md border border-blue-300 bg-blue-50 px-3 py-1.5 text-sm text-blue-700 hover:bg-blue-100 disabled:opacity-40 inline-flex items-center gap-1"
            onClick={openChartWizard}
            disabled={(nearbyActive ? fullNearbyRows : fullRows).length === 0}
            title="Visualize current filtered rows as chart — updates automatically when filters change"
          >
            📊 Chart
            {(nearbyActive ? fullNearbyRows : fullRows).length > 0 && (
              <span className="rounded-full bg-blue-200 px-1.5 py-0.5 text-xs font-medium">
                {(nearbyActive ? fullNearbyRows : fullRows).length}
              </span>
            )}
          </button>
          {/* Ask Moby button */}
          <button
            className="rounded-md border px-2.5 py-1.5 hover:bg-indigo-50 flex items-center gap-1.5"
            onClick={() => {
              const activeRows = nearbyActive ? fullNearbyRows : fullRows;
              const labelMap: Record<string, string> = {};
              (fieldDefs as any[]).forEach((f: any) => { if (f?.key) labelMap[f.key] = f.label || f.key; });
              const contextTable = {
                columns: visibleColumns.map(k => ({ key: k, label: labelMap[k] || k })),
                rows: activeRows.slice(0, 500),
              };
              try {
                sessionStorage.setItem("moby_last_table_v1", JSON.stringify(contextTable));
                sessionStorage.setItem("moby_last_filters_v1", JSON.stringify(filters));
              } catch {}
              window.dispatchEvent(new CustomEvent("cts:explorer:ask-ai", {
                detail: { table: contextTable, filters, rowCount: activeRows.length },
              }));
              window.history.pushState({}, "", `${window.location.origin}/chat`);
              window.dispatchEvent(new Event("popstate"));
            }}
            title="Ask Moby about the current Explorer data"
          >
            <img src={Moby} width={18} height={18} alt="Moby" />
          </button>
        </div>
      </div>

      {/* Tabla */}
      <div ref={tableContainerRef} data-testid="explorer-results-table" className="overflow-auto rounded-xl border border-gray-200 bg-white shadow-sm">
        {/* Selection banner — shown when rows are selected */}
        {selectedRowIds.size > 0 && (
          <div className="flex items-center gap-3 px-3 py-1.5 bg-violet-50 border-b border-violet-100 text-xs text-violet-800">
            <span className="font-medium">{selectedRowIds.size} row{selectedRowIds.size !== 1 ? "s" : ""} selected</span>
            {selectedRowIds.size < viewRows.length && (
              <button
                className="underline hover:no-underline"
                onClick={() => setSelectedRowIds(new Set(viewRows.map((r) => r.account_id).filter(Boolean)))}
              >
                Select all {viewRows.length} results
              </button>
            )}
            {selectedRowIds.size === viewRows.length && viewRows.length > 0 && (
              <span className="text-violet-600">All {viewRows.length} results selected</span>
            )}
            <button
              className="ml-auto underline hover:no-underline"
              onClick={() => setSelectedRowIds(new Set())}
            >
              Clear selection
            </button>
          </div>
        )}
        <table className="w-max min-w-full text-sm">
          <thead className="bg-gray-50">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {/* Checkbox select-all */}
                <th className="px-2 py-2 w-8 align-bottom">
                  <input
                    type="checkbox"
                    title="Select all visible rows"
                    className="accent-violet-600 cursor-pointer"
                    checked={table.getRowModel().rows.length > 0 && table.getRowModel().rows.every(r => selectedRowIds.has((r.original as ExplorerRow).account_id))}
                    onChange={(e) => {
                      const ids = table.getRowModel().rows.map(r => (r.original as ExplorerRow).account_id).filter(Boolean);
                      setSelectedRowIds(prev => {
                        const next = new Set(prev);
                        if (e.target.checked) ids.forEach(id => next.add(id));
                        else ids.forEach(id => next.delete(id));
                        return next;
                      });
                    }}
                  />
                </th>
                {hg.headers.map((header) => {
                  const canSort = header.column.getCanSort();
                  const dir = header.column.getIsSorted() as false | "asc" | "desc";
                  const isAccountName = header.column.id === ACCOUNT_NAME_COL_ID;
                  const fixedWidth = columnWidths[header.column.id];
                  return (
                    <th
                      key={header.id}
                      data-testid="explorer-table-header"
                      draggable
                      onDragStart={(e) => {
                        // Un gesto que empieza en el tirador es un resize, no un reorder.
                        if (isResizingRef.current) { e.preventDefault(); return; }
                        e.dataTransfer.setData("text/plain", header.column.id);
                        e.dataTransfer.effectAllowed = "move";
                      }}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        e.preventDefault();
                        const from = e.dataTransfer.getData("text/plain");
                        const to = header.column.id;
                        if (!from || from === to) return;
                        setColumnOrder((prev) => {
                          const base = prev.length ? [...prev] : table.getAllLeafColumns().map(c=>c.id);
                          const fi = base.indexOf(from), ti = base.indexOf(to);
                          if (fi === -1 || ti === -1) return base;
                          base.splice(fi, 1);
                          base.splice(ti, 0, from);
                          return base;
                        });
                      }}
                      className={`relative px-3 py-2 text-left font-semibold text-gray-700 select-none align-bottom ${fixedWidth || isAccountName ? "" : "max-w-[180px]"}`}
                      style={fixedWidth ? { width: fixedWidth, minWidth: fixedWidth, maxWidth: fixedWidth } : undefined}
                      title={String(header.column.columnDef.header ?? "")}
                    >
                      <button
                        className={`inline-flex items-center gap-1 w-full min-w-0 ${canSort ? "cursor-pointer" : "cursor-default"}`}
                        onClick={header.column.getToggleSortingHandler()}
                        disabled={!canSort}
                        title={canSort ? `${String(header.column.columnDef.header ?? "")} — click to sort` : String(header.column.columnDef.header ?? "")}
                      >
                        <span className="truncate">{flexRender(header.column.columnDef.header, header.getContext())}</span>
                        {dir ? <span className="flex-shrink-0">{dir === "asc" ? " ▲" : " ▼"}</span> : ""}
                      </button>
                      {header.column.getCanFilter() ? (
                        <div className="mt-1">
                          <input
                            className="border rounded-md px-2 py-1 text-xs w-full min-w-0"
                            placeholder="filter…"
                            value={(header.column.getFilterValue() as string) ?? ""}
                            onChange={(e) => header.column.setFilterValue(e.target.value)}
                          />
                        </div>
                      ) : null}
                      {/* Tirador de redimensionado. draggable=false para que arrastrarlo
                          no dispare el drag-to-reorder de la cabecera. */}
                      <div
                        role="separator"
                        aria-orientation="vertical"
                        aria-label={`Resize ${String(header.column.columnDef.header ?? header.column.id)} column`}
                        draggable={false}
                        onDragStart={(e) => { e.preventDefault(); e.stopPropagation(); }}
                        onMouseDown={(e) => startColumnResize(header.column.id, e)}
                        onDoubleClick={() => resetColumnWidth(header.column.id)}
                        onClick={(e) => e.stopPropagation()}
                        title="Drag to resize — double-click to auto-fit"
                        className="absolute top-0 right-0 h-full w-1.5 cursor-col-resize hover:bg-violet-400/60 active:bg-violet-500"
                      />
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td className="px-3 py-6 text-center text-gray-500" colSpan={table.getAllLeafColumns().length + 1}>
                  {busy || bootLoading ? "Loading…" : "No results"}
                </td>
              </tr>
            ) : (
              table.getRowModel().rows.map((row) => {
                const accId = (row.original as ExplorerRow).account_id;
                const isChecked = selectedRowIds.has(accId);
                return (
                <tr
                  key={row.id}
                  data-testid="explorer-table-row"
                  data-account-id={accId}
                  className={[
                    "border-t hover:bg-gray-50",
                    isChecked ? "bg-violet-50/40" : "",
                    selectedAccountId && accId === selectedAccountId ? "bg-blue-50/40" : "",
                    // NEW: estilo de resaltado (coincide con eventos del chat)
                    highlightIds.includes(accId)
                      ? "bg-amber-50 ring-1 ring-amber-300"
                      : ""
                  ].join(" ")}
                  onClick={() => setSelectedAccountId(accId)}
                >
                  {/* Per-row checkbox */}
                  <td className="px-2 py-2 w-8" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      className="accent-violet-600 cursor-pointer"
                      checked={isChecked}
                      onChange={(e) => {
                        setSelectedRowIds(prev => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(accId);
                          else next.delete(accId);
                          return next;
                        });
                      }}
                    />
                  </td>
                  {row.getVisibleCells().map((cell) => {
                    const isAccountName = cell.column.id === ACCOUNT_NAME_COL_ID;
                    const fixedWidth = columnWidths[cell.column.id];
                    const raw = cell.getValue();
                    // El title se calcula para TODA columna, incluida Account Name:
                    // es la que más se recorta cuando el usuario estrecha su ancho,
                    // y por tanto la que más necesita el tooltip.
                    const strVal = raw == null ? "" : String(raw);
                    const content = flexRender(
                      cell.column.columnDef.cell ?? ((ctx) => String(ctx.getValue() ?? "")),
                      cell.getContext()
                    );
                    // Sin ancho fijado, Account Name se muestra entera (la tabla scrollea).
                    // Con ancho fijado manda el usuario: la celda recorta.
                    const clips = !isAccountName || Boolean(fixedWidth);
                    return (
                    <td key={cell.id} data-testid="explorer-table-cell"
                        className={`px-3 py-2 overflow-hidden ${isAccountName ? "whitespace-nowrap" : ""} ${fixedWidth || isAccountName ? "" : "max-w-[260px]"}`}
                        style={fixedWidth ? { width: fixedWidth, minWidth: fixedWidth, maxWidth: fixedWidth } : undefined}
                        title={strVal.length > 40 ? strVal : undefined}>
                      {clips ? <div className="truncate">{content}</div> : content}
                    </td>
                    );
                  })}
                </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Paginación */}
      <div data-testid="explorer-pagination" className="flex items-center justify-between mt-3">
        <div data-testid="explorer-page-info" className="text-sm text-gray-600">
          Showing{" "}
          <strong>
            {table.getRowModel().rows.length === 0
              ? 0
              : table.getState().pagination.pageIndex * table.getState().pagination.pageSize + 1}
          </strong>
          –
          <strong>
            {Math.min(
              (table.getState().pagination.pageIndex + 1) * table.getState().pagination.pageSize,
              table.getFilteredRowModel().rows.length
            )}
          </strong>{" "}
          of <strong>{table.getFilteredRowModel().rows.length}</strong>
        </div>
        <div className="flex items-center gap-2">
          <button data-testid="explorer-page-first" className="px-3 py-1.5 border rounded-md disabled:opacity-50"
            onClick={() => table.setPageIndex(0)} disabled={!table.getCanPreviousPage()}>« First</button>
          <button data-testid="explorer-page-prev" className="px-3 py-1.5 border rounded-md disabled:opacity-50"
            onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()}>‹ Prev</button>
          <span className="text-sm text-gray-700">
            Page <strong>{table.getState().pagination.pageIndex + 1}</strong> / {table.getPageCount()}
          </span>
          <button data-testid="explorer-page-next" className="px-3 py-1.5 border rounded-md disabled:opacity-50"
            onClick={() => table.nextPage()} disabled={!table.getCanNextPage()}>Next ›</button>
          <button data-testid="explorer-page-last" className="px-3 py-1.5 border rounded-md disabled:opacity-50"
            onClick={() => table.setPageIndex(table.getPageCount() - 1)} disabled={!table.getCanNextPage()}>Last »</button>
        </div>
      </div>

      {/* Botón flotante Nearby */}
      {nearbyActive && !nearbyPanelOpen && (
        <button
          className="fixed bottom-5 right-5 z-[9050] inline-flex items-center gap-2 rounded-full shadow-xl border border-blue-600 bg-blue-600 text-white px-5 py-3 text-sm font-medium hover:opacity-90"
          onClick={showNearbyPanel}
          title="Show Nearby panel"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7Z" stroke="currentColor" strokeWidth="1.8" />
            <circle cx="12" cy="9" r="2.5" fill="currentColor"/>
          </svg>
          Nearby panel
        </button>
      )}

      {/* Modal Nearby Multi */}
      <NearbyMultiModal
        open={nearbyMultiModalOpen}
        count={selectedRowIds.size}
        onClose={() => setNearbyMultiModalOpen(false)}
        onConfirm={applyNearbyMulti}
      />

      {/* Modal Nearby */}
      <NearbyModal
        open={nearbyModalOpen}
        baseAccountId={nearbyBaseAccount || ""}
        baseAccountName={nearbyBaseName || ""}
        onClose={() => setNearbyModalOpen(false)}
        onConfirm={applyNearby}
      />

      {/* Drawer Nearby */}
      <NearbyDrawer
        open={nearbyActive && nearbyPanelOpen}
        baseAccountId={lastNearbyParams?.baseAccountId}
        baseAccountName={lastNearbyBaseName}
        maxKm={nearbyMeta?.max_km ?? lastNearbyParams?.maxKm ?? null}
        useSeparateNearbyFilters={useSeparateNearbyFilters}
        filtersNearby={filtersNearby}
        fields={Array.isArray(fieldDefs) ? (fieldDefs as LocalFieldDef[]) : []}
        layout={nearbyLayout}
        sideWidth={nearbySideWidth}
        onSetLayout={setNearbyLayout}
        onSetSideWidth={setNearbySideWidth}
        onHidePanel={hideNearbyPanel}
        onExitNearby={clearNearby}
        onToggleSeparate={setUseSeparateNearbyFilters}
        onChangeFiltersNearby={setFiltersNearby}
        onApplyFilters={reapplyNearbyWithFilters}
        onClearFilters={clearNearbyFilters}
        onApplyKm={reapplyNearbyWithKm}
        onClear={clearNearby}
      />

      {/* Details Modal (NEW: con datos reales de backend) */}
      <SiteDetailsModal
        open={detailsOpen}
        onClose={() => setDetailsOpen(false)}
        accountId={selectedAccountId}
        accountName={selectedAccountName || undefined}
      />

      <ChartModal
        open={chartOpen}
        onClose={() => setChartOpen(false)}
        title={chartTitle}
        onChangeTitle={(t) => setChartTitle(t)}
        rows={nearbyActive ? fullNearbyRows : fullRows}
        custom={
          <CustomView
            title={chartTitle}
            data={chartData}
            xKey={chartXKey}
            yKeys={chartYKeys}
            type={chartType}
            xCandidates={chartXCandidates}
            yCandidates={chartYCandidates}
            labelByKey={labelByKey}
            stacked={chartStacked}
            legendMax={chartLegendMax}
            onChangeLegendMax={(n) => setChartLegendMax(n)}
            onChangeType={(t) => setChartType(t)}
            onChangeXKey={(x) => setChartXKey(x)}
            onChangeStacked={(v) => setChartStacked(v)}
            onToggleYKey={(y) => {
              setChartYKeys(prev =>
                prev.includes(y) ? prev.filter(k => k !== y) : [...prev, y]
              );
            }}
          />
        }
      />
    </div>
  );
}
