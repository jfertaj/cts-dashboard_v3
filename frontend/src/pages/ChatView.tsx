// src/pages/ChatView.tsx
import React, { useEffect, useMemo, useRef, useState } from "react";
import { askAI, ChatResponse, getFieldsIndex } from "../lib/ai";
import ChartModal from "../components/ChartModal";
import Moby from "../assets/Moby.png";
import AIResultTable from "../components/AIResultTable";
import { findMatchingAliases, AliasEntry } from "../alias/qualificationAliasPack";

type Msg = { role: "user" | "assistant"; content: React.ReactNode };

export default function ChatView() {
  // ——— Estilos para HTML del asistente (mejora listas y lectura) ———
  const ASSISTANT_HTML_CLS = [
    "text-sm text-gray-800 leading-6 max-w-[68ch]",
    "prose prose-sm prose-blue",
    "prose-headings:mt-3 prose-p:my-2",
    "prose-ul:my-2 prose-ol:my-2",
    "prose-ul:ml-5 prose-ol:ml-5",
    "prose-ul:list-disc prose-ol:list-decimal",
    "prose-li:my-0.5",
    "prose-ul:space-y-1 prose-ol:space-y-1",
    "prose-li:marker:text-gray-500",
  ].join(" ");

  // --- persistence keys ---
  const MSGS_KEY = "moby_chat_messages_v1";
  const INPUT_KEY = "moby_chat_input_v1";
  const TABLE_KEY   = "moby_last_table_v1";
  const FILTERS_KEY = "moby_last_filters_v1";

  const [messages, setMessages] = useState<Msg[]>([
    {
      role: "assistant",
      content: (
        <>
          Hi, I’m Moby{" "}
          <img
            src={Moby}
            alt="Moby the cat"
            width={22}
            style={{ verticalAlign: "middle", display: "inline-block" }}
          />{" "}
          What would you like to explore or visualize?
        </>
      ),
    },
  ]);

  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [lastUserQ, setLastUserQ] = useState<string>("");
  const [lastTableForAI, setLastTableForAI] = useState<{columns: any[], rows: any[]} | null>(null);
  const [lastFiltersForAI, setLastFiltersForAI] = useState<Record<string,any> | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Chart state (reuse your ChartModal)
  type ChartType = "bar" | "line" | "pie";
  const [chartOpen, setChartOpen] = useState(false);
  const [chartType, setChartType] = useState<ChartType>("bar");
  const [chartTitle, setChartTitle] = useState<string>("Explorer Chart");
  const [chartXKey, setChartXKey] = useState<string>("sf.Account.Name");
  const [chartYKeys, setChartYKeys] = useState<string[]>([]);
  const [chartData, setChartData] = useState<Array<Record<string, any>>>([]);
  const labelByKey = useMemo(() => new Map<string, string>(), []);

  // ---- Explorer field catalog (para saber qué columnas reconoce) ----
  const [explorerFields, setExplorerFields] = useState<
    { key: string; type?: string; label?: string }[]
  >([]);
  const explorerKeySet = useMemo(
    () => new Set(explorerFields.map((f) => f.key)),
    [explorerFields]
  );

  // --- labelMap and prettyLabel ---
  // Human‑friendly label overrides for known SF metrics
  const OVERRIDE_LABELS: Record<string, string> = {
    // Canonical SF keys
    "sf.C_Number_of_new_T1D_diagnosed_U_18__c": "New T1D diagnosed < 18",
    "sf.C_Number_of_new_T1D_diagnosed_O_18__c": "New T1D diagnosed ≥ 18",
    "sf.C_Number_of_T1D_Patients_currently_U_18__c": "Current T1D < 18",
    "sf.C_Number_of_T1D_Patients_currently_O_18__c": "Current T1D ≥ 18",
    // Sometimes the agent sends the label text instead of the key — normalize those too
    "C Number Of New T1D Diagnosed U 18": "New T1D diagnosed < 18",
    "C Number Of New T1D Diagnosed O 18": "New T1D diagnosed ≥ 18",
  };

  function prettifyRawLabel(raw: string): string {
    let lbl = String(raw || "")
      .replace(/^sf\./, "")
      .replace(/^C[\s_]+/i, "")         // drop leading "C " or "C_"
      .replace(/[_\.]+/g, " ")          // underscores/dots -> space
      .replace(/\bU[\s_]*18\b/gi, "< 18")
      .replace(/\bO[\s_]*18\b/gi, "≥ 18")
      .replace(/\bNumber\s+Of\b/gi, "Number of")
      .replace(/\s{2,}/g, " ")
      .trim();

    // Title case (keep math symbols intact)
    lbl = lbl.replace(/\b([a-z])/g, (s) => s.toUpperCase());
    return lbl;
  }

  const labelMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const f of explorerFields) {
      if (!f?.key) continue;
      const base = OVERRIDE_LABELS[f.key] || OVERRIDE_LABELS[f.label || ""] || f.label || f.key;
      m.set(f.key, prettifyRawLabel(base));
    }
    return m;
  }, [explorerFields]);

  const prettyLabel = (key: string) =>
    OVERRIDE_LABELS[key] ||
    labelMap.get(key) ||
    prettifyRawLabel(key);

  useEffect(() => {
    (async () => {
      try {
        const res = await fetch("/api/explorer/fields");
        const data = await res.json();
        setExplorerFields(data.fields || []);
      } catch {}
    })();
  }, []);

  // --- auto-scroll ---
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, busy]);

  // --- restore from sessionStorage on mount (only text messages to avoid serializing React nodes) ---
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(MSGS_KEY);
      if (raw) {
        const saved: Array<{ role: "user" | "assistant"; content: string | null }> =
          JSON.parse(raw);
        const rebuilt: Msg[] = saved
          .filter((m) => typeof m.content === "string" && m.content !== null)
          .map((m) => ({ role: m.role, content: m.content as string }));
        if (rebuilt.length) setMessages(rebuilt);
      }
      const inputSaved = sessionStorage.getItem(INPUT_KEY);
      if (inputSaved) setInput(inputSaved);
    } catch {}
  }, []);

  // --- restore last table if present (con botones) ---
  const restoredTableRef = useRef(false);
  useEffect(() => {
    if (restoredTableRef.current) return;
    if (explorerFields.length === 0) return; // esperamos a tener catálogo para mostrar botones correctos
    try {
      const rawFilters = sessionStorage.getItem(FILTERS_KEY);
      if (rawFilters) { try { setLastFiltersForAI(JSON.parse(rawFilters)); } catch {} }

      const raw = sessionStorage.getItem(TABLE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed.columns && parsed.rows) {
          const cols =
            Array.isArray(parsed.columns) && typeof parsed.columns[0] === "string"
              ? (parsed.columns as string[]).map((k) => ({ key: k, label: prettyLabel(k) }))
              : parsed.columns;
          setMessages((m) => [
            ...m,
            {
              role: "assistant",
              content: (
                <ActionableTable
                  columns={cols}
                  rows={parsed.rows}
                  explorerKeySet={explorerKeySet}
                />
              ),
            },
          ]);
          restoredTableRef.current = true;
        }
      }
    } catch {}
  }, [explorerFields, explorerKeySet, prettyLabel]);

  // --- persist on change ---
  useEffect(() => {
    try {
      const serializable = messages.map((m) => ({
        role: m.role,
        content: typeof m.content === "string" ? (m.content as string) : null,
      }));
      sessionStorage.setItem(MSGS_KEY, JSON.stringify(serializable));
    } catch {}
  }, [messages]);

  useEffect(() => {
    try {
      sessionStorage.setItem(INPUT_KEY, input);
    } catch {}
  }, [input]);

  // --------- Componente interno: tabla con botones para Explorer ----------
  function ActionableTable({
    columns,
    rows,
    explorerKeySet,
  }: {
    columns: string[] | Array<{ key: string; label?: string }>;
    rows: Array<Record<string, any>>;
    explorerKeySet: Set<string>;
  }) {
    // normaliza columns a array de keys
    const colKeys: string[] = Array.isArray(columns)
      ? typeof columns[0] === "string"
        ? (columns as string[])
        : (columns as Array<{ key: string }>).map((c) => c.key)
      : [];

    const knownCols = colKeys.filter((c) => explorerKeySet.has(c));
    const hasKnownCols = knownCols.length > 0;
    try {
      if (hasKnownCols) {
        sessionStorage.setItem("cts:explorer:pending-columns", JSON.stringify(knownCols));
      }
    } catch {}

    // buscamos account ids en diferentes formatos
    const accIds: string[] = (rows || [])
      .map(
        (r) =>
          r["sf.Account.Id"] ||
          r["sf.AccountId"] ||
          r["Account.Id"] ||
          r["account_id"] ||
          r["Account Id"] ||
          (r?.data?.["sf.Account.Id"] || r?.data?.["sf.AccountId"]) 
      )
      .filter(Boolean)
      .map((x) => String(x).trim());

    const handleHighlight = () => {
      if (!accIds.length) return;
      window.dispatchEvent(
        new CustomEvent("cts:chat:explorer:highlight", {
          detail: { account_ids: accIds },
        })
      );
      // Legacy event for older Explorer listeners
      window.dispatchEvent(
        new CustomEvent("cts:explorer:highlight", {
          detail: { account_ids: accIds },
        })
      );
    };

    const handleOpenFiltered = () => {
      if (!accIds.length) return;

      // 1) Build filters by Account Id
      const detail = {
        filters: {
          logic: "AND",
          rules: [{ field: "sf.Account.Id", operator: "in", value: accIds }],
        },
      };

      // 2) Persist intent so Explorer can pick it up even if the event is missed
      try {
        sessionStorage.setItem("cts:explorer:pending:set", JSON.stringify(detail));
        sessionStorage.setItem("cts:explorer:pending-ids", JSON.stringify(accIds));
        sessionStorage.setItem("cts:explorer:pending-account-ids", JSON.stringify(accIds));
      } catch {}

      // 3) Switch SPA tab to Explorer (so the listener mounts)
      try {
        // Use path-based navigation so App.tsx's getTabFromURL() picks it up
        const base = `${window.location.origin}`;
        window.history.pushState({}, "", base + "/explorer");
        // notify App to change tab
        window.dispatchEvent(new Event("popstate"));
      } catch {}

      // 4) Dispatch events (once now, and once after a short delay to ensure Explorer is mounted)
      const fire = () => {
        // 1️⃣ Filter the IDs (map + table filtering in Explorer)
        window.dispatchEvent(
          new CustomEvent("cts:explorer:filter-ids", {
            detail: { account_ids: accIds },
          })
        );
        // New event name variant for Explorer listeners
        window.dispatchEvent(
          new CustomEvent("cts:chat:explorer:filter-ids", {
            detail: { account_ids: accIds },
          })
        );
        // 2️⃣ Apply full filter object to Explorer (keeps filters panel in sync)
        window.dispatchEvent(new CustomEvent("cts:chat:explorer:set", { detail }));
        // 3️⃣ Optional: highlight these accounts
        window.dispatchEvent(
          new CustomEvent("cts:chat:explorer:highlight", {
            detail: { account_ids: accIds },
          })
        );
      };

      // Fire immediately (in case Explorer is already mounted)
      fire();
      // Fire again shortly after tab switch to catch fresh listeners
      setTimeout(fire, 100);
    };

    const handleAddColumns = () => {
      if (!hasKnownCols) return;
      window.dispatchEvent(
        new CustomEvent("cts:chat:explorer:columns:add", {
          detail: { columns: knownCols },
        })
      );
    };

    const exportCSV = () => {
      try {
        const header = colKeys.map((k) => (columns as any).find((c: any) => c.key === k)?.label || k);
        const csvRows = [header.join(",")].concat(
          (rows || []).map((r) => colKeys.map((k) => JSON.stringify(r[k] ?? "")).join(","))
        );
        const blob = new Blob([csvRows.join("\n")], { type: "text/csv;charset=utf-8;" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "moby_results.csv";
        a.click();
        URL.revokeObjectURL(url);
      } catch {}
    };

    return (
      <div className="space-y-2">
        <AIResultTable columns={columns as any} rows={rows} />
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
            onClick={exportCSV}
            title="Export to CSV"
            aria-label="Export to CSV"
          >
            ⬇️ <span>Export CSV</span>
          </button>
          {accIds.length > 0 && (
            <>
              <button
                className="inline-flex items-center gap-1.5 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                onClick={handleHighlight}
                title="Highlight these sites in the Explorer map/table"
                aria-label="Highlight in Explorer"
              >
                🔦 <span>Highlight in Explorer</span>
              </button>
              <button
                className="inline-flex items-center gap-1.5 rounded-md border border-indigo-600 bg-indigo-600 px-2.5 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
                onClick={handleOpenFiltered}
                title="Open Explorer filtered to these sites"
                aria-label="Open Explorer filtered"
              >
                🎯 <span>Open in Explorer (filter)</span>
              </button>
            </>
          )}
          {hasKnownCols && (
            <button
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
              onClick={handleAddColumns}
              title="Add these columns to the Explorer table"
              aria-label="Add columns to Explorer"
            >
              ➕ <span>Add columns to Explorer</span>
            </button>
          )}
        </div>
      </div>
    );
  }

  // --------------------- ARTIFACTS HANDLER ---------------------
  const handleArtifacts = (resp: ChatResponse) => {
    // Helpers
    const asNumber = (v: any) => {
      if (typeof v === "number") return v;
      if (v === null || v === undefined) return NaN;
      const n = parseFloat(String(v).replace(/,/g, ""));
      return Number.isFinite(n) ? n : NaN;
    };
    const coerceNumeric = (
      rows: Array<Record<string, any>>,
      yKeys: string[]
    ) => {
      if (!Array.isArray(rows) || !rows.length || !yKeys?.length) return rows || [];
      const Y = new Set(yKeys);
      return rows.map((r) => {
        const out: Record<string, any> = { ...r };
        for (const k of Y) {
          const n = asNumber(out[k]);
          out[k] = Number.isFinite(n) ? n : 0;
        }
        return out;
      });
    };
    const autoYKeys = (rows: Array<Record<string, any>>, xKey: string) => {
      if (!Array.isArray(rows) || !rows.length) return [] as string[];
      const keys = Object.keys(rows[0] || {}).filter((k) => k !== xKey);
      const numeric = keys.filter((k) => Number.isFinite(asNumber(rows[0][k])));
      const preferred = [
        "sf.C_Number_of_T1D_Patients_currently_U_18__c",
        "sf.C_Number_of_T1D_Patients_currently_O_18__c",
        "t1d_u18",
        "t1d_o18",
        "Stage1",
        "Stage2",
      ].filter((k) => numeric.includes(k));
      const fallback = numeric.filter((k) => !preferred.includes(k));
      return preferred.concat(fallback).slice(0, 2);
    };

    // 1) Texto
    const answerText =
      (resp as any)?.answer_md ?? (resp as any)?.answer ?? (resp as any)?.text ?? null;
    if (typeof answerText === "string" && answerText.trim()) {
      setMessages((m) => [...m, { role: "assistant", content: answerText.trim() }]);
    }

    // 1.b) Clarifications (deterministas desde backend)
    if (resp?.clarify?.question && Array.isArray(resp.clarify.options)) {
      const q = resp.clarify.question;
      const opts = resp.clarify.options as Array<{ label: string; query: string }>;
      const Clarify: React.FC = () => {
        const choose = async (opt: { label: string; query: string }) => {
          setMessages((m) => [
            ...m,
            { role: "user", content: `${opt.label}` },
          ]);
          setBusy(true);
          try {
            const next = await askAI(`${lastUserQ}. Clarification: ${opt.query}`, lastTableForAI);
            handleArtifacts(next);
          } catch (e: any) {
            setMessages((m) => [
              ...m,
              { role: "assistant", content: `⚠️ Error: ${e?.message || ""}` },
            ]);
          } finally {
            setBusy(false);
          }
        };
        return (
          <div className="space-y-2">
            <div className="text-sm text-gray-800">{q}</div>
            <div className="flex flex-wrap gap-2">
              {opts.map((o, i) => (
                <button
                  key={i}
                  className="rounded-md border px-2.5 py-1.5 text-xs hover:bg-gray-50"
                  onClick={() => void choose(o)}
                >
                  {o.label}
                </button>
              ))}
            </div>
          </div>
        );
      };
      setMessages((m) => [
        ...m,
        { role: "assistant", content: <Clarify /> },
      ]);
      return;
    }

    // 2) Visualizaciones (prioritario): si hay gráfico, abrirlo y NO mostrar tabla duplicada
    if (resp?.visualization?.data && resp.visualization?.xKey) {
      const v = resp.visualization as any;
      const type: ChartType = v.type === "line" ? "line" : v.type === "pie" ? "pie" : "bar";
      const xKey: string = v.xKey;
      let yKeys: string[] = Array.isArray(v.yKeys) ? v.yKeys : [];
      let data: Array<Record<string, any>> = Array.isArray(v.data) ? v.data : [];
      if (!yKeys.length) yKeys = autoYKeys(data, xKey);
      data = coerceNumeric(data, yKeys);
      setChartType(type);
      setChartXKey(xKey);
      setChartYKeys(yKeys);
      setChartData(data);
      setChartTitle(v.meta?.title || "Explorer Chart");
      setChartOpen(true);
      // No añadir tabla si viene también en la misma respuesta; el gráfico es suficiente
      return;
    }

    // 3) Tabla
    if ((resp as any)?.table?.columns && (resp as any)?.table?.rows) {
      let { columns, rows } = (resp as any).table;
      if (Array.isArray(columns) && typeof columns[0] === "string") {
        columns = (columns as string[]).map((k) => ({ key: k, label: prettyLabel(k) }));
      }

      // Si no hay filas, no mostrar tabla ni persistir
      const hasRows = Array.isArray(rows) && rows.length > 0;
      if (!hasRows) {
        // aún podemos mostrar el texto del modelo ya añadido arriba; no añadimos tabla vacía
        return;
      }

      // Guardar para AI follow-ups eficientes y persistir para restaurar tras navegación
      setLastTableForAI({ columns, rows });
      try {
        const accountIds = (rows || [])
          .map(
            (r: any) =>
              r["sf.Account.Id"] ||
              r["sf.AccountId"] ||
              r?.data?.["sf.Account.Id"] ||
              r?.data?.["sf.AccountId"] ||
              r?.account_id
          )
          .filter(Boolean);
        sessionStorage.setItem(
          TABLE_KEY,
          JSON.stringify({ columns, rows, account_ids: accountIds })
        );
      } catch {}

      // Guardar last_filters si vino en la respuesta (para follow-ups con explorer_search)
      if ((resp as any).last_filters) {
        const lf = (resp as any).last_filters;
        setLastFiltersForAI(lf);
        try { sessionStorage.setItem(FILTERS_KEY, JSON.stringify(lf)); } catch {}
      }

      // Siempre renderiza inmediatamente la tabla recibida
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: (
            <ActionableTable
              columns={columns}
              rows={rows}
              explorerKeySet={explorerKeySet}
            />
          ),
        },
      ]);
    }

    // (visualization handled above)

    // 4) Retrocompat
    const arts = (resp as any)?.artifacts;
    if (Array.isArray(arts)) {
      for (const a of arts) {
        if (a?.type === "chart") {
          const v = a.data || a.options || {};
          const type: ChartType =
            a.chart_kind === "line" || v.type === "line" ? "line" : 
            a.chart_kind === "pie" || v.type === "pie" ? "pie" : "bar";
          if (v?.xKey && v?.data) {
            let yKeys: string[] = Array.isArray(v.yKeys) ? v.yKeys : [];
            let data: Array<Record<string, any>> = Array.isArray(v.data)
              ? v.data
              : [];
            if (!yKeys.length) yKeys = autoYKeys(data, v.xKey);
            data = coerceNumeric(data, yKeys);
            setChartType(type);
            setChartXKey(v.xKey);
            setChartYKeys(yKeys);
            setChartData(data);
            setChartTitle(v.meta?.title || "Explorer Chart");
            setChartOpen(true);
          }
        }
        if (a?.type === "explorer_action" && a?.action === "set_filters") {
          window.dispatchEvent(
            new CustomEvent("cts:chat:explorer:set", { detail: a.payload })
          );
        }
      }
    }
  };

  // --------------------- SEND ---------------------
  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    // Prevent the table-restore useEffect from appending the previous session's
    // table while this new query is being processed. Also clear the saved table
    // so it doesn't get restored again if the new query returns the same data.
    restoredTableRef.current = true;
    try { sessionStorage.removeItem(TABLE_KEY); } catch {}
    setMessages((m) => [...m, { role: "user", content: text }]);
    setLastUserQ(text);
    setInput("");
    setBusy(true);
    try {
      // FOLLOW-UP RESOLVER (antes de ir al LLM)
      const handled = await tryFollowUp(text);
      if (handled) {
        setBusy(false);
        return;
      }
      // LLM
      const resp: ChatResponse = await askAI(text, lastTableForAI, lastFiltersForAI);
      handleArtifacts(resp);
    } catch (e: any) {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: `⚠️ Error contacting Moby: ${e?.message || "Unknown error"}`,
        },
      ]);
    } finally {
      setBusy(false);
    }
  };

  // ---------- Follow-up resolver ----------
  function isFollowUpText(q: string): boolean {
    const s = q.toLowerCase();
    return (
      /(de esos|de esas|de los anteriores|de las anteriores|de los 5|de los cinco|de los resultados|de arriba|sobre esos|sobre las anteriores)/i.test(
        s
      ) ||
      /(of those|from those|among those|of the previous|in the previous|those ones|the above|from the list)/i.test(
        s
      ) ||
      /(de ceux|de celles|parmi ceux|des précédents|parmi les précédents|dans les précédents|ceux-ci|celles-ci|ci-dessus)/i.test(
        s
      ) ||
      /(di quelli|di quelle|tra quelli|tra le precedenti|dei precedenti|nelle precedenti|sopra citati)/i.test(
        s
      ) ||
      /(von diesen|aus diesen|unter diesen|von den vorherigen|unter den vorherigen|oben genannten|oben erwähnten)/i.test(
        s
      ) ||
      /(van die|van deze|uit die|uit deze|onder die|onder deze|van de vorige|uit de vorige|bovenstaande|hierboven|de voorgaande|de eerdere)/i.test(
        s
      )
    );
  }

  // ---------- Helpers de intención ----------
  const DIST_RE =
    /(?:within|≤|<=|radius|radio|distancia|a)\s*(?:de\s*)?(\d{2,4})\s*km/i;
  function extractDistance(q: string): number | null {
    const m = DIST_RE.exec(q);
    return m ? Number(m[1]) : null;
  }

  // Sinónimos rápidos (aún usados por el fuzzy fallback)
  const KEYWORD_ALIASES: Array<{ rx: RegExp; prefer: string[] }> = [
    {
      rx: /\b(farmacia|pharmacy|dispensary)\b.*\b(on[\s-]?site|interna|propia)?/i,
      prefer: [
        "qual.onsite_pharmacy",
        "qual.pharmacy_available",
        "qual.comments_pharmacy",
      ],
    },
    {
      rx: /\b(pernocta|overnight\s*stay|ingreso\s*nocturno)\b/i,
      prefer: [
        "qual.overnight_stay",
        "qual.beds_available",
        "qual.comments_facilities",
      ],
    },
    { rx: /\b(hla\s*typing)\b/i, prefer: ["qual.C_Is_HLA_typing_performed__c"] },
  ];

  // Descubre si la frase parece “consulta textual” (no se usa directamente pero lo dejamos por si lo extiendes)
  function looksTextQuery(q: string): boolean {
    const s = q.toLowerCase();
    const hasNumber = /\d/.test(s);
    const hasComparator =
      /(>=|≤|<=|>|<|=|at least|como mínimo|más de|menos de|greater than|less than)/i.test(
        s
      );
    return !hasNumber && !hasComparator;
  }

  async function findFieldKeysByFuzzy(
    query: string,
    max: number = 6
  ): Promise<string[]> {
    const needle = query.toLowerCase();
    const fields = await getFieldsIndex();
    // 0) preferencia por algunos alias simples
    for (const a of KEYWORD_ALIASES) {
      if (a.rx.test(query)) return a.prefer;
    }
    const scored: Array<{ key: string; score: number }> = [];
    for (const f of fields) {
      const hay = `${f.label ?? ""} ${f.key}`.toLowerCase();
      const tokens = needle.split(/[^a-z0-9_]+/i).filter(Boolean);
      const ok = tokens.every((t) => hay.includes(t));
      if (ok) scored.push({ key: f.key, score: hay.indexOf(tokens[0]) });
    }
    // prioriza qual.* y extra.*
    scored.sort((a, b) => {
      const pa = a.key.startsWith("qual.")
        ? 0
        : a.key.startsWith("extra.")
        ? 1
        : 2;
      const pb = b.key.startsWith("qual.")
        ? 0
        : b.key.startsWith("extra.")
        ? 1
        : 2;
      return pa - pb || a.score - b.score;
    });
    return scored.slice(0, max).map((x) => x.key);
  }

  type Rule = { field: string; operator: string; value?: any };

  // Aplica reglas “contains / = / >= …” en el cliente (por si el backend no soporta todas)
  function applyRulesClient(rows: any[], rules: Rule[]): any[] {
    const cmp = (a: any, op: string, b: any) => {
      if (op === "contains")
        return String(a ?? "")
          .toLowerCase()
          .includes(String(b ?? "").toLowerCase());
      if (op === "=") return String(a) === String(b);
      const na = Number(a),
        nb = Number(b);
      if (!Number.isFinite(na) || !Number.isFinite(nb)) return false;
      if (op === ">=") return na >= nb;
      if (op === ">") return na > nb;
      if (op === "<=") return na <= nb;
      if (op === "<") return na < nb;
      return false;
    };
    return rows.filter((r) => rules.every((rule) => cmp(r[rule.field], rule.operator, rule.value)));
  }

  // ---- helper: comparadores desde texto
  function parseComparatorFromText(
    raw: string
  ): { op: string; value: number } | null {
    const s = raw
      .toLowerCase()
      .replace(/\bcomo mínimo\b|al menos/gi, ">=")
      .replace(/\bmás de\b/gi, ">")
      .replace(/\bmenos de\b/gi, "<")
      .replace(/≥/g, ">=")
      .replace(/≤/g, "<=");
    const m = /(>=|<=|>|<|=)?\s*([\d.,]+)/.exec(s);
    if (!m) return null;
    const op = (m[1] || ">=").trim();
    const value = parseFloat(m[2].replace(",", ""));
    return Number.isFinite(value) ? { op, value } : null;
  }

  async function tryFollowUp(q: string): Promise<boolean> {
    try {
      // 1) ¿Es follow-up (o tiene radio km)?
      const distanceRe =
        /(?:within|radius|radio|distancia\s*(?:de)?|a)\s*(?:de\s*)?(\d{2,4})\s*km/i;
      const isFU = isFollowUpText(q) || distanceRe.test(q);
      if (!isFU) return false;

      // 2) Última tabla → account_ids
      const saved = sessionStorage.getItem(TABLE_KEY);
      if (!saved) return false;
      const parsed = JSON.parse(saved);
      const account_ids: string[] = (parsed?.account_ids || [])
        .map(String)
        .filter(Boolean);
      if (!account_ids.length) return false;

      // 3) Radio
      const m = /(\d{2,4})\s*km/i.exec(q);
      const maxKm = m ? Number(m[1]) : 120;
      const baseAccount = String(account_ids[0]);

      // 4) Construcción **genérica** de filtros
      const rules: any[] = [];

      // 4.a) Comparadores numéricos (fallback por fuzzy)
      {
        const cmp = q
          .toLowerCase()
          .replace("como mínimo", ">=")
          .replace("al menos", ">=")
          .replace("más de", ">")
          .replace("menos de", "<");
        const numRe =
          /(>=|<=|>|<|=)?\s*([\d.,]+)\s*(patients?|pacientes|stage1|stage2|u18|o18)?/i;
        const numMatch = cmp.match(numRe);
        if (numMatch) {
          const op = (numMatch[1] || ">=").trim();
          const val = parseFloat(numMatch[2].replace(",", ""));
          const fields = await findFieldKeysByFuzzy(q);
          for (const f of fields)
            rules.push({ field: f, operator: op, value: val });
        }
      }

      // 4.b) Reglas desde el alias pack (qual y/o SF)
      {
        const matches: AliasEntry[] = findMatchingAliases(q);

        for (const a of matches) {
          // BOOLEAN
          if (a.type === "boolean") {
            const neg = /\b(no|not|sin|without|doesn['’]?t|don['’]?t|ninguna?)\b/i.test(
              q
            );
            for (const key of a.prefer) {
              rules.push({ field: key, operator: "=", value: !neg });
            }
            continue;
          }

          // NUMBER
          if (a.type === "number") {
            const parsedCmp = parseComparatorFromText(q);
            if (parsedCmp) {
              for (const key of a.prefer) {
                rules.push({
                  field: key,
                  operator: parsedCmp.op,
                  value: parsedCmp.value,
                });
              }
            }
            continue;
          }

          // TEXT
          if (a.type === "text") {
            const termMatch = q.match(
              /(?:que\s+ponga|that\s+says|contenga|contains)\s+["“”']?([^"“”']+)["“”']?/i
            );
            const term = (termMatch?.[1] || q).trim();
            for (const key of a.prefer) {
              rules.push({ field: key, operator: "ilike", value: `%${term}%` });
            }
          }
        }
      }

      // 4.c) Texto libre en comentarios (fallback)
      if (/comentari|comment/i.test(q)) {
        const comments = (await getFieldsIndex())
          .filter(
            (f) => /comment/i.test(f.key) || /comment/i.test(f.label || "")
          )
          .slice(0, 5)
          .map((f) => f.key);
        const term = q.replace(/.*comentari[oa]s?:?/i, "").trim();
        for (const k of comments)
          rules.push({ field: k, operator: "ilike", value: `%${term}%` });
      }

      // 4.d) Normaliza operadores si tu backend lo necesita (opcional)
      for (const r of rules) {
        if (r.operator === "contains") {
          r.operator = "ilike";
          r.value = `%${r.value}%`;
        }
      }

      const cleanRules = rules.filter(Boolean);
      const filters = cleanRules.length
        ? { logic: "AND", rules: cleanRules }
        : { logic: "AND", rules: [] };

      // 5) Columnas: añade SF + qual detectadas
      const columns = Array.from(
        new Set([
          "sf.Account.Id",
          "sf.Account.Name",
          "sf.Account.ShippingCountry",
          "sf.Account.ShippingCity",
          ...(cleanRules.map((r: any) => r.field).filter(Boolean) as string[]),
        ])
      );

      // 6) Llamada
      const res = await fetch("/api/explorer/search/within-drive-km", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_account_id: baseAccount,
          max_km: maxKm,
          filters,
          columns,
        }),
      });
      const data = await res.json();
      const rows = data?.rows ?? [];

      const columnsOut = rows.length
        ? Array.from(
            new Set(
              rows.flatMap((r: any) => Object.keys(r))
            )
          ).map((k) => ({ key: k, label: prettyLabel(k) }))
        : [{ key: "account_id", label: "Account Id" }];

      sessionStorage.setItem(
        TABLE_KEY,
        JSON.stringify({ columns: columnsOut, rows, account_ids })
      );

      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: rows.length
            ? `Encontré ${rows.length} centro(s) dentro de ≤ ${maxKm} km.`
            : "No se han encontrado centros con esos criterios.",
        },
        {
          role: "assistant",
          content: (
            <ActionableTable
              columns={columnsOut as any}
              rows={rows}
              explorerKeySet={explorerKeySet}
            />
          ),
        },
      ]);
      return true;
    } catch {
      return false;
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const clearConversation = () => {
    setMessages([
      {
        role: "assistant",
        content: (
          <>
            Hi, I'm Moby{" "}
            <img
              src={Moby}
              alt="Moby the cat"
              width={22}
              style={{ verticalAlign: "middle", display: "inline-block" }}
            />{" "}
            What would you like to explore or visualize?
          </>
        ),
      },
    ]);
    setLastTableForAI(null);
    try {
      sessionStorage.removeItem(MSGS_KEY);
      sessionStorage.removeItem(INPUT_KEY);
      // sessionStorage.removeItem(TABLE_KEY); // opcional
    } catch {}
  };

  // ---- Render helper: formatea strings con saltos de línea y ancho legible ----
  const renderContent = (node: React.ReactNode) => {
    if (typeof node === "string") {
      const looksHtml = /<\/?[a-z][\s\S]*>/i.test(node);
      if (looksHtml) {
        const sanitized = node.replace(/<table[\s\S]*?<\/table>/gi, "");
        return (
          <div
            className={ASSISTANT_HTML_CLS}
            dangerouslySetInnerHTML={{ __html: sanitized }}
          />
        );
      }
      return (
        <div className="text-sm text-gray-800 whitespace-pre-wrap leading-6 max-w-[68ch]">
          {node}
        </div>
      );
    }
    return node;
  };

  return (
    <div className="w-full max-w-[96rem] mx-auto px-6 py-6">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-gray-900 flex items-center gap-2">
            Moby (Chat)
            <img
              src={Moby}
              alt="Moby the cat"
              width={24}
              height={24}
              className="inline-block align-middle"
            />
          </h1>
          <p className="text-sm text-gray-600">
            Type your question in natural language. Moby can query data, adjust
            Explorer filters, and open charts.
          </p>
        </div>
        <button
          className="rounded-md border px-3 py-1.5 text-sm hover:bg-gray-50"
          onClick={clearConversation}
          disabled={busy}
          title="Clear conversation"
        >
          Clear conversation
        </button>
      </div>

      <div className="grid grid-rows-[1fr_auto] h-[72vh] rounded-xl border bg-white shadow-sm overflow-hidden">
        {/* Messages */}
        <div ref={scrollRef} className="overflow-auto p-4 space-y-3">
          {messages.map((m, i) => (
            <div
              key={i}
              className={`max-w-[70ch] ${m.role === "user" ? "ml-auto text-right" : ""}`}
            >
              <div
                className={`inline-block px-3 py-2 rounded-lg text-sm ${
                  m.role === "user"
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-800"
                }`}
                style={{ wordBreak: "break-word" }}
              >
                {renderContent(m.content)}
              </div>
            </div>
          ))}
          {busy && <div className="text-sm text-gray-500">Moby is thinking…</div>}
        </div>

        {/* Input area */}
        <div className="border-t bg-gray-50 p-3">
          <div className="flex items-center gap-2">
            <input
              className="flex-1 rounded-lg border px-3 py-2 text-sm focus:ring-2 focus:ring-[#0072CE] bg-white"
              placeholder="Ask Moby…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              disabled={busy}
              aria-label="Ask Moby"
            />
            <button
              className="rounded-lg bg-[#0072CE] text-white px-4 py-2 text-sm font-medium hover:opacity-90 disabled:opacity-60"
              onClick={send}
              disabled={busy || input.trim().length === 0}
            >
              Send
            </button>
          </div>
        </div>
      </div>

      {/* Chart modal */}
      <ChartModal
        open={chartOpen}
        onClose={() => setChartOpen(false)}
        title={chartTitle}
        data={chartData}
        xKey={chartXKey}
        yKeys={chartYKeys}
        type={chartType}
        xCandidates={[]}
        yCandidates={[]}
        labelByKey={labelByKey}
        onChangeTitle={setChartTitle}
        onChangeType={(t) => setChartType(t as ChartType)}
        onChangeXKey={() => {}}
        onToggleYKey={() => {}}
      />
    </div>
  );
}