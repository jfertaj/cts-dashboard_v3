// src/lib/api.ts

// ------------------------------ Base URL -----------------------------------
const API_BASE =
  ((import.meta as any).env?.VITE_API_BASE as string | undefined)?.replace(/\/+$/, "") || "";

// ===== DEBUG FLAG UNICO PARA ESTE BUILD =====
console.log("[CTS] api.ts LOADED — BUILD MARK:", new Date().toISOString());

/* ----------------------------- infra: utils -------------------------------- */
const now = () => Date.now();
const sleep = (ms: number) => new Promise(res => setTimeout(res, ms));
const stableBodyKey = (init?: RequestInit) => {
  const b = init?.body;
  if (typeof b === "string") return b;
  if (b == null) return "";
  return "__opaque__";
};

// Peticiones en vuelo para coalescing (method+url+bodyKey)
const inflight = new Map<string, Promise<Response>>();
const inflightKey = (url: string, init?: RequestInit) =>
  `${(init?.method || "GET").toUpperCase()} ${url} :: ${stableBodyKey(init)}`;

// Timeout por defecto (ms)
const DEFAULT_TIMEOUT = 60_000;


/* ----------------------------- small helpers ----------------------------- */
function asArray<T>(x: any): T[] { return Array.isArray(x) ? (x as T[]) : []; }
function pick<T>(obj: any, key: string, fallback: T): T {
  return obj && Object.prototype.hasOwnProperty.call(obj, key) ? (obj[key] as T) : fallback;
}

