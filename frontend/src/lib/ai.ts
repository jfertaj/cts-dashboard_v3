// src/lib/ai.ts
export type VizPayload = {
  type: "bar" | "line";
  xKey: string;
  yKeys: string[];
  data: Array<Record<string, any>>;
  meta?: { title?: string };
};

export type TablePayload = {
  columns: { key: string; label?: string }[];
  rows: Array<Record<string, any>>;
};

export type ChatResponse = {
  ok?: boolean;
  answer?: string;
  table?: TablePayload;
  visualization?: VizPayload;
  clarify?: { question: string; options: Array<{ label: string; query: string }> };
  last_filters?: Record<string, any>;
};

export async function askAI(
  prompt: string,
  lastTable?: TablePayload | null,
  lastFilters?: Record<string, any> | null
): Promise<ChatResponse> {
  const payload: any = {
    messages: [{ role: "user", content: prompt }],
  };

  // Incluir last_table si está disponible para follow-ups eficientes
  if (lastTable && lastTable.rows && lastTable.rows.length > 0) {
    payload.last_table = lastTable;
  }

  // Incluir last_filters para follow-ups con explorer_search
  if (lastFilters && Array.isArray(lastFilters.rules) && lastFilters.rules.length > 0) {
    payload.last_filters = lastFilters;
  }
  
  const res = await fetch("/api/ai/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}


// ---- Explorer helpers for follow-up ---------------------------------
export async function columnsFill(accountIds: string[], columns: string[]) {
  const res = await fetch("/api/explorer/columns/fill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_ids: accountIds, columns }),
  });
  if (!res.ok) throw new Error(`columns/fill HTTP ${res.status}`);
  return await res.json(); // { rows }
}

type FieldDef = { key: string; label?: string; source?: string; group?: string; type?: string };
let _fieldsCache: FieldDef[] | null = null;
export async function getFieldsIndex(): Promise<FieldDef[]> {
  if (_fieldsCache) return _fieldsCache;
  const r = await fetch("/api/explorer/fields");
  if (!r.ok) throw new Error(`fields HTTP ${r.status}`);
  const j = await r.json();
  _fieldsCache = (j?.fields ?? []) as FieldDef[];
  return _fieldsCache;
}