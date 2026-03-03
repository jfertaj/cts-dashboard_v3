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


/**
 * Streaming version of askAI — uses the /api/ai/chat/stream SSE endpoint.
 * Calls `onToken(chunk)` for each text fragment Claude generates,
 * then returns the complete ChatResponse when the stream finishes.
 */
export async function askAIStream(
  prompt: string,
  lastTable?: TablePayload | null,
  lastFilters?: Record<string, any> | null,
  onToken?: (text: string) => void,
): Promise<ChatResponse> {
  const payload: any = { messages: [{ role: "user", content: prompt }] };
  if (lastTable && lastTable.rows && lastTable.rows.length > 0)
    payload.last_table = lastTable;
  if (lastFilters && Array.isArray(lastFilters.rules) && lastFilters.rules.length > 0)
    payload.last_filters = lastFilters;

  const res = await fetch("/api/ai/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);

  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResult: ChatResponse = {};

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      let data: any;
      try { data = JSON.parse(line.slice(6)); } catch { continue; }

      if (data.type === "token") {
        onToken?.(data.text ?? "");
      } else if (data.type === "done") {
        const { type: _t, ...rest } = data;
        finalResult = rest as ChatResponse;
      } else if (data.type === "error") {
        throw new Error(data.message ?? "Stream error");
      }
    }
  }
  return finalResult;
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