/* Always send cookies to the backend (session-based auth) */
async function api<T>(path: string, init?: RequestInit & { timeoutMs?: number; retries?: number }): Promise<T> {
  const url = `${API_BASE}${path}`;
  const { timeoutMs = DEFAULT_TIMEOUT, retries = 0, signal, ...rest } = (init || {}) as any;

  const ac = new AbortController();
  const to = setTimeout(() => ac.abort(new DOMException("Timeout", "AbortError")), timeoutMs);
  if (signal) {
    if ((signal as AbortSignal).aborted) ac.abort();
    (signal as AbortSignal).addEventListener("abort", () => ac.abort(), { once: true });
  }

  const reqInit: RequestInit = {
    credentials: "include",
    headers: { Accept: "application/json", ...(rest.headers || {}) },
    signal: ac.signal,
    ...rest,
  };

  const key = inflightKey(url, reqInit);
  const doFetch = async (): Promise<Response> => {
    if (inflight.has(key)) return inflight.get(key)!;
    const p = fetch(url, reqInit).finally(() => inflight.delete(key));
    inflight.set(key, p);
    return p;
  };

  let attempt = 0;
  try {
    while (true) {
      attempt++;
      const res = await doFetch();
      if (res.ok) {
        clearTimeout(to);
        if (res.status === 204) return undefined as unknown as T;
        return (await res.json()) as T;
      }
      if (["GET","HEAD","OPTIONS"].includes(String(reqInit.method || "GET").toUpperCase())
          && [502,503,504].includes(res.status) && attempt <= retries) {
        await sleep(150 * attempt * attempt);
        continue;
      }
      const txt = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}: ${txt || res.statusText}`);
    }
  } finally {
    clearTimeout(to);
  }
}

/* ---------------------- Types used by Explorer screens -------------------- */
export type FieldDef = {
  key: string;
  label: string;
  type: "string" | "number" | "boolean" | "date" | "datetime" | "ID";
  source: "site" | "sf" | "qual";
  group?: string;
  qual_section?: string;
};

export type FilterRule = {
  field: string;
  op: "eq" | "neq" | "contains" | "icontains" | "not_contains" | "starts_with" | "ends_with" | ">" | ">=" | "<" | "<=";
  value: any;
};
export type FilterGroup = { logic: "AND" | "OR"; rules: (FilterRule | FilterGroup)[] };

export type ExplorerPoint = {
  lat: number | null;
  lng: number | null;
  account_id: string;
  account_name: string;
  city?: string | null;
  country?: string | null;
  badges: { profiling: boolean; qualification: boolean };
  cs?: {
    clinical?: boolean | null;
    referral?: boolean | null;
    detect?: boolean | null;
  };
  meta?: {
    pi_name?: string | null;
    pi_email?: string | null;
    pi_phone?: string | null;
    assignments?: string[];
    assignments_count?: number | null;
    nd_u18?: number | null;
    nd_o18?: number | null;
  };
};
export type ExplorerRow = {
  account_id: string;
  account_name: string;
  country?: string | null;
  city?: string | null;
  opportunity_type?: string | null;
  data: Record<string, any>;
};

/* ---------------------- Payload normalization helpers -------------------- */
type FlatRule = { field: string; operator: string; value: any };

// Nodo de filtro anidado que entenderá el backend v2
type ApiFilterNode =
  | { field: string; operator: string; value: any }
  | { logic: "AND" | "OR"; rules: ApiFilterNode[] };

// Quita prefijos de fuente para las keys de filtro (el backend suele esperar nombres "crudos")
function normalizeFieldKey(k: string): string {
  if (!k) return k;
  return k.replace(/^(sf|qual|site)\./, "");
}


function mapOpFlexible(op?: string): string {
  switch ((op || "").toLowerCase()) {
    case "eq": return "equals";
    case "neq": return "not_equals";
    case "contains":
    case "icontains": return "contains";
    case "not_contains": return "not_contains";
    case "starts_with": return "starts_with";
    case "ends_with": return "ends_with";
    case ">":
    case "gt": return ">";
    case ">=":
    case "ge": return ">=";
    case "<":
    case "lt": return "<";
    case "<=":
    case "le": return "<=";
    case "equals":
    case "not_equals": return op!;
    default: return "equals";
  }
}

// ====== v1 (plano): mantiene compat con backend actual ======
function normalizeFiltersFlat(input: FilterGroup): { logic: "AND" | "OR"; rules: FlatRule[] } {
  const out: FlatRule[] = [];
  const walk = (g: FilterGroup) => {
    for (const r of g.rules) {
      if ((r as any).logic) {
        walk(r as FilterGroup);
      } else {
        const rr = r as any;
        const field = normalizeFieldKey(rr.field);
        const operator = mapOpFlexible(rr.op ?? rr.operator);
        out.push({ field, operator, value: rr.value });
      }
    }
  };
  walk(input);
  return { logic: input.logic, rules: out };
}

// ====== v2 (árbol): preserva AND/OR anidados ======
function normalizeFiltersTree(input: FilterGroup): ApiFilterNode {
  const toNode = (node: FilterGroup | FlatRule | any): ApiFilterNode => {
    if (node && node.logic && Array.isArray(node.rules)) {
      const logic = (node.logic === "OR" ? "OR" : "AND") as "AND" | "OR";
      return {
        logic,
        rules: (node.rules as any[]).map((child) => toNode(child)),
      };
    }
    // hoja
    const field = normalizeFieldKey(node.field);
    const operator = mapOpFlexible(node.op ?? node.operator);
    return { field, operator, value: node.value };
  };
  return toNode(input);
}

/** Re-prefija claves a "sf." para que encajen con los headers de la tabla */
function rePrefixDataKeys(data: Record<string, any> | undefined) {
  const src = data || {};
  const out: Record<string, any> = {};
  for (const [k, v] of Object.entries(src)) {
    if (k.startsWith("sf.") || k.startsWith("qual.") || k.startsWith("site.")) {
      out[k] = v;
    } else if (k.startsWith("Account.")) {
      // ✅ mantener Account.* tal cual (NO lo conviertas a sf.Account.*)
      out[k] = v;
    } else if (k.startsWith("extra.")) {
      // ✅ mantener extra.* tal cual (NO lo conviertas a sf.extra.*)
      out[k] = v;
    } else if (k.startsWith("Account.")) {
      out[k] = v;
    } else {
      // Valores Opportunity “crudos” → sf.<campo>
      out[`sf.${k}`] = v;
    }
  }
  return out;
}

function ensureUniqueLabels(fields: FieldDef[]): FieldDef[] {
  const seen = new Map<string, number>();
  return fields.map((f) => {
    const base = f.label?.trim() || f.key;
    const count = (seen.get(base) || 0) + 1;
    seen.set(base, count);
    if (count === 1) return f;
    return { ...f, label: `${base} (${count})` };
  });
}


/* ------------------------------ Qualifications --------------------------- */
export async function uploadQualification(file: File) {
  const fd = new FormData();
  fd.append("file", file);
  return api<{ status: string; site_id: number; site_name: string; rows: number; geocoded: boolean }>(
    "/api/qualification/upload",
    { method: "POST", body: fd }
  );
}

export type QualificationRow = {
  id: number;
  site_id: number;
  site_name: string;
  file_name: string;
  version?: string | null;
  uploaded_at?: string | null;
  salesforce_account_id?: string | null;
  salesforce_account_name?: string | null;
  city?: string | null;
  country?: string | null;
};

export async function listQualifications(): Promise<{ rows: QualificationRow[] }> {
  const raw = await api<any>("/api/qualification/list");
  return { rows: asArray<QualificationRow>(pick(raw, "rows", [])) };
}

/* ---------------------------- Salesforce linking (SF) -------------------- */
export type SfAccount = {
  id: string;
  name: string;
  type?: string | null;
  shipping_city?: string | null;
  shipping_country?: string | null;
  shipping_street?: string | null;
  shipping_postal_code?: string | null;
};

export async function linkSiteToAccount(site_id: number, account_id: string) {
  return api<{ status: string }>("/api/salesforce/accounts/link-site", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id, account_id }),
  });
}

// ⚠️ Distinto de unlinkSite (tu BD)
export async function unlinkSiteFromAccount(site_id: number) {
  return api<{ status: string }>("/api/salesforce/accounts/unlink-site", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id }),
  });
}

/* -------------------------------- Explorer ------------------------------- */

export async function getExplorerFields(): Promise<{ fields: FieldDef[] }> {
  const raw = await api<any>("/api/explorer/fields");
  const base = asArray<FieldDef>(pick(raw, "fields", []));

  const hasKey = new Set(base.map(f => f.key));

  const virtuals: FieldDef[] = [];
  if (!hasKey.has("sf.MemberName")) {
    virtuals.push({
      key: "sf.MemberName",
      label: "Member (Account)",
      type: "string",
      source: "sf",
      group: "Salesforce",
    });
  }
  const all = [...base, ...virtuals];
  return { fields: ensureUniqueLabels(all) };
}

type ExplorerSearchPayload = { filters: FilterGroup; columns: string[] };

export async function explorerSearch(
  payload: ExplorerSearchPayload
): Promise<{ points: ExplorerPoint[]; rows: ExplorerRow[] }> {
  const cols = (payload.columns ?? []);
  const normalized = {
    // v1: plano (compat)
    filters: normalizeFiltersFlat(payload.filters),
    // v2: árbol (backend nuevo podrá leer `filters_tree`)
    filters_tree: normalizeFiltersTree(payload.filters),
    columns: cols.map((k) => (k.startsWith("sf.") ? k.slice(3) : k)),
  };

  const raw = await api<any>("/api/explorer/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(normalized),
    retries: 1,
    // Las búsquedas pueden tardar bastante (SOQL + joins + geocoding)
    timeoutMs: 90_000,
  });

  const points = asArray<ExplorerPoint>(raw.points || []);
  const rows = asArray<any>(raw.rows || []).map((r: any) => ({
    ...r,
    data: rePrefixDataKeys(r.data),
  }));
  return { points, rows };
}

/* Bootstrap inicial: sólo Salesforce map bootstrap */
type SfBootstrapNode = {
  account_id: string;
  site: string;
  city?: string | null;
  country?: string | null;
  latitude?: number | null;
  longitude?: number | null;
  hasQualification?: boolean;
  hasProfiling?: boolean;
};

export async function explorerBootstrap(): Promise<{ points: ExplorerPoint[]; rows: ExplorerRow[] }> {
  const nodes = asArray<SfBootstrapNode>(await api<any>("/api/salesforce/map/bootstrap", {
    // Al bootstrap también le damos más aire
    timeoutMs: 90_000,
  }));

  const points: ExplorerPoint[] = nodes
    .filter(n => n.latitude != null && n.longitude != null)
    .map(n => ({
      account_id: n.account_id,
      account_name: n.site,
      lat: n.latitude ?? null,
      lng: n.longitude ?? null,
      city: n.city ?? null,
      country: n.country ?? null,
      badges: { profiling: !!n.hasProfiling, qualification: !!n.hasQualification },
      // Copiamos los flags CS y meta que vienen del bootstrap para que el mapa
      // pueda colorear los markers y mostrar la info en el InfoWindow.
      cs: {
        // Compute CS flags from multiple possible locations for robustness:
        // 1) top-level n.cs (preferred), 2) n.meta.csContribution (backend compatibility),
        // 3) legacy extra keys under meta (some backends used extra.CS_* names).
  clinical: !!((n as any).cs?.clinical || (n as any).meta?.csContribution?.INNODIA_Clinical_Trial_Site__c || (n as any).meta?.["extra.CS_Clinical_Site_CS__c"] || (n as any).meta?.["extra.INNODIA_Clinical_Trial_Site__c"]),
        referral: !!((n as any).cs?.referral || (n as any).meta?.csContribution?.Referral_Outreach_Site_Non_CTS__c || (n as any).meta?.["extra.CS_Referral_Outreach_Site__c"]),
        detect: !!((n as any).cs?.detect || (n as any).meta?.csContribution?.Elegible_for_DETECT_Site__c || (n as any).meta?.["extra.CS_Eligible_DETECT__c"]),
      },
      meta: {
        pi_name: (n as any).meta?.pi_name ?? null,
        pi_email: (n as any).meta?.pi_email ?? null,
        pi_phone: (n as any).meta?.pi_phone ?? null,
        assignments: Array.isArray((n as any).meta?.assignments) ? (n as any).meta.assignments : [],
        assignments_count: (n as any).meta?.assignments_count ?? null,
        nd_u18: typeof (n as any).meta?.nd_u18 === "number" ? (n as any).meta.nd_u18 : ((n as any).meta?.nd_u18 ?? null),
        nd_o18: typeof (n as any).meta?.nd_o18 === "number" ? (n as any).meta.nd_o18 : ((n as any).meta?.nd_o18 ?? null),
      },
    }));

  // Debug instrumentation: log the parsed ExplorerPoint for the specific
  // account we're investigating so we can confirm if `cs` survived the
  // bootstrap → client parsing step. Keep it guarded and lightweight.
  try {
    const dbg = points.find(p => p.account_id === "001Vg00000UGORhIAP");
    if (dbg) {
      // Use console.info so the message is visible even if 'Debug' level is filtered
      console.info && console.info("[CTS] explorerBootstrap parsed point for 001Vg00000UGORhIAP:", dbg);
    }
  } catch (e) {
    // swallow - purely diagnostics
  }

  const rows: ExplorerRow[] = nodes.map(n => ({
    account_id: n.account_id,
    account_name: n.site,
    city: n.city ?? null,
    country: n.country ?? null,
    opportunity_type: undefined,
    data: rePrefixDataKeys({
      "Account.BillingCity": n.city ?? null,
      "Account.BillingCountry": n.country ?? null,
    }),
  }));

  return { points, rows };
}

/* ------------------------------ Salesforce session ------------------------ */
export async function salesforceMe() {
  try {
    const res = await fetch(`${API_BASE}/api/salesforce/me`, { credentials: "include" });
    if (!res.ok) return { authenticated: false };
    return res.json();
  } catch {
    return { authenticated: false };
  }
}
export function salesforceLoginRedirect(next: string = "/") {
  const url = `${API_BASE}/api/salesforce/oauth/login?next=${encodeURIComponent(next)}`;
  window.location.href = url;
}
export async function salesforceLogout() {
  await fetch(`${API_BASE}/api/salesforce/logout`, { method: "POST", credentials: "include" });
}

/* ------------------------ Helpers qualification (tu BD) ------------------- */
export async function searchAccounts(query: string) {
  return api<any>("/api/explorer/accounts/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
}

export async function searchSalesforceAccounts(q: string): Promise<{ results: SfAccount[] }> {
  const r = await searchAccounts(q);
  const rows = asArray<any>(r.rows || []);
  const results: SfAccount[] = rows.map((x: any) => ({
    id: x.id,
    name: x.name,
    shipping_city: x.city ?? null,
    shipping_country: x.country ?? null,
  }));
  return { results };
}

export async function linkSite(site_id: number, account_id: string) {
  return api<any>("/api/qualification/link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id, account_id }),
  });
}

export async function unlinkSite(site_id: number) {
  return api<any>("/api/qualification/unlink", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site_id }),
  });
}


/* ---------------- explorer columns fill: batch + cache -------------------- */
type FillResp = { rows: Array<{ account_id: string; data: Record<string, any> }> };

/** ✅ Kill-switch DESACTIVADO: permitimos /columns/fill desde frontend */
const DISABLE_FILL = false;

// Caché de celdas (account_id + column) con TTL y LRU simple
const FILL_TTL_MS = 120_000; // 2 min
const FILL_MAX_CELLS = 5000;
type CellKey = string; // `${account_id}::${column}`
const fillCache = new Map<CellKey, { v: any; exp: number }>();
const touchCache = (k: CellKey, v: any) => {
  if (fillCache.size >= FILL_MAX_CELLS) {
    const n = Math.ceil(FILL_MAX_CELLS * 0.1);
    let i = 0;
    for (const key of fillCache.keys()) { fillCache.delete(key); if (++i >= n) break; }
  }
  fillCache.set(k, { v, exp: now() + FILL_TTL_MS });
};
const readCache = (k: CellKey) => {
  const e = fillCache.get(k);
  if (!e) return undefined;
  if (e.exp < now()) { fillCache.delete(k); return undefined; }
  return e.v;
};

// Batcher por ráfaga
type PendingFill = {
  ids: Set<string>;
  cols: Set<string>;
  resolvers: Array<(v: FillResp) => void>;
  rejecters: Array<(e: any) => void>;
  timer?: number;
};
const pendingFill: PendingFill = { ids: new Set(), cols: new Set(), resolvers: [], rejecters: [] };
const FLUSH_DELAY_MS = 40;

function scheduleFillFlush() {
  if (DISABLE_FILL) return;
  if (pendingFill.timer) return;
  pendingFill.timer = window.setTimeout(async () => {
    pendingFill.timer = undefined;
    const ids = Array.from(pendingFill.ids);
    const cols = Array.from(pendingFill.cols);
    const resolvers = pendingFill.resolvers.slice();
    const rejecters = pendingFill.rejecters.slice();
    pendingFill.ids.clear(); pendingFill.cols.clear();
    pendingFill.resolvers = []; pendingFill.rejecters = [];

    if (ids.length === 0 || cols.length === 0) {
      resolvers.forEach(r => r({ rows: [] }));
      return;
    }
    try {
      const body = JSON.stringify({ account_ids: ids, columns: cols });
      const resp = await api<FillResp>("/api/explorer/columns/fill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
        retries: 1,
      });
      // actualiza cache celda a celda
      for (const row of resp.rows || []) {
        const aid = row.account_id;
        const data = row.data || {};
        for (const [k, v] of Object.entries(data)) {
          const col = k.startsWith("sf.") || k.startsWith("qual.") || k.startsWith("site.") ? k : `sf.${k}`;
          touchCache(`${aid}::${col}`, v);
        }
      }
      resolvers.forEach(r => r(resp));
      // Telemetría opcional
      window.dispatchEvent(new CustomEvent("cts:fill", {
        detail: { scope: "batch", reqCols: cols, idsCount: ids.length, filledRows: resp.rows?.length ?? 0 }
      }));
    } catch (e) {
      rejecters.forEach(rj => rj(e));
    }
  }, FLUSH_DELAY_MS);
}

/** 🚀 ACTIVO: rellena celdas para (ids × columnas), usando caché y batching */
export async function explorerFillColumns(params: {
  account_ids: string[];
  columns: string[];
}): Promise<FillResp> {
  // normaliza entradas
  const ids = Array.from(new Set((params.account_ids || []).filter(Boolean)));
  const cols = Array.from(new Set((params.columns || []).filter(Boolean)));
  const reqCols = cols; // para telemetría

  // Primer corte: lee de caché lo que se pueda
  const missingById: Record<string, string[]> = {};
  const hitRows: Array<{ account_id: string; data: Record<string, any> }> = [];
  for (const id of ids) {
    const rowData: Record<string, any> = {};
    for (const col of cols) {
      const key = `${id}::${col}`;
      const cached = readCache(key);
      if (cached !== undefined) rowData[col] = cached;
      else (missingById[id] ||= []).push(col);
    }
    if (Object.keys(rowData).length) hitRows.push({ account_id: id, data: rowData });
  }

  if (DISABLE_FILL) {
    return { rows: hitRows };
  }

  const idsMissing = Object.keys(missingById);
  if (idsMissing.length === 0) {
    // Todo venía de caché
    window.dispatchEvent(new CustomEvent("cts:fill", {
      detail: { scope: "cache", reqCols, idsCount: ids.length, filledRows: hitRows.length }
    }));
    return { rows: hitRows };
  }

  // Coalesce al batcher global
  return new Promise<FillResp>((resolve, reject) => {
    idsMissing.forEach((id) => pendingFill.ids.add(id));
    cols.forEach((c) => pendingFill.cols.add(c));
    pendingFill.resolvers.push((resp) => {
      const merged = [...hitRows];
      if (resp?.rows?.length) {
        merged.push(
          ...resp.rows.map(r => ({
            account_id: r.account_id,
            data: rePrefixDataKeys(r.data),
          }))
        );
      }
      resolve({ rows: merged });
    });
    pendingFill.rejecters.push(reject);
    scheduleFillFlush();
  });
}

/* ------------------------ Preview qualifications ------------------- */
export async function getQualificationPreviewBySite(siteId: number, limit = 500) {
  const url = `/api/qualification/preview/by-site/${encodeURIComponent(siteId)}?limit=${limit}`;
  try {
    return await api<any>(url, { retries: 1 });
  } catch (e: any) {
    console.error("Preview fetch failed:", url, e);
    throw e;
  }
}

/* ------------------------ Delete excels and Info in DB ------------------- */
export async function deleteQualification(id: number) {
  return api<{ status: string }>(`/api/qualification/delete/${id}`, { method: "DELETE" });
}

/* ------------------------ Salesforce extras: Member & PI ------------------ */
export type SfAccountExtras = {
  account_id: string;
  member: { account_id: string; name?: string | null } | null;
  pi: { contact_id?: string | null; name?: string | null; email?: string | null; phone?: string | null } | null;
};

export async function getSalesforceAccountExtras(accountId: string): Promise<SfAccountExtras> {
  const q = encodeURIComponent(accountId);
  return api<SfAccountExtras>(`/api/salesforce/account-extras?account_id=${q}`);
}

export async function getSalesforceAccountExtrasMany(accountIds: string[]): Promise<Record<string, SfAccountExtras>> {
  const unique = Array.from(new Set(accountIds.filter(Boolean)));
  const pairs = await Promise.all(
    unique.map(async (id) => {
      try {
        const res = await getSalesforceAccountExtras(id);
        return [id, res] as const;
      } catch {
        return [id, { account_id: id, member: null, pi: null }] as const;
      }
    })
  );
  return Object.fromEntries(pairs);
}